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

from trading_agent.config import TRADES_DIR, load_risk_limits
from trading_agent.utils import load_json_list, today

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


def _todays_trade_count() -> int:
    path = TRADES_DIR / today() / "orders_submitted.json"
    return len(load_json_list(path))


def daily_trade_count_reason() -> str | None:
    """Breach reason once today's total submitted orders — every source, BUY
    and SELL alike — reach portfolio.max_daily_trades.

    Distinct from execute.auto_pilot's own `auto_apply.max_trades_per_day`:
    that one paces how many of *auto-apply's own* trades happen today and
    never sees a human's; this is a hard ceiling on the day's order count
    regardless of source, enforced at the same execution boundary as every
    other guardrail (execute.order_manager.submit_approved_order()), so 5
    auto-applied trades plus several more a human approves can't quietly add
    up past the real limit either.

    Counts data/trades/<today>/orders_submitted.json — the same file every
    order is already appended to on submission, so there's no separate
    counter to keep in sync or reset at day boundary; it resets naturally
    because the path is date-partitioned.

    No config key defaults this away silently: a missing/zero
    max_daily_trades is "no cap configured" (None), not always-refuse — same
    convention as position_count_reason().
    """
    cap = load_risk_limits().get("portfolio", {}).get("max_daily_trades")
    if not cap:
        return None

    try:
        count = _todays_trade_count()
    except Exception as exc:
        return f"Cannot verify the {cap}-trade daily cap ({exc}) — refusing to proceed blind."

    if count >= cap:
        return (
            f"Daily trade cap reached: {count} of {cap} orders already submitted today "
            "(counts every source — human and auto-applied alike)."
        )
    return None


def position_count_reason(symbol: str, side: str) -> str | None:
    """Breach reason when a BUY would open a brand-new position past
    position.max_concurrent_positions.

    position_size_reason() bounds any ONE position to 5% of the portfolio,
    but nothing else bounded how many different positions could each sit near
    that cap at once — 10 positions at ~5% each is already half the account,
    20 is all of it, with every individual buy still passing the per-position
    check. This is the aggregate-exposure check that was missing: it counts
    distinct symbols currently held and refuses a BUY that would open a new
    one past the configured cap. Adding to a symbol already held doesn't
    increase that count, so it isn't refused here — position_size_reason()
    is what bounds that case.

    No config key defaults this away silently: a missing/zero
    max_concurrent_positions is treated as "no cap configured" (None) rather
    than always-refuse, matching how the rest of this file only enforces caps
    that are actually set.
    """
    if side.upper() != "BUY":
        return None

    cap = load_risk_limits()["position"].get("max_concurrent_positions")
    if not cap:
        return None

    try:
        held_symbols = {str(p.get("symbol", "")).upper() for p in _positions()}
    except Exception as exc:
        return (
            f"Cannot verify the {cap}-position concurrent-positions cap ({exc}) — "
            "refusing to proceed blind."
        )

    if symbol.upper() in held_symbols:
        return None

    if len(held_symbols) >= cap:
        return (
            f"Opening {symbol} would exceed the {cap}-position concurrent-positions cap "
            f"(already holding {len(held_symbols)}: {', '.join(sorted(held_symbols)) or 'none'})."
        )
    return None


