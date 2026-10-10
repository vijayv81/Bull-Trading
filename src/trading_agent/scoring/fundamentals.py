"""0-1 fundamental score for an operating company, from yfinance's own figures.

Per user instruction 2026-10-10: `fundamental` used to be hard-wired to None.
Five readings, each scaled linearly between a floor (0) and a ceiling (1) set
in agent_config.yaml -> fundamentals.ranges (reversed for debt and P/E, where
lower is better), then averaged. Not a valuation model — a sanity signal that
a profitable, growing, modestly levered company scores above a shrinking,
cash-burning one.

"Exclude, don't fake": returns None when the company has no real revenue or
market cap (shells, brand-new listings: yfinance reports junk like a 390% ROE
on a blank check), or when fewer than `min_metrics` readings exist. The caller
then drops the component and renormalises, as for every other missing signal.
"""

from __future__ import annotations

from typing import Any

from trading_agent.config import load_agent_config

# yfinance .info key -> (config range name, invert)
_METRICS = {
    "profitMargins": ("profit_margin", False),
    "revenueGrowth": ("revenue_growth", False),
    "returnOnEquity": ("return_on_equity", False),
    "debtToEquity": ("debt_to_equity", True),
}


def _scale(value: float, lo: float, hi: float, invert: bool) -> float:
    x = (value - lo) / (hi - lo) if hi != lo else 0.5
    x = min(1.0, max(0.0, x))
    return 1.0 - x if invert else x


def fundamental_score(info: dict[str, Any] | None, cfg: dict[str, Any] | None = None) -> float | None:
    cfg = cfg if cfg is not None else load_agent_config().get("fundamentals") or {}
    if not info or not cfg.get("enabled", True):
        return None
    if (info.get("totalRevenue") or 0) < cfg.get("min_revenue", 1_000_000):
        return None
    if (info.get("marketCap") or 0) < cfg.get("min_market_cap", 10_000_000):
        return None
    ranges = cfg.get("ranges") or {}

    readings = []
    for key, (name, invert) in _METRICS.items():
        value = info.get(key)
        if value is not None and name in ranges:
            lo, hi = ranges[name]
            readings.append(_scale(float(value), lo, hi, invert))
    pe = info.get("forwardPE") or info.get("trailingPE")
    if pe is not None and pe > 0 and "pe" in ranges:
        lo, hi = ranges["pe"]
        readings.append(_scale(float(pe), lo, hi, True))  # loss-makers have no P/E; margins already cover them

    if len(readings) < cfg.get("min_metrics", 2):
        return None
    return round(sum(readings) / len(readings), 4)
