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

# long_term_trend() needs a meaningful long-run average, not exactly 252
# trading days (a strict "52 weeks") — that would exclude every recently
# listed ticker outright on a gap-free-history technicality. ~40 weeks is
# close enough to represent a real long-run trend while tolerating some
# missing days (holidays, thin trading). Below this, long_term_trend()
# returns None (excluded, not faked) rather than computing a "52-week
# average" from a few months of data.
MIN_BARS_FOR_TREND = 200
# Capped at (roughly) the last 52 weeks even when more history is available,
# so a stock with years of bars doesn't dilute "long-term" into "all-time."
TREND_LOOKBACK_TRADING_DAYS = 252
# orchestrator.run_checkpoint() fetches this many CALENDAR days of bars (not
# just MIN_BARS_FOR_TECHNICAL's shorter window) so technical_score() and
# long_term_trend() can share one Alpaca bars fetch per ticker instead of two
# — comfortably covers TREND_LOOKBACK_TRADING_DAYS after weekends/holidays.
TREND_FETCH_CALENDAR_DAYS = 370
# Same reasoning as MOMENTUM_CAP: this is a confirming, longer-horizon signal
# folded into technical_score(), not the dominant term — capped so a stock
# that's already far above (or below) both its 30-day and 52-week average
# doesn't swamp the SMA-crossover/momentum reading it's added to.
TREND_CAP = 0.15


def long_term_trend(bars: pd.DataFrame) -> dict[str, float | bool] | None:
    """Where the latest close sits versus its own 30-day and ~52-week
    averages — longer-run trend context alongside technical_score()'s
    short/medium-term SMA crossover, per the user's 2026-09-30 request to
    weigh 30-day history against the 52-week average and the latest price.

    None when there isn't roughly 40 weeks of history (MIN_BARS_FOR_TREND) —
    a name that recently listed doesn't have a real 52-week trend to report,
    excluded rather than faked from a shorter window, same "exclude, don't
    fake" convention as every other component in this module. This also
    means most of the thinly-traded micro-cap/warrant tickers this project
    has struggled with (GRMLW, DAICW, ABLVW, SOAR, ...) simply won't have a
    `bullish` long-term trend to lean on — by design, not an oversight: a
    stock with no real track record shouldn't get the market-regime
    stop-loss dampening below, which is meant for an established uptrend
    dipping on broad-market weakness, not a speculative name with no history
    to judge that from.
    """
    if bars.empty or len(bars) < MIN_BARS_FOR_TREND:
        return None
    close = bars["close"]
    latest = float(close.iloc[-1])
    avg_30d = float(close.iloc[-30:].mean())
    avg_52wk = float(close.iloc[-TREND_LOOKBACK_TRADING_DAYS:].mean())
    return {
        "latest_price": latest,
        "avg_30d": round(avg_30d, 4),
        "avg_52wk": round(avg_52wk, 4),
        "pct_vs_30d": round((latest - avg_30d) / avg_30d * 100, 2),
        "pct_vs_52wk": round((latest - avg_52wk) / avg_52wk * 100, 2),
        "bullish": latest > avg_52wk,
    }


