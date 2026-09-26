"""Auto-apply: an opt-in, config-gated exception to the human-approval hard
requirement (plan §8), never a replacement for it.

Off unless config/risk_limits.yaml -> operational.auto_apply.enabled is true —
`false` is the one-line revert to the original "never executes without a
human approval record" behavior, no code change needed. When on, this module
writes its OWN approve decision for the day's highest-confidence actionable
recommendations (tagged terms.source: "auto" in data/approvals/, never
indistinguishable from a human's) and submits them through the exact same
execute.order_manager.submit_approved_order() a human's approval would use —
every guardrail (kill switch, 5% per-position cap, max concurrent positions,
no shorts, no options, daily-loss halt, max daily trade count) still applies
unchanged. This only automates the decision step, nothing else in the gate.

Every successful submission also gets a synthetic journal.record_entry() —
see _journal_auto_decision() — so the improvement loop
(journal.aggregate_performance() -> historical_hitrate() ->
propose_weight_adjustments()) isn't blind to the majority of this project's
actual trading activity just because no human typed a reasoning string.
"""

from __future__ import annotations

from typing import Any

from trading_agent.config import TRADES_DIR, load_risk_limits
from trading_agent.execute.order_manager import OrderRefused, submit_approved_order
from trading_agent.notify.approval_gateway import record_decision
from trading_agent.utils import load_json_list, today


def _todays_auto_trade_count(day: str | None = None) -> int:
    path = TRADES_DIR / (day or today()) / "orders_submitted.json"
    return sum(1 for t in load_json_list(path) if t.get("source") == "auto")


def _journal_auto_decision(rec: dict[str, Any], checkpoint: str) -> None:
    """Auto-apply picks its own trades with no human reasoning to record —
    but journal.record_entry() was previously only ever called from the
    human-driven CLI (`trading-agent journal record`), so an auto-applied
    order never generated a journal entry at all. That silently starved
    journal.aggregate_performance() (and therefore
    recommendation_engine.historical_hitrate(), the 5th confidence
    component) of the majority of this project's actual trading activity,
    since auto-apply is what does most of the trading.

    A synthetic reasoning string — the same component scores a human
    reviewing this recommendation would have seen — stands in for
    "reasoning in the user's own words." Never allowed to turn a successful
    submission into an "error" result: a broken journal write is logged,
    not raised, same as every other best-effort side effect in this module.
    """
    try:
        from trading_agent.journal import record_entry

        scores = rec.get("component_scores", {})
        reasoning = (
            f"Auto-applied: confidence {rec['confidence']} "
            f"(technical={scores.get('technical')}, sentiment={scores.get('sentiment')}, "
            f"catalyst={scores.get('catalyst')}, fundamental={scores.get('fundamental')})"
        )
        if rec.get("sell_pressure"):
            reasoning += f", sell_pressure={rec['sell_pressure']} (position_pnl_pct={rec.get('position_pnl_pct')})"

        record_entry(
            rec["ticker"],
            checkpoint,
            decision="approve",
            reasoning=reasoning,
            action=rec["action"],
            confidence=rec["confidence"],
        )
    except Exception as exc:  # noqa: BLE001 - a broken journal write must not undo a real submission
        print(f"Could not journal auto-applied {rec['ticker']}: {exc}")


def qty_for_recommendation(rec: dict[str, Any], equity: float, price: float) -> float:
    """suggested_size_pct_of_portfolio, in dollars of equity, converted to a
    whole share count at `price`. Rounds down — this only ever sizes at or
    under the suggested %, never over it."""
    pct = rec.get("suggested_size_pct_of_portfolio") or 0.0
    if pct <= 0 or price <= 0:
        return 0.0
    return float(int((equity * (pct / 100)) // price))


def auto_apply(recs: list[dict[str, Any]], checkpoint: str) -> list[dict[str, Any]]:
    """Auto-approve and submit up to the day's remaining auto_apply cap from
    this checkpoint's recommendations, highest confidence first. The cap is
    shared across all 4 daily checkpoints (re-read from data/trades/ fresh
    each call), not reset per checkpoint.

    Returns one status record per candidate considered — "submitted",
    "refused" (a guardrail said no — the same outcome a human's approval could
    hit), "skipped" (computed qty was 0), or "error" (an exception reaching
    Alpaca). Never raises: one bad candidate must not stop the rest, and the
    checkpoint that called this must not be halted by it either.
    """
    auto_cfg = load_risk_limits().get("operational", {}).get("auto_apply", {})
    if not auto_cfg.get("enabled", False):
        return []

    daily_cap = auto_cfg.get("max_trades_per_day", 5)
    remaining = daily_cap - _todays_auto_trade_count()
    if remaining <= 0:
        return []

    candidates = sorted(
        (r for r in recs if r.get("action") in ("BUY", "SELL")),
        key=lambda r: r["confidence"],
        reverse=True,
    )[:remaining]

    results = []
    for rec in candidates:
        try:
            from trading_agent.data.alpaca_client import get_account, get_latest_quote

            equity = float(get_account()["equity"])
            quote = get_latest_quote(rec["ticker"])
            price = float(quote.get("ask_price") or quote.get("bid_price") or 0.0)
            qty = qty_for_recommendation(rec, equity, price)
            if qty <= 0:
                results.append({"ticker": rec["ticker"], "status": "skipped", "reason": "computed qty is 0"})
                continue

            record_decision(rec["ticker"], checkpoint, "approve", {"qty": qty, "source": "auto"})
            order = submit_approved_order(rec, qty, source="auto")
            _journal_auto_decision(rec, checkpoint)
            results.append({"ticker": rec["ticker"], "status": "submitted", "qty": qty, "order": order})
        except OrderRefused as exc:
            results.append({"ticker": rec["ticker"], "status": "refused", "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one candidate's failure must not stop the rest
            results.append({"ticker": rec["ticker"], "status": "error", "reason": str(exc)})

    return results