def stale_recommendation_reason(rec: dict[str, Any]) -> str | None:
    """Breach reason when the market has moved since a recommendation was
    scored, checked right before every order submission (auto or human) —
    the only point where it's guaranteed the order is actually about to go
    out. A recommendation can sit for up to
    operational.approval_expiry_hours waiting on a human decision, and even
    auto-apply's own scoring-to-submission gap isn't instantaneous; the
    market doesn't wait with it either way.

    Two independent checks, either one refuses; both fail closed (an
    unreadable quote/bars looks like a breach, not a pass):

    1. **Price drift** — current quote vs. `rec["reference_price"]` (the
       price technical_score() was actually computed from — see
       recommendation_engine.score_candidate()). Refused past
       `execution.max_price_drift_pct`. Skipped (not failed) when the
       recommendation itself has no reference_price — an older record from
       before this field existed, or an ad hoc one from agents/tools.py —
       since there's nothing to compare against.
    2. **Direction re-check** (`execution.reverify_technical`, default
       true) — recomputes technical_score() from fresh bars right now, and
       refuses if the direction it implies (BUY when >= 0.5, else SELL) no
       longer agrees with the recommendation's own `action`. Skipped when
       there still isn't enough bar history to compute one (same
       MIN_BARS_FOR_TECHNICAL threshold used everywhere else).

    Only applies to BUY/SELL — a HOLD never reaches order submission at all.
    """
    action = rec.get("action")
    if action not in ("BUY", "SELL"):
        return None

    symbol = rec["ticker"]
    cfg = load_risk_limits().get("execution", {})
    max_drift_pct = cfg.get("max_price_drift_pct")
    reference_price = rec.get("reference_price")

    if max_drift_pct and reference_price:
        try:
            current_price = _reference_price(symbol)
        except Exception as exc:
            return f"Cannot verify {symbol}'s current price before submission ({exc}) — refusing to proceed blind."

        drift_pct = abs(current_price - reference_price) / reference_price * 100
        if drift_pct > max_drift_pct:
            return (
                f"{symbol}'s price has moved {drift_pct:.2f}% since this recommendation was scored "
                f"({reference_price:.2f} -> {current_price:.2f}), past the {max_drift_pct}% drift limit — "
                "re-run the checkpoint for a fresh recommendation before approving/executing."
            )

    if cfg.get("reverify_technical", True):
        try:
            import pandas as pd

            from trading_agent.data.alpaca_client import get_recent_bars
            from trading_agent.scoring.recommendation_engine import MIN_BARS_FOR_TECHNICAL, technical_score

            bars = pd.DataFrame(get_recent_bars(symbol))
        except Exception as exc:
            return f"Cannot re-verify {symbol}'s technical signal before submission ({exc}) — refusing to proceed blind."

        if len(bars) >= MIN_BARS_FOR_TECHNICAL:
            fresh_action = "BUY" if technical_score(bars) >= 0.5 else "SELL"
            if fresh_action != action:
                return (
                    f"{symbol}'s technical signal has flipped since this recommendation was scored "
                    f"({action} then, {fresh_action} now) — re-run the checkpoint for a fresh read "
                    "before approving/executing."
                )

    return None


def sector_concentration_reason(symbol: str, side: str, qty: float) -> str | None:
    """Breach reason when a BUY would push one sector's share of the
    portfolio past portfolio.max_sector_concentration_pct — a config key
    declared from the start but never enforced anywhere: the 5% per-position
    cap and the max-concurrent-positions cap each bound one axis (how big
    ONE position gets, how many distinct ones you hold) but neither bounds
    how concentrated those positions are by sector. Ten different tech
    names could each individually pass both those checks while the account
    is entirely one sector's risk.

    Sector comes from data/market_data.py's get_sector() — yfinance's own
    classification, free and keyless, no new vendor (CLAUDE.md §7.1). Unlike
    every other check in this file, an unknown sector (routine for ETFs
    like SPY, sometimes for newer/foreign listings, or any lookup failure)
    SKIPS this specific check rather than failing closed: sector is a
    best-effort enrichment layer on top of the core account-state
    guardrails above, not something this project can independently verify
    the way it can equity/positions/quotes, and refusing every trade
    whenever a free classification happens to be missing would make the
    cap far more disruptive than protective. Account-state reads (equity,
    positions, quote) still fail closed once a sector IS known — only the
    "do we know the sector at all" gate is soft.
    """
    if side.upper() != "BUY":
        return None

    cap = load_risk_limits().get("portfolio", {}).get("max_sector_concentration_pct")
    if not cap:
        return None

    from trading_agent.data.market_data import get_sector

    sector = get_sector(symbol)
    if not sector:
        return None

    try:
        equity = float(_account()["equity"])
        price = _reference_price(symbol)
        positions = _positions()
    except Exception as exc:
        return f"Cannot verify the {cap}% sector concentration cap ({exc}) — refusing to proceed blind."

    if equity <= 0:
        return f"Cannot verify the {cap}% sector concentration cap: account equity is {equity}."

    other_sector_value = sum(
        abs(float(p.get("market_value") or 0.0))
        for p in positions
        if str(p.get("symbol", "")).upper() != symbol.upper() and get_sector(str(p.get("symbol", ""))) == sector
    )
    projected = other_sector_value + _existing_position_value(symbol) + qty * price
    pct = projected / equity * 100
    if pct > cap:
        return (
            f"{symbol} ({sector}) would push that sector to {pct:.2f}% of the {equity:,.2f} "
            f"portfolio, over the {cap}% cap."
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
