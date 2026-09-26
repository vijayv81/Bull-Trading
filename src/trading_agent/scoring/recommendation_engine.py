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

# technical_score()'s default `slow` window — also the number of daily bars a
# caller must have before treating its return value as a real signal rather
# than the neutral fallback below. orchestrator.py checks bar count against
# this same constant before calling technical_score(), so an insufficient-data
# call becomes an excluded (None) component in score_candidate() rather than a
# silent 0.5 that looks identical to genuine neutral technicals.
MIN_BARS_FOR_TECHNICAL = 50


# orchestrator.run_checkpoint() seeds candidates from top-10 "gainer" movers
# (plan §4) alongside the watchlist — without a cap, a stock already up 20-30%
# in 5 days would score MORE bullish for it, systematically favoring buying
# into an already-extended move. Capped here, not filtered upstream: the
# spread (trend) term is untouched, only the momentum term saturates past
# this, so a genuinely strong trend still scores well without extra credit
# for how far a short-term move has already run.
MOMENTUM_CAP = 0.10


def technical_score(bars: pd.DataFrame, fast: int = 20, slow: int = MIN_BARS_FOR_TECHNICAL) -> float:
    """0-1 technical score from a fast/slow SMA spread plus short-term momentum.

    Returns a neutral 0.5 when there isn't enough history (`bars.empty or
    len(bars) < slow`) — fine for this function's own standalone contract
    (e.g. the interactive agent's price-history tool just wants a display
    number), but a caller feeding this into score_candidate() should treat
    "insufficient data" as no signal at all, not a real neutral reading — see
    MIN_BARS_FOR_TECHNICAL and orchestrator.run_checkpoint().
    """
    if bars.empty or len(bars) < slow:
        return 0.5
    close = bars["close"]
    fast_ma = close.rolling(fast).mean()
    slow_ma = close.rolling(slow).mean()
    spread = (fast_ma.iloc[-1] - slow_ma.iloc[-1]) / slow_ma.iloc[-1]
    momentum = close.pct_change(5).iloc[-1] if len(close) > 5 else 0.0
    momentum_capped = max(-MOMENTUM_CAP, min(MOMENTUM_CAP, momentum))
    score = 0.5 + (spread * 5) + (momentum_capped * 2)
    return max(0.0, min(1.0, score))


def historical_hitrate(ticker: str) -> float | None:
    """Rolling per-ticker accuracy from data/performance/strategy_metrics.json.

    Returns None until enough realized outcomes exist for this ticker — see
    reporting/report_builder.py for how that file gets updated. score_candidate()
    excludes a None component from the weighted score entirely rather than
    substituting a neutral 0.5, which would silently dilute every other
    component's weight with a constant that carries no per-ticker signal.
    """
    path = PERFORMANCE_DIR / "strategy_metrics.json"
    if not path.exists():
        return None
    metrics = json.loads(path.read_text())
    entry = metrics.get("by_ticker", {}).get(ticker)
    return entry.get("hit_rate") if entry else None


def position_sell_pressure(pnl_pct: float, stop_loss_pct: float, take_profit_pct: float) -> float:
    """0 (no sell pressure) to 1 (strong sell pressure) from how far a held
    position's unrealized return has moved past either the stop-loss (cut
    losses) or take-profit (lock in gains) threshold — both extremes point
    toward selling, for opposite reasons, so this is a tent shape, not a
    simple bullish/bearish scale: 0 for anything comfortably inside the
    range, 0.5 right at either threshold, ramping linearly to 1.0 by the time
    the position is twice as far past it.
    """
    if pnl_pct <= -stop_loss_pct:
        overshoot = (-pnl_pct - stop_loss_pct) / stop_loss_pct
        return min(1.0, 0.5 + 0.5 * overshoot)
    if pnl_pct >= take_profit_pct:
        overshoot = (pnl_pct - take_profit_pct) / take_profit_pct
        return min(1.0, 0.5 + 0.5 * overshoot)
    return 0.0


