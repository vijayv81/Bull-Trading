"""Daily/weekly markdown report generation (plan §10).

Reports are generated as files and are meant to be committed alongside the
day's/week's data snapshot (plan §7) so the report and the data behind it
stay linked in git history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading_agent.config import (
    APPROVALS_DIR,
    PERFORMANCE_DIR,
    RECOMMENDATIONS_DIR,
    REPORTS_DIR,
    TRADES_DIR,
    load_agent_config,
)
from trading_agent.utils import load_json_list, today

BENCHMARK_SYMBOL = "SPY"


def _day_recs(day: str) -> list[dict]:
    items = []
    for path in sorted((RECOMMENDATIONS_DIR / day).glob("recs_*.json")) if (RECOMMENDATIONS_DIR / day).exists() else []:
        items.extend(load_json_list(path))
    return items


def _day_decisions(day: str) -> list[dict]:
    items = []
    for path in sorted((APPROVALS_DIR / day).glob("decisions_*.json")) if (APPROVALS_DIR / day).exists() else []:
        items.extend(load_json_list(path))
    return items


def _day_trades(day: str) -> list[dict]:
    path = TRADES_DIR / day / "orders_submitted.json"
    return load_json_list(path)


def _day_auto_apply_attempts(day: str) -> list[dict]:
    """Every candidate execute.auto_pilot.auto_apply() considered that day,
    across all checkpoints — submitted, refused, skipped, or errored, not
    just the successes _day_trades() already covers. See
    auto_pilot._persist_attempt()'s docstring for why this exists: a day of
    nothing-but-refusals used to be indistinguishable from a day nothing was
    attempted.
    """
    items = []
    day_path = TRADES_DIR / day
    if day_path.exists():
        for path in sorted(day_path.glob("auto_apply_attempts_*.json")):
            items.extend(load_json_list(path))
    return items


_CHECKPOINTS = ("pre_open", "market_open", "midday", "pre_close")


def _by_checkpoint_breakdown(recs: list[dict]) -> dict[str, dict[str, int]]:
    """BUY/SELL/HOLD counts per checkpoint, in checkpoint order — a quick
    "what did each checkpoint actually find" view for build_daily_summary(),
    distinct from the flat recommendations_count it already reported.
    """
    breakdown = {}
    for cp in _CHECKPOINTS:
        cp_recs = [r for r in recs if r.get("checkpoint") == cp]
        if not cp_recs:
            continue
        breakdown[cp] = {
            "total": len(cp_recs),
            "buy": sum(1 for r in cp_recs if r.get("action") == "BUY"),
            "sell": sum(1 for r in cp_recs if r.get("action") == "SELL"),
            "hold": sum(1 for r in cp_recs if r.get("action") == "HOLD"),
        }
    return breakdown


def _approvals_summary(decisions: list[dict]) -> dict[str, int]:
    """Approve/reject counts, split by who decided (terms.source == "auto"
    vs. a human) — the same human/auto distinction data/trades/ already
    tags submitted orders with, applied here to every decision recorded,
    not just the ones that went on to execute.
    """
    return {
        "approved": sum(1 for d in decisions if d.get("decision") == "approve"),
        "rejected": sum(1 for d in decisions if d.get("decision") == "reject"),
        "auto": sum(1 for d in decisions if (d.get("terms") or {}).get("source") == "auto"),
        "human": sum(1 for d in decisions if (d.get("terms") or {}).get("source") != "auto"),
    }


def _signal_type_hit_rates() -> dict[str, dict]:
    """Rolling per-signal-type accuracy from data/performance/strategy_metrics.json
    — journal.aggregate_performance()'s `by_signal_type`, the same data
    recommendation_engine.propose_weight_adjustments() reads. {} until enough
    outcome history exists (same "nothing to report yet" convention as that
    function).
    """
    import json

    path = PERFORMANCE_DIR / "strategy_metrics.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("by_signal_type", {})


def _learning_outcome_note(by_signal: dict[str, dict]) -> str | None:
    """A plain-language read of what adopting propose_weight_adjustments()'s
    current proposal would likely change — deliberately modest and
    qualitative (which component would carry more/less weight), never a
    projected return number: the per-signal hit-rate gap says which signal
    has been more directionally reliable, not what doing something about it
    would be worth. None when there's no gap large enough to say anything
    (mirrors propose_weight_adjustments()'s own >15pt threshold, read from
    the same data, so the two never disagree about whether there's a
    finding this run).
    """
    if len(by_signal) < 2:
        return None
    ranked = sorted(by_signal.items(), key=lambda kv: kv[1].get("hit_rate", 0.5))
    worst_signal, worst_stats = ranked[0]
    best_signal, best_stats = ranked[-1]
    if worst_stats.get("hit_rate", 0.5) >= best_stats.get("hit_rate", 0.5) - 0.15:
        return None
    return (
        f"If incorporated, {worst_signal}'s influence on blended confidence would shrink and "
        f"{best_signal}'s would grow — recommendations would lean more on whichever research/technical "
        f"read {best_signal} represents, which has agreed with actual price direction more often "
        f"({best_stats.get('hit_rate'):.0%} vs. {worst_signal}'s {worst_stats.get('hit_rate'):.0%}) "
        f"over the outcome history recorded so far. Not a projected return — a hit-rate gap says which "
        f"signal has been more directionally reliable, not how much that would be worth."
    )


def _unrealized_pnl() -> dict:
    """Live snapshot straight from Alpaca's own per-position figures — Alpaca
    already tracks unrealized P&L against real-time price and cost basis, so
    there's nothing to reconstruct here. Best-effort: a read failure reports
    itself rather than blocking report generation on live account access.
    """
    try:
        from trading_agent.data.alpaca_client import get_positions

        positions = get_positions()
    except Exception as exc:  # noqa: BLE001 - a report must still build without Alpaca reachable
        return {"positions": [], "total": 0.0, "error": str(exc)}

    rows = []
    total = 0.0
    for p in positions:
        pl = float(p.get("unrealized_pl") or 0.0)
        total += pl
        rows.append(
            {
                "symbol": p.get("symbol"),
                "qty": p.get("qty"),
                "avg_entry_price": p.get("avg_entry_price"),
                "current_price": p.get("current_price"),
                "unrealized_pl": round(pl, 2),
                "unrealized_plpc": round(float(p.get("unrealized_plpc") or 0.0) * 100, 2),
            }
        )
    return {"positions": rows, "total": round(total, 2), "error": None}


def _realized_pnl(trades: list[dict]) -> dict:
    """Realized P&L from confirmed fills, average-cost basis per symbol,
    processed in submission order across `trades`.

    A BUY updates the running average cost; a SELL realizes
    (fill_price - avg_cost) * qty against it. An order with no confirmed
    fill yet (`filled_qty`/`filled_avg_price` still null — Alpaca fills a
    market order asynchronously and this project doesn't poll for
    confirmation before writing data/trades/) is counted separately as
    `pending_fills` and excluded from the total, rather than guessed at.
    """
    basis: dict[str, tuple[float, float]] = {}  # symbol -> (qty_held, avg_cost)
    realized_by_symbol: dict[str, float] = {}
    pending_fills = 0

    for t in sorted(trades, key=lambda t: t.get("submitted_at", "")):
        order = t.get("order", {})
        symbol = order.get("symbol")
        side = str(order.get("side") or "").lower()
        filled_qty = order.get("filled_qty")
        filled_price = order.get("filled_avg_price")
        if not symbol or not filled_qty or not filled_price:
            pending_fills += 1
            continue

        filled_qty = float(filled_qty)
        filled_price = float(filled_price)
        held_qty, avg_cost = basis.get(symbol, (0.0, 0.0))

        if side == "buy":
            new_qty = held_qty + filled_qty
            avg_cost = (held_qty * avg_cost + filled_qty * filled_price) / new_qty if new_qty else 0.0
            basis[symbol] = (new_qty, avg_cost)
        elif side == "sell":
            realized_by_symbol[symbol] = realized_by_symbol.get(symbol, 0.0) + (filled_price - avg_cost) * filled_qty
            basis[symbol] = (held_qty - filled_qty, avg_cost)

    return {
        "by_symbol": {s: round(v, 2) for s, v in realized_by_symbol.items()},
        "total": round(sum(realized_by_symbol.values()), 2),
        "pending_fills": pending_fills,
    }


def build_daily_report(day: str | None = None) -> Path:
    day = day or today()
    recs = _day_recs(day)
    decisions = {d["ticker"]: d["decision"] for d in _day_decisions(day)}
    trades = _day_trades(day)

    lines = [f"# Daily Report — {day}", "", "## Recommendations"]
    if not recs:
        lines.append("_No recommendations generated._")
    for rec in recs:
        outcome = decisions.get(rec["ticker"], "expired/no response")
        lines.append(
            f"- **{rec['ticker']}** {rec['action']} (confidence {rec['confidence']}, "
            f"{rec['checkpoint']}) — {outcome}"
        )

    lines += ["", "## Trades Executed (paper)"]
    if not trades:
        lines.append("_No trades executed._")
    for t in trades:
        order = t["order"]
        lines.append(f"- {order.get('symbol')} {order.get('side')} qty={order.get('qty')}")

    guardrail_notes = [d for d in decisions.values() if d not in ("approve", "reject")]
    lines += ["", "## Guardrail / Incident Notes"]
    lines.append("_None recorded._" if not guardrail_notes else "\n".join(guardrail_notes))

    out_dir = REPORTS_DIR / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day}.md"
    path.write_text("\n".join(lines))
    return path


def build_weekly_report(week_start: str | None = None) -> Path:
    """Aggregate the 7 days starting week_start (YYYY-MM-DD, default: most
    recent Monday) into a weekly rollup, including realized P&L for the week
    (from confirmed order fills) and a live unrealized P&L snapshot (straight
    from Alpaca's own per-position figures).
    """
    if week_start:
        start = datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=now.weekday())

    days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    total_recs = total_approved = total_rejected = total_expired = total_trades = 0
    per_day_lines = []
    week_trades = []
    for day in days:
        recs = _day_recs(day)
        decisions = {d["ticker"]: d["decision"] for d in _day_decisions(day)}
        trades = _day_trades(day)
        week_trades.extend(trades)
        approved = sum(1 for r in recs if decisions.get(r["ticker"]) == "approve")
        rejected = sum(1 for r in recs if decisions.get(r["ticker"]) == "reject")
        expired = len(recs) - approved - rejected
        total_recs += len(recs)
        total_approved += approved
        total_rejected += rejected
        total_expired += expired
        total_trades += len(trades)
        if recs or trades:
            per_day_lines.append(
                f"- {day}: {len(recs)} recommendations, {approved} approved, "
                f"{rejected} rejected, {expired} expired, {len(trades)} trades"
            )

    realized = _realized_pnl(week_trades)
    unrealized = _unrealized_pnl()

    pnl_lines = [f"- Realized this week: ${realized['total']:,.2f}"]
    if realized["by_symbol"]:
        pnl_lines += [f"  - {sym}: ${amt:,.2f}" for sym, amt in sorted(realized["by_symbol"].items())]
    if realized["pending_fills"]:
        pnl_lines.append(
            f"  - {realized['pending_fills']} order(s) with no confirmed fill yet, excluded from the total"
        )

    if unrealized["error"]:
        pnl_lines.append(f"- Unrealized (live positions): unavailable — {unrealized['error']}")
    else:
        pnl_lines.append(f"- Unrealized (live, open positions right now): ${unrealized['total']:,.2f}")
        pnl_lines += [
            f"  - {p['symbol']}: ${p['unrealized_pl']:,.2f} ({p['unrealized_plpc']:+.2f}%)"
            for p in unrealized["positions"]
        ]
        if not unrealized["positions"]:
            pnl_lines.append("  - No open positions.")

    lines = [
        f"# Weekly Report — week of {days[0]}",
        "",
        "## Summary",
        f"- Total recommendations: {total_recs}",
        f"- Approved: {total_approved} | Rejected: {total_rejected} | Expired: {total_expired}",
        f"- Trades executed (paper): {total_trades}",
        "",
        "## P&L",
        *pnl_lines,
        "",
        "## Daily breakdown",
        *(per_day_lines or ["_No activity this week._"]),
        "",
        "## Proposed model/config improvements",
        "_Pending your review — see `trading-agent propose-weights`._",
        "",
        "## Notes",
        "Realized P&L only counts orders with a confirmed fill "
        "(`filled_qty`/`filled_avg_price`) — this project doesn't yet poll Alpaca "
        "for fill confirmation after submission, so a market order recorded before "
        "it fills is excluded and counted under pending fills above, not guessed at. "
        "Unrealized P&L is a live snapshot as of report generation, not as of any "
        "particular day in the week.",
    ]

    out_dir = REPORTS_DIR / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    iso = datetime.strptime(days[0], "%Y-%m-%d").isocalendar()
    path = out_dir / f"{iso.year}-W{iso.week:02d}.md"
    path.write_text("\n".join(lines))
    return path


def _portfolio_return_pct() -> float | None:
    """Today's account return so far — same equity-vs-prior-close measure
    guardrails.daily_loss_reason() already uses, so this always agrees with
    what would have halted the routine. None (not 0.0) when Alpaca can't be
    reached, so a benchmark comparison never silently reports a flat day.
    """
    try:
        from trading_agent.data.alpaca_client import get_account

        account = get_account()
        equity = float(account["equity"])
        last_equity = float(account["last_equity"])
    except Exception:  # noqa: BLE001 - a report must still build without live Alpaca access
        return None
    if last_equity <= 0:
        return None
    return round((equity - last_equity) / last_equity * 100, 2)


def _benchmark_return_pct(symbol: str = BENCHMARK_SYMBOL) -> float | None:
    """Thin wrapper over data.alpaca_client.get_market_return_pct() — kept as
    its own name here since this module's callers/tests already refer to it,
    but the actual "latest daily bar vs the one before it" logic is shared
    with orchestrator.run_checkpoint()'s market-regime stop-loss dampening
    (both want the same SPY-style benchmark read, not two copies of it).
    """
    from trading_agent.data.alpaca_client import get_market_return_pct

    return get_market_return_pct(symbol, lookback_days=5)


def build_daily_summary(day: str | None = None) -> dict:
    """End-of-day learnings + benchmark comparison — distinct from
    build_daily_report()'s activity log. Pulls today's journaled reasoning and
    outcomes (data/journal/) as the "findings", and compares the account's
    own return today against BENCHMARK_SYMBOL's.

    Per user instruction 2026-09-30 ("capture everything done during the day
    by the agent"), also includes: a per-checkpoint BUY/SELL/HOLD breakdown
    (`by_checkpoint`), every approve/reject decision split human vs. auto
    (`approvals`), the day's executed trades (`trades`, ticker/side/qty/
    source/checkpoint), and every auto-apply attempt regardless of outcome
    (`auto_apply_attempts` — see auto_pilot._persist_attempt()), so a day
    that was all refusals is visibly different from a quiet one instead of
    the two looking identical.

    Per user instruction 2026-10-01 ("what optimization or learning
    occurred during the day and if they were incorporated and also
    possible outcomes with this learning"): `optimization_proposals` is
    `recommendation_engine.propose_weight_adjustments()`'s current output —
    the same read a human gets from `trading-agent propose-weights`, so the
    email never says anything that command wouldn't also say.
    `scoring_weights` is what's *actually* in effect right now
    (`agent_config.yaml`) — proposals are never auto-applied (plan §6.3,
    hard requirement), so this is how "were they incorporated" gets
    answered honestly: by showing the real weights, not a claim either
    way. `learning_outcome` is a qualitative, non-numeric read of what
    adopting the current proposal would likely change (see
    _learning_outcome_note()) — `None` when there's no gap large enough to
    say anything, same threshold propose_weight_adjustments() itself uses.
    """
    from trading_agent.journal import load_entries
    from trading_agent.scoring.recommendation_engine import propose_weight_adjustments

    day = day or today()
    recs = _day_recs(day)
    decisions = _day_decisions(day)
    trades = _day_trades(day)
    entries = load_entries(day)
    marked_entries = [e for e in entries if e.get("outcome") is not None]
    signal_hit_rates = _signal_type_hit_rates()

    portfolio_pct = _portfolio_return_pct()
    benchmark_pct = _benchmark_return_pct()
    outperformance_pct = (
        round(portfolio_pct - benchmark_pct, 2) if portfolio_pct is not None and benchmark_pct is not None else None
    )

    return {
        "day": day,
        "portfolio_return_pct": portfolio_pct,
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "benchmark_return_pct": benchmark_pct,
        "outperformance_pct": outperformance_pct,
        "recommendations_count": len(recs),
        "trades_count": len(trades),
        "journal_entries": marked_entries,
        "by_checkpoint": _by_checkpoint_breakdown(recs),
        "approvals": _approvals_summary(decisions),
        "trades": [
            {
                "ticker": t.get("order", {}).get("symbol"),
                "side": t.get("order", {}).get("side"),
                "qty": t.get("order", {}).get("qty"),
                "source": t.get("source", "human"),
                "checkpoint": t.get("recommendation", {}).get("checkpoint"),
            }
            for t in trades
        ],
        "auto_apply_attempts": _day_auto_apply_attempts(day),
        "optimization_proposals": propose_weight_adjustments(),
        "scoring_weights": load_agent_config().get("scoring_weights", {}),
        "signal_hit_rates": signal_hit_rates,
        "learning_outcome": _learning_outcome_note(signal_hit_rates),
    }
