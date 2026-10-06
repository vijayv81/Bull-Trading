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
from trading_agent.utils import append_json, day_dir, load_json_list, today


def _todays_auto_trade_count(day: str | None = None) -> int:
    path = TRADES_DIR / (day or today()) / "orders_submitted.json"
    return sum(1 for t in load_json_list(path) if t.get("source") == "auto")


def _persist_attempt(result: dict[str, Any], checkpoint: str) -> None:
    """Every candidate auto_apply() considers — submitted, refused, skipped,
    or errored — used to live only in this function's return value, read
    once by notify_digest() for that single checkpoint's email and then
    gone. That made "what did auto-apply actually try today" unanswerable
    after the fact — data/trades/ only ever recorded successes
    (orders_submitted.json), so a day of nothing-but-refusals looked
    identical to a day nothing was attempted. Persisted here so
    reporting.report_builder.build_daily_summary() can show the full
    picture, refusals included, not just what went through. Best-effort:
    a write failure is logged, never allowed to turn a real submission
    result into an error.
    """
    try:
        path = day_dir(TRADES_DIR) / f"auto_apply_attempts_{checkpoint}.json"
        append_json(path, {**result, "checkpoint": checkpoint})
    except Exception as exc:  # noqa: BLE001 - a broken audit write must not affect the real outcome
        print(f"Could not persist auto-apply attempt for {result.get('ticker')}: {exc}")


def _record_capped(recs: list[dict[str, Any]], checkpoint: str, daily_cap: int, ordered: int) -> None:
    """Log the actionable candidates the daily cap kept auto-apply from trying.

    Once the day's orders were used up, auto_apply() used to return before
    touching a single candidate, leaving no trace: on 2026-10-05 five
    pre_open SELLs filled the cap, and pre_close's three BUYs (IREN, PDSB,
    SDEV, 85-100 confidence) looked in every report like calls that were
    simply never picked. Status "capped" says what happened — not a refusal
    (no guardrail judged them) and not a skip for size. Persisted only, not
    returned: the per-checkpoint digest stays as it was.
    """
    seen: set[str] = set()
    for rec in sorted(recs, key=lambda r: r["confidence"], reverse=True):
        ticker = str(rec.get("ticker", "")).upper()
        if rec.get("action") not in ("BUY", "SELL") or ticker in seen:
            continue
        seen.add(ticker)
        _persist_attempt(
            {
                "ticker": rec["ticker"],
                "status": "capped",
                "reason": f"daily auto-apply cap reached ({ordered} of {daily_cap} orders today)",
            },
            checkpoint,
        )


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


def _symbols_at_position_cap() -> set[str]:
    """Symbols whose held market value is already at or over
    position.max_position_pct_of_portfolio. Best-effort: an unreadable
    account returns an empty set, and position_size_reason() still refuses
    any over-cap BUY at submission, so nothing gets through either way."""
    try:
        from trading_agent.data.alpaca_client import get_account, get_positions

        cap = load_risk_limits()["position"]["max_position_pct_of_portfolio"]
        equity = float(get_account()["equity"])
        if equity <= 0:
            return set()
        return {
            str(p.get("symbol", "")).upper()
            for p in get_positions()
            if abs(float(p.get("market_value") or 0.0)) / equity * 100 >= cap
        }
    except Exception:  # noqa: BLE001 - a pre-filter, not a guardrail
        return set()


def _held_qty(ticker: str) -> float:
    from trading_agent.data.alpaca_client import get_positions

    for p in get_positions():
        if str(p.get("symbol", "")).upper() == ticker.upper():
            return float(p.get("qty") or 0.0)
    return 0.0


def qty_for_recommendation(rec: dict[str, Any], equity: float, price: float) -> float:
    """suggested_size_pct_of_portfolio, in dollars of equity, converted to a
    whole share count at `price`. Rounds down — this only ever sizes at or
    under the suggested %, never over it.

    suggested_size_pct_of_portfolio is a fresh-position-sizing target — it
    has no idea how much of a held ticker actually exists. That's fine for a
    BUY (position_size_reason() bounds it against the 5% cap either way),
    but for a SELL it produced a qty sized as if opening a new position,
    almost always bigger than what's actually held. guardrails.short_sale_reason()
    then refused it as an attempted short — which meant a stop-loss/
    take-profit-triggered SELL (recommendation_engine.position_sell_pressure())
    could score correctly and still never execute through auto-apply.
    Confirmed live 2026-09-28: GRMLW/ABLVW/APUS all scored SELL on a
    stop-loss breach and every auto-apply attempt on them was refused.
    A SELL is now capped at what's actually held, matching
    short_sale_reason()'s own bound, same as a human sizing an exit would."""
    pct = rec.get("suggested_size_pct_of_portfolio") or 0.0
    if pct <= 0 or price <= 0:
        return 0.0
    qty = float(int((equity * (pct / 100)) // price))
    if rec.get("action") == "SELL":
        qty = min(qty, _held_qty(rec["ticker"]))
    return qty


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
    ordered_today = _todays_auto_trade_count()
    remaining = daily_cap - ordered_today
    if remaining <= 0:
        _record_capped(recs, checkpoint, daily_cap, ordered_today)
        return []

    # A BUY on a symbol already at the per-position cap can't place even one
    # share; it was refused every checkpoint (MGLD/AIFF, 7 times in the week
    # to 2026-10-02). Dropped before any attempt rather than retried.
    at_cap = _symbols_at_position_cap()
    candidates = sorted(
        (
            r
            for r in recs
            if r.get("action") in ("BUY", "SELL")
            and not (r["action"] == "BUY" and str(r["ticker"]).upper() in at_cap)
        ),
        key=lambda r: r["confidence"],
        reverse=True,
    )

    # Walk the whole confidence-ranked list until `remaining` orders are
    # placed. Only submissions count against the cap, so a refused or
    # skipped candidate no longer uses up a slot a lower-ranked one could
    # have filled — previously only the top `remaining` were ever tried.
    results = []
    submitted = 0
    for index, rec in enumerate(candidates):
        if submitted >= remaining:
            # The cap was reached partway through the ranked list; the rest
            # were never tried.
            _record_capped(candidates[index:], checkpoint, daily_cap, ordered_today + submitted)
            break
        try:
            from trading_agent.data.alpaca_client import get_account, get_latest_quote

            equity = float(get_account()["equity"])
            quote = get_latest_quote(rec["ticker"])
            price = float(quote.get("ask_price") or quote.get("bid_price") or 0.0)
            qty = qty_for_recommendation(rec, equity, price)
            if qty <= 0:
                result = {"ticker": rec["ticker"], "status": "skipped", "reason": "computed qty is 0"}
                results.append(result)
                _persist_attempt(result, checkpoint)
                continue

            record_decision(rec["ticker"], checkpoint, "approve", {"qty": qty, "source": "auto"})
            order = submit_approved_order(rec, qty, source="auto")
            _journal_auto_decision(rec, checkpoint)
            result = {"ticker": rec["ticker"], "status": "submitted", "qty": qty, "order": order}
            results.append(result)
            _persist_attempt(result, checkpoint)
            submitted += 1
        except OrderRefused as exc:
            result = {"ticker": rec["ticker"], "status": "refused", "reason": str(exc)}
            results.append(result)
            _persist_attempt(result, checkpoint)
        except Exception as exc:  # noqa: BLE001 - one candidate's failure must not stop the rest
            result = {"ticker": rec["ticker"], "status": "error", "reason": str(exc)}
            results.append(result)
            _persist_attempt(result, checkpoint)

    return results
