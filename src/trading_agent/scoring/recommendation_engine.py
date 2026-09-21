"""Confidence scoring (plan §6): blends research + market data into one
recommendation record. Weights come from config/agent_config.yaml and are
never auto-adjusted — see propose_weight_adjustments() for the human-reviewed
weekly proposal step (§6.3); it only prints, it never writes config.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

import pandas as pd

from trading_agent.config import PERFORMANCE_DIR, load_agent_config, load_risk_limits

Action = Literal["BUY", "SELL", "HOLD"]

# The moving-average crossover from the project's original scaffold, reframed
# as one input (0-1) to the blended confidence score rather than a standalone
# buy/sell/hold call.


def technical_score(bars: pd.DataFrame, fast: int = 20, slow: int = 50) -> float:
    """0-1 technical score from a fast/slow SMA spread plus short-term momentum."""
    if bars.empty or len(bars) < slow:
        return 0.5
    close = bars["close"]
    fast_ma = close.rolling(fast).mean()
    slow_ma = close.rolling(slow).mean()
    spread = (fast_ma.iloc[-1] - slow_ma.iloc[-1]) / slow_ma.iloc[-1]
    momentum = close.pct_change(5).iloc[-1] if len(close) > 5 else 0.0
    score = 0.5 + (spread * 5) + (momentum * 2)
    return max(0.0, min(1.0, score))


def historical_hitrate(ticker: str) -> float:
    """Rolling per-ticker accuracy from data/performance/strategy_metrics.json.

    Defaults to a neutral 0.5 until enough realized outcomes exist — see
    reporting/report_builder.py for how that file gets updated.
    """
    path = PERFORMANCE_DIR / "strategy_metrics.json"
    if not path.exists():
        return 0.5
    metrics = json.loads(path.read_text())
    return metrics.get("by_ticker", {}).get(ticker, {}).get("hit_rate", 0.5)


def score_candidate(
    ticker: str,
    checkpoint: str,
    sentiment: float,
    technical: float,
    fundamental: float,
    catalyst: float,
) -> dict[str, Any]:
    """Combine component scores (each 0-1) into a 0-100 confidence + action.

    Record shape matches plan §6.2 exactly, so reports and the approval
    gateway can rely on the field names.
    """
    weights = load_agent_config()["scoring_weights"]
    hitrate = historical_hitrate(ticker)

    components = {
        "sentiment": sentiment,
        "technical": technical,
        "fundamental": fundamental,
        "catalyst": catalyst,
        "historical_hitrate": hitrate,
    }
    confidence = sum(weights[key] * value for key, value in components.items()) * 100

    risk = load_risk_limits()["position"]
    action: Action = "HOLD"
    if confidence >= risk["min_confidence_to_notify"]:
        action = "BUY" if technical >= 0.5 else "SELL"

    return {
        "ticker": ticker,
        "checkpoint": checkpoint,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "confidence": round(confidence, 1),
        "component_scores": components,
        "suggested_size_pct_of_portfolio": (
            risk["max_position_pct_of_portfolio"] if action != "HOLD" else 0.0
        ),
        "stop_loss_pct": 4.0,
        "take_profit_pct": 8.0,
        "note": "Research-only output, not investment advice.",
    }


def propose_weight_adjustments() -> list[str]:
    """Weekly review pass (plan §6.3): reads per-signal-type accuracy and prints
    proposed weight changes. Deliberately does NOT write config/agent_config.yaml
    — a human reviews the proposals and commits the change themselves, so
    strategy drift is never silent/unsupervised.
    """
    path = PERFORMANCE_DIR / "strategy_metrics.json"
    if not path.exists():
        return ["No performance history yet — nothing to propose."]

    metrics = json.loads(path.read_text())
    by_signal = metrics.get("by_signal_type", {})
    if not by_signal:
        return ["No per-signal-type accuracy recorded yet — nothing to propose."]

    proposals = []
    ranked = sorted(by_signal.items(), key=lambda kv: kv[1].get("hit_rate", 0.5))
    if ranked:
        worst_signal, worst_stats = ranked[0]
        best_signal, best_stats = ranked[-1]
        if worst_stats.get("hit_rate", 0.5) < best_stats.get("hit_rate", 0.5) - 0.15:
            proposals.append(
                f"'{worst_signal}' hit rate ({worst_stats.get('hit_rate'):.0%}) trails "
                f"'{best_signal}' ({best_stats.get('hit_rate'):.0%}) by >15pts — consider "
                f"reducing its weight in config/agent_config.yaml and committing the change."
            )
    return proposals or ["No signal-type gap large enough to warrant a proposal this week."]
