"""Portfolio guardrails enforced at both the proposal and the execution boundary.

Each check returns a human-readable refusal reason, or None when the guardrail
is satisfied — callers decide what to do with it. orchestrator.run_checkpoint()
raises RoutineHalted; execute.order_manager.submit_approved_order() raises
OrderRefused. Keeping the checks free of that decision lets the same rule apply
before a recommendation is ever made *and* before an order is ever sent.

Fail-closed: when account state can't be read, the account-dependent checks
report a breach rather than assuming the portfolio is healthy. A guardrail that
silently passes when it cannot see anything is not a guardrail.

Alpaca lookups are imported lazily inside the helpers below so that this module
stays importable without credentials (and so data/alpaca_client.py can import
is_option_symbol from here without a circular import).
"""

from __future__ import annotations

import re
from typing import Any

from trading_agent.config import load_risk_limits

# OCC contract symbol: root + YYMMDD + C/P + 8-digit strike, e.g. AAPL240119C00150000.
# Anchored, so ordinary equity tickers can never match.
OPTION_SYMBOL = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


class RoutineHalted(Exception):
    """Raised when a guardrail stops the checkpoint routine for the day."""


def is_option_symbol(symbol: str) -> bool:
    """True for OCC-style option contract symbols.

    Deliberately has no config escape hatch — "no options, ever" is a code-level
    ban in the same spirit as allow_live_trading, so it cannot be undone by
    editing a YAML file.
    """
    return bool(OPTION_SYMBOL.match(symbol.strip().upper()))


def options_reason(symbol: str) -> str | None:
    if is_option_symbol(symbol):
        return (
            f"{symbol} is an options contract — this project never trades options. "
            "Hard-coded ban in guardrails.is_option_symbol; there is no config override."
        )
    return None


def _account() -> dict[str, Any]:
    from trading_agent.data.alpaca_client import get_account

    return get_account()


def _positions() -> list[dict[str, Any]]:
    from trading_agent.data.alpaca_client import get_positions

    return get_positions()


def _reference_price(symbol: str) -> float:
    from trading_agent.data.alpaca_client import get_latest_quote

    quote = get_latest_quote(symbol)
    # Ask is the realistic fill for a BUY; bid is a usable fallback on thin quotes.
    for key in ("ask_price", "bid_price"):
        value = quote.get(key)
        if value:
            return float(value)
    raise ValueError(f"No usable quote price for {symbol}")


def daily_loss_reason() -> str | None:
    """Breach reason once today's drawdown reaches portfolio.max_daily_drawdown_pct.

    Measured as Alpaca's `equity` against `last_equity` (the prior close), so the
    window resets with each trading day without needing any state on disk.
    """
    cap = load_risk_limits()["portfolio"]["max_daily_drawdown_pct"]
    try:
        account = _account()
        equity = float(account["equity"])
        last_equity = float(account["last_equity"])
    except Exception as exc:
        return f"Cannot verify the {cap}% daily loss cap ({exc}) — refusing to proceed blind."

    if last_equity <= 0:
        return f"Cannot verify the {cap}% daily loss cap: prior-close equity is {last_equity}."

    change_pct = (equity - last_equity) / last_equity * 100
    if change_pct <= -cap:
        return (
            f"Daily loss {change_pct:.2f}% has reached the {cap}% cap — halted for the day "
            f"(equity {equity:,.2f} vs {last_equity:,.2f} at prior close)."
        )
    return None


def short_sale_reason(symbol: str, side: str, qty: float) -> str | None:
    """Breach reason when a SELL would open or increase a short position.

    SELL is only ever meant to reduce an existing long here — the 5% cap in
    position_size_reason() applies to BUY alone, so a SELL that isn't capped
    at the existing holding would open unbounded short exposure with no
    guardrail bounding it at all. Fails closed: an unreadable position looks
    like a breach, not a pass.
    """
    if side.upper() != "SELL":
        return None

    try:
        held = _existing_position_qty(symbol)
    except Exception as exc:
        return f"Cannot verify existing {symbol} holdings before a SELL ({exc}) — refusing to proceed blind."

    if held <= 0:
        return (
            f"SELL {qty} {symbol} refused: no existing long position to reduce (held: {held}). "
            "This project never opens short positions."
        )
    if qty > held:
        return (
            f"SELL {qty} {symbol} refused: only {held} shares held — selling {qty} would open "
            "a short for the remainder. This project never opens short positions."
        )
    return None


def position_size_reason(symbol: str, side: str, qty: float) -> str | None:
    """Breach reason when a BUY would push the position past the per-position cap.

    Counts any existing position in the same symbol, so repeated partial buys
    cannot stack past the cap one approval at a time. Short-sale exposure is
    guarded separately by short_sale_reason() — a SELL never reaches this cap
    check at all.
    """
    if side.upper() != "BUY":
        return None

    cap = load_risk_limits()["position"]["max_position_pct_of_portfolio"]
    try:
        equity = float(_account()["equity"])
        price = _reference_price(symbol)
        existing = _existing_position_value(symbol)
    except Exception as exc:
        return f"Cannot verify the {cap}% per-position cap ({exc}) — refusing to proceed blind."

    if equity <= 0:
        return f"Cannot verify the {cap}% per-position cap: account equity is {equity}."

    projected = existing + qty * price
    pct = projected / equity * 100
    if pct > cap:
        return (
            f"{symbol} would reach {pct:.2f}% of the {equity:,.2f} portfolio "
            f"({projected:,.2f} incl. {existing:,.2f} already held), over the {cap}% cap."
        )
    return None


def _existing_position_value(symbol: str) -> float:
    for position in _positions():
        if str(position.get("symbol", "")).upper() == symbol.upper():
            return abs(float(position.get("market_value") or 0.0))
    return 0.0


def _existing_position_qty(symbol: str) -> float:
    for position in _positions():
        if str(position.get("symbol", "")).upper() == symbol.upper():
            return float(position.get("qty") or 0.0)
    return 0.0