def technical_score(bars: pd.DataFrame, fast: int = 20, slow: int = MIN_BARS_FOR_TECHNICAL) -> float:
    """0-1 technical score from a fast/slow SMA spread plus short-term
    momentum, plus (when enough history exists) longer-run trend context
    from long_term_trend() — the latest price's position versus its own
    30-day and 52-week average, capped at TREND_CAP for the same reason
    MOMENTUM_CAP exists: a confirming signal, not one allowed to dominate.

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

    trend = long_term_trend(bars)
    if trend is not None:
        trend_fraction = (trend["pct_vs_30d"] / 100 + trend["pct_vs_52wk"] / 100) / 2
        score += max(-TREND_CAP, min(TREND_CAP, trend_fraction))

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
    long_term_trend_ctx: dict[str, Any] | None = None,
    market_return_pct: float | None = None,
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

    A bullish direction only becomes a BUY at `min_confidence_to_buy`
    (config, default 85 — a materially higher bar than
    `min_confidence_to_notify`, per the user's 2026-09-30 request that a
    purchase needs real conviction, not just "better than a coin flip").
    Below that it reports HOLD instead of BUY — not "BUY at low confidence"
    — so a human reviewing recommendations never sees a purchase idea this
    project itself wouldn't act on. This bar applies to BUY only: a fresh
    bearish call, and a stop-loss/take-profit-forced SELL below, are both
    still gated by the (lower) min_confidence_to_notify / sell_pressure
    logic as before — raising the bar on buying more is not raising the bar
    on cutting a loss. guardrails.min_confidence_reason() is the matching
    execution-time backstop.

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

    Market-regime dampening (position.market_regime_stop_loss_dampening,
    default true): a stop-loss breach (never take-profit — locking in gains
    isn't something a market dip should block) on a position whose own
    long-term trend is still bullish (long_term_trend_ctx["bullish"], see
    long_term_trend()) is scaled down, not ignored, when the drop looks
    systemic rather than stock-specific — `market_return_pct` (the
    benchmark's own same-day return, e.g. SPY) at or below
    market_regime_down_threshold_pct (default -1.0). This is deliberately
    narrow: it never fires without BOTH a genuinely down market AND an
    established (200+ bar) uptrend, so a speculative name with no real
    history — most of the micro-cap/warrant tickers this project has
    struggled with — gets no protection here, same as it gets none from
    long_term_trend() itself. Dampened, not zeroed, because a broad-market
    day doesn't fully rule out real stock-specific weakness either.

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
    min_confidence_to_buy = risk.get("min_confidence_to_buy", 85)

    sell_pressure = 0.0
    regime_dampened = False
    if position_pnl_pct is not None and risk.get("mandatory_stop_loss", True):
        sell_pressure = position_sell_pressure(position_pnl_pct, stop_loss_pct, take_profit_pct)

        is_stop_loss_breach = position_pnl_pct <= -stop_loss_pct
        down_threshold = risk.get("market_regime_down_threshold_pct", 1.0)
        if (
            is_stop_loss_breach
            and risk.get("market_regime_stop_loss_dampening", True)
            and market_return_pct is not None
            and market_return_pct <= -down_threshold
            and long_term_trend_ctx is not None
            and long_term_trend_ctx.get("bullish")
        ):
            sell_pressure *= risk.get("market_regime_dampening_factor", 0.4)
            regime_dampened = True

    action: Action = "HOLD"
    if technical is not None:
        effective_technical = technical * (1 - sell_pressure)
        if confidence >= risk["min_confidence_to_notify"]:
            if effective_technical >= 0.5:
                action = "BUY" if confidence >= min_confidence_to_buy else "HOLD"
            else:
                action = "SELL"
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
        "long_term_trend": long_term_trend_ctx,
        "market_return_pct": market_return_pct,
        "regime_dampened": regime_dampened,
        "note": "Research-only output, not investment advice.",
    }


def _suggested_size_pct(action: Action, confidence: float, risk: dict[str, Any]) -> float:
    """Position size as a % of portfolio, for a human (or auto-apply) to size from.

    Config-gated (position.confidence_scaled_sizing, default on) so this is a
    one-line revert: false goes back to the original flat behavior (always
    exactly the cap for any actionable rec, confidence-blind). When on, it
    scales linearly from a floor confidence up to max_position_pct_of_portfolio
    at confidence 100 — a just-over-the-floor call gets the floor, not the
    same size as a 99-confidence one.

    The floor is action-specific: a BUY can never score below
    min_confidence_to_buy (score_candidate() reports HOLD instead), so a BUY
    scales from THAT bar, not the lower min_confidence_to_notify — otherwise
    every real BUY would be compressed into the top sliver of the range
    instead of actually spanning floor-to-cap. A SELL/HOLD still uses
    min_confidence_to_notify, unchanged.
    """
    if action == "HOLD":
        return 0.0

    cap_pct = risk["max_position_pct_of_portfolio"]
    if not risk.get("confidence_scaled_sizing", True):
        return cap_pct

    floor_pct = risk.get("min_position_pct_of_portfolio", cap_pct)
    min_confidence = risk.get("min_confidence_to_buy", 85) if action == "BUY" else risk["min_confidence_to_notify"]
    span = max(100 - min_confidence, 1e-9)
    lean = min(max((confidence - min_confidence) / span, 0.0), 1.0)
    return round(floor_pct + (cap_pct - floor_pct) * lean, 2)


MIN_OUTCOMES_PER_SIGNAL_FOR_PROPOSAL = 10


def signals_with_enough_outcomes(by_signal: dict[str, dict]) -> dict[str, dict]:
    """The by_signal_type entries with at least
    MIN_OUTCOMES_PER_SIGNAL_FOR_PROPOSAL measured outcomes. A hit rate over 3
    calls is noise: one lucky call moves it 33 points, so comparing two such
    rates says nothing about which signal is more reliable.
    """
    return {s: v for s, v in by_signal.items() if v.get("n", 0) >= MIN_OUTCOMES_PER_SIGNAL_FOR_PROPOSAL}


def propose_weight_adjustments() -> list[str]:
    """Weekly review pass (plan §6.3): reads per-signal-type accuracy and prints
    proposed weight changes. Deliberately does NOT write config/agent_config.yaml
    — a human reviews the proposals and commits the change themselves, so
    strategy drift is never silent/unsupervised.

    Only signals with at least MIN_OUTCOMES_PER_SIGNAL_FOR_PROPOSAL outcomes
    are compared; with fewer than two such signals there is nothing to propose.
    """
    path = PERFORMANCE_DIR / "strategy_metrics.json"
    if not path.exists():
        return ["No performance history yet — nothing to propose."]

    metrics = json.loads(path.read_text())
    all_signals = metrics.get("by_signal_type", {})
    if not all_signals:
        return ["No per-signal-type accuracy recorded yet — nothing to propose."]

    by_signal = signals_with_enough_outcomes(all_signals)
    if len(by_signal) < 2:
        most = max((v.get("n", 0) for v in all_signals.values()), default=0)
        return [
            f"Not enough outcome history to compare signals yet (most-measured signal has {most} "
            f"outcome(s); need at least {MIN_OUTCOMES_PER_SIGNAL_FOR_PROPOSAL} each for two signals) "
            "— nothing to propose."
        ]

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