def score_candidate(
    ticker: str,
    checkpoint: str,
    sentiment: float,
    technical: float | None,
    fundamental: float | None,
    catalyst: float,
    position_pnl_pct: float | None = None,
    reference_price: float | None = None,
) -> dict[str, Any]:
    """Combine component scores (each 0-1) into a 0-100 confidence + action.

    Record shape matches plan §6.2 exactly, so reports and the approval
    gateway can rely on the field names.

    A component passed as None (no real data yet — see historical_hitrate(),
    orchestrator.py's fundamental=None, and technical=None when there aren't
    enough bars — see MIN_BARS_FOR_TECHNICAL) is excluded from the weighted
    sum rather than treated as a neutral 0.5: a constant baked into every
    ticker's score can't discriminate between them, it only dilutes the
    components that can. Its weight is redistributed proportionally across
    whatever components ARE available this call.

    technical is also the only directional input this formula has — action
    is BUY/SELL by whether technical is >= 0.5. Without it there is no basis
    to guess a direction, so a missing technical always forces HOLD, however
    high confidence is from the remaining components alone.

    position_pnl_pct (only passed for a ticker orchestrator.run_checkpoint()
    found in current Alpaca positions — see get_positions()) biases that
    action toward SELL, never overriding technical outright: effective
    direction is computed from `technical * (1 - sell_pressure)`, so a
    strongly bullish technical read can still keep the call BUY/HOLD against
    a mild stop-loss/take-profit breach, while a weak-to-moderate technical
    flips to SELL under the same pressure. Gated by
    `risk_limits.yaml -> position.mandatory_stop_loss` (default true) —
    false is a one-line revert to pure-technical direction, unconditionally,
    same convention as confidence_scaled_sizing/auto_apply.enabled.

    reference_price (the last close technical_score() was actually computed
    from, when bars were available) is recorded on the returned dict so
    guardrails.stale_recommendation_reason() can catch a stale recommendation
    at order-submission time — a recommendation can sit for up to
    approval_expiry_hours waiting on a human, and the market doesn't wait
    with it.
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
    available = {key: value for key, value in components.items() if value is not None}
    if not available:
        raise ValueError(f"No scoring components available for {ticker} — cannot compute a confidence score.")
    weight_total = sum(weights[key] for key in available)
    confidence = sum(weights[key] * value for key, value in available.items()) / weight_total * 100

    risk = load_risk_limits()["position"]
    stop_loss_pct = risk.get("stop_loss_pct", 4.0)
    take_profit_pct = risk.get("take_profit_pct", 8.0)

    sell_pressure = 0.0
    if position_pnl_pct is not None and risk.get("mandatory_stop_loss", True):
        sell_pressure = position_sell_pressure(position_pnl_pct, stop_loss_pct, take_profit_pct)

    action: Action = "HOLD"
    if technical is not None:
        effective_technical = technical * (1 - sell_pressure)
        if confidence >= risk["min_confidence_to_notify"]:
            action = "BUY" if effective_technical >= 0.5 else "SELL"
        elif sell_pressure > 0 and effective_technical < 0.5:
            # A stop-loss/take-profit breach on a held position must still
            # force a SELL even when the fresh confidence blend doesn't
            # clear the notify threshold on its own — cutting a loss (or
            # locking in a gain) is a risk-management decision, not a fresh
            # conviction call, so it can't be silently gated behind the same
            # bar a brand-new BUY idea has to clear. Without this branch, a
            # held position with weak/bearish sentiment+catalyst+technical
            # AND a deep stop-loss breach would report HOLD — exactly the
            # position stop_loss_pct/take_profit_pct exist to catch.
            action = "SELL"

    return {
        "ticker": ticker,
        "checkpoint": checkpoint,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "confidence": round(confidence, 1),
        "component_scores": components,
        "suggested_size_pct_of_portfolio": _suggested_size_pct(action, confidence, risk),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "position_pnl_pct": position_pnl_pct,
        "sell_pressure": round(sell_pressure, 2),
        "reference_price": reference_price,
        "note": "Research-only output, not investment advice.",
    }


def _suggested_size_pct(action: Action, confidence: float, risk: dict[str, Any]) -> float:
    """Position size as a % of portfolio, for a human (or auto-apply) to size from.

    Config-gated (position.confidence_scaled_sizing, default on) so this is a
    one-line revert: false goes back to the original flat behavior (always
    exactly the cap for any actionable rec, confidence-blind). When on, it
    scales linearly from min_position_pct_of_portfolio at the notify threshold
    up to max_position_pct_of_portfolio at confidence 100 — a just-over-threshold
    call gets the floor, not the same size as a 99-confidence one.
    """
    if action == "HOLD":
        return 0.0

    cap_pct = risk["max_position_pct_of_portfolio"]
    if not risk.get("confidence_scaled_sizing", True):
        return cap_pct

    floor_pct = risk.get("min_position_pct_of_portfolio", cap_pct)
    min_confidence = risk["min_confidence_to_notify"]
    span = max(100 - min_confidence, 1e-9)
    lean = min(max((confidence - min_confidence) / span, 0.0), 1.0)
    return round(floor_pct + (cap_pct - floor_pct) * lean, 2)


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
