"""Liquid candidate sourcing: which new tickers a checkpoint researches.

Per user instruction 2026-10-21. New candidates used to be the day's top-10
gainers and top-10 losers over $1. Those are mostly thin, sub-$2 or very
volatile names (the ones behind the September-October losses), so once the
entry filter (scoring/entry_quality.py) started refusing them, every one of
roughly 200 recommendations from 2026-10-13 to 10-15 was a HOLD and the active
slice sat idle.

Now the pool is a curated list of liquid large caps (`candidate_sourcing.universe`)
plus, optionally, the day's movers, screened on daily bars before any research
is spent: price at least `min_price`, 20-day average dollar volume at least
`min_avg_dollar_volume`, 20-day average daily range at most
`max_avg_daily_range_pct` (what the entry filter would refuse anyway), and
enough history for the technical score. Survivors are ranked by that
technical score and the top `max_candidates` are researched. The watchlist,
held positions and the core symbol are added by the orchestrator separately.

It decides what gets looked at, not what gets bought: every survivor still goes
through the same research, scoring, entry filter and guardrails. Ranking by
the technical score is a way to spend research on names that could become a
BUY, not evidence of an edge (that signal has been right 40% of the time).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

DEFAULTS: dict[str, Any] = {
    "mode": "liquid",
    "include_movers": True,
    "min_price": 10.0,
    "min_avg_dollar_volume": 50_000_000,
    "max_avg_daily_range_pct": 8.0,
    "lookback_bars": 20,
    "max_candidates": 10,
    "universe": [],
}


def sourcing_cfg(agent_config: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **(agent_config.get("candidate_sourcing") or {})}


def screen(symbol_bars: dict[str, list[dict]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per symbol that passes, ranked by technical score (highest first):
    {symbol, price, avg_dollar_volume, avg_daily_range_pct, technical}."""
    from trading_agent.scoring.recommendation_engine import MIN_BARS_FOR_TECHNICAL, technical_score

    n = int(cfg["lookback_bars"])
    rows = []
    for symbol, bars in symbol_bars.items():
        df = pd.DataFrame(bars)
        if len(df) < max(n, MIN_BARS_FOR_TECHNICAL) or not {"close", "high", "low", "volume"} <= set(df.columns):
            continue
        tail = df.tail(n)
        price = float(df["close"].iloc[-1])
        if price <= 0 or price < float(cfg["min_price"]):
            continue
        dollar_volume = float((tail["close"] * tail["volume"]).mean())
        if dollar_volume < float(cfg["min_avg_dollar_volume"]):
            continue
        range_pct = float(((tail["high"] - tail["low"]) / tail["close"] * 100).mean())
        if range_pct > float(cfg["max_avg_daily_range_pct"]):
            continue
        rows.append({
            "symbol": symbol,
            "price": round(price, 2),
            "avg_dollar_volume": round(dollar_volume),
            "avg_daily_range_pct": round(range_pct, 2),
            "technical": round(float(technical_score(df)), 4),
        })
    return sorted(rows, key=lambda r: r["technical"], reverse=True)


def select_liquid_candidates(
    movers: dict[str, Any], exclude: set[str], cfg: dict[str, Any]
) -> list[str]:
    """The symbols to research this checkpoint. Best-effort: a bars failure
    returns [] (the watchlist and held positions still run), never raises."""
    from trading_agent.data.alpaca_client import get_recent_bars_multi

    pool = {str(s).upper() for s in cfg.get("universe") or []}
    if cfg.get("include_movers", True):
        for side in ("gainers", "losers"):
            pool |= {str(m["symbol"]).upper() for m in movers.get(side, []) if m.get("symbol")}
    pool -= {s.upper() for s in exclude}
    if not pool:
        return []
    try:
        bars = get_recent_bars_multi(sorted(pool), lookback_days=120)
    except Exception as exc:  # noqa: BLE001 - sourcing is best-effort
        print(f"Liquid candidate screen failed: {exc}")
        return []
    rows = screen(bars, cfg)
    return [r["symbol"] for r in rows[: int(cfg["max_candidates"])]]
