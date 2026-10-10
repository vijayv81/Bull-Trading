"""Entry quality: is this name one a 4% stop-loss can actually protect?

Per user instruction 2026-10-10, after the week of 2026-10-05 realized -$5.9k.
Almost all of it was sub-$3 names (GRMLW, DAICW, ABLVW, SOAR, INLF, APUS ...)
whose average daily range was 10-67% and whose overnight gaps averaged 4-20%.
For those, a stop at -4% is a wish: the next print is already 15% lower, so
the exits filled at -17% to -48%. The fix on the exit side is to let exits
through (guardrails); this is the entry side, refusing to open a position the
stop cannot protect, and sizing the rest so a normal bad day costs a bounded
share of the portfolio.

Measured from the daily bars the scorer already fetched (no extra call). All
of it is BUY-only and returns None when disabled; a missing reading is "no
opinion" for that rule (fewer than `range_lookback_bars` bars gives no range
figure), except that a short history is itself what makes a name "risky" and
so raises the confidence bar.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from trading_agent.config import load_risk_limits

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "min_price": 2.0,
    "max_avg_daily_range_pct": 8.0,
    "range_lookback_bars": 20,
    "max_daily_move_risk_pct_of_portfolio": 0.30,
    "risky_price_below": 5.0,
    "risky_min_history_bars": 200,
    "risky_min_confidence_to_buy": 92,
}


def entry_quality_cfg() -> dict[str, Any]:
    return {**DEFAULTS, **(load_risk_limits().get("entry_quality") or {})}


def avg_daily_range_pct(bars: pd.DataFrame, lookback: int = 20) -> float | None:
    """Mean of (high - low) / close over the last `lookback` bars, in percent.
    None with fewer bars than that, or without the columns."""
    if bars is None or len(bars) < lookback or not {"high", "low", "close"} <= set(bars.columns):
        return None
    tail = bars.tail(lookback)
    close = tail["close"].astype(float)
    if (close <= 0).any():
        return None
    return float(((tail["high"].astype(float) - tail["low"].astype(float)) / close * 100).mean())


def is_risky(price: float | None, history_bars: int | None, cfg: dict[str, Any]) -> bool:
    """A thin record: cheap, or too little history to trust a trend."""
    if price is not None and price < float(cfg["risky_price_below"]):
        return True
    return history_bars is not None and history_bars < int(cfg["risky_min_history_bars"])


def breach_reason(
    symbol: str,
    price: float | None,
    range_pct: float | None,
    history_bars: int | None,
    confidence: float | None,
    cfg: dict[str, Any],
) -> str | None:
    """First rule a BUY fails, or None. Shared by scoring and the order-time
    backstop so the two can never disagree."""
    if price is not None and price < float(cfg["min_price"]):
        return (
            f"{symbol} is priced at ${price:.4g}, under the ${cfg['min_price']:g} entry floor "
            "(risk_limits.yaml -> entry_quality.min_price): a stop-loss cannot protect a name this cheap."
        )
    if range_pct is not None and range_pct > float(cfg["max_avg_daily_range_pct"]):
        return (
            f"{symbol} moves {range_pct:.1f}% a day on average, over the {cfg['max_avg_daily_range_pct']:g}% limit "
            "(risk_limits.yaml -> entry_quality.max_avg_daily_range_pct): the "
            "stop-loss would be gapped through."
        )
    floor = float(cfg["risky_min_confidence_to_buy"])
    if is_risky(price, history_bars, cfg) and (confidence is None or confidence < floor):
        return (
            f"{symbol} has a thin record (priced under ${cfg['risky_price_below']:g} or under "
            f"{cfg['risky_min_history_bars']} daily bars), so a BUY needs confidence {floor:g}, "
            f"not {confidence}."
        )
    return None


def assess_entry(
    symbol: str,
    price: float | None,
    bars: pd.DataFrame,
    confidence: float | None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The reading recorded on a recommendation:
    {avg_daily_range_pct, history_bars, risky, blocked (reason|None), max_size_pct (float|None)}.
    None when entry_quality is disabled."""
    cfg = cfg or entry_quality_cfg()
    if not cfg.get("enabled", True):
        return None
    range_pct = avg_daily_range_pct(bars, int(cfg["range_lookback_bars"]))
    history = len(bars) if bars is not None else None
    max_size = None
    if range_pct:
        max_size = round(float(cfg["max_daily_move_risk_pct_of_portfolio"]) * 100 / range_pct, 2)
    return {
        "avg_daily_range_pct": None if range_pct is None else round(range_pct, 2),
        "history_bars": history,
        "risky": is_risky(price, history, cfg),
        "blocked": breach_reason(symbol, price, range_pct, history, confidence, cfg),
        "max_size_pct": max_size,
    }
