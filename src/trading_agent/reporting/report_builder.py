"""Daily/weekly markdown report generation (plan §10).

Reports are generated as files and are meant to be committed alongside the
day's/week's data snapshot (plan §7) so the report and the data behind it
stay linked in git history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trading_agent.config import (
    APPROVALS_DIR,
    PERFORMANCE_DIR,
    RECOMMENDATIONS_DIR,
    REPORTS_DIR,
    TRADES_DIR,
    load_agent_config,
    load_risk_limits,
)
from trading_agent.reporting.sell_results import sell_result_lines, window_sell_results
from trading_agent.utils import load_json_list, today

BENCHMARK_SYMBOL = "SPY"


def _day_recs(day: str) -> list[dict]:
    items = []
    for path in sorted((RECOMMENDATIONS_DIR / day).glob("recs_*.json")) if (RECOMMENDATIONS_DIR / day).exists() else []:
        items.extend(load_json_list(path))
    return items


def _day_halts(day: str) -> list[dict]:
    """Checkpoints that halted that day (notify.approval_gateway.
    record_checkpoint_halt()), in the order they happened."""
    items: list[dict] = []
    if (RECOMMENDATIONS_DIR / day).exists():
        for path in (RECOMMENDATIONS_DIR / day).glob("halt_*.json"):
            items.extend(load_json_list(path))
    return sorted(items, key=lambda h: h.get("halted_at", ""))


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
    finding this run). Applies the same minimum-outcomes floor too.
    """
    from trading_agent.scoring.recommendation_engine import signals_with_enough_outcomes

    by_signal = signals_with_enough_outcomes(by_signal)
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


def _realized_from_sells(sell_results: dict, fallback_trades: list[dict]) -> dict:
    """The realized-P&L dict (same shape as _realized_pnl()) from the window's
    actual SELL fills. data/trades/ never carries fill prices (an order is
    recorded as `pending_new` the instant it's submitted), so _realized_pnl()
    on those files reported $0.00 with every order "pending" — it remains the
    fallback only when Alpaca can't be read."""
    if sell_results.get("error"):
        return _realized_pnl(fallback_trades)
    return {
        "by_symbol": sell_results["by_symbol"],
        "total": sell_results["total_net_usd"],
        "pending_fills": sell_results["pending"],
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


def week_dates(week_start: str | None = None) -> list[str]:
    """The 7 days (YYYY-MM-DD) starting week_start, default: most recent
    Monday — shared by build_weekly_report() and build_weekly_learning_review()
    so both report on exactly the same week for the same `week_start` argument.
    """
    if week_start:
        start = datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=now.weekday())
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]


def _open_option_lines() -> list[str]:
    """The weekly report's improvements section: each optimization option
    still open (optimizations.open_options()) as a markdown line with
    clickable Apply/Dismiss links to the Optimization Ticket page — or the
    apply command when no ticket URL is configured. Per user instruction
    2026-10-02, replacing a static "see `trading-agent propose-weights`"
    pointer the reader couldn't act on from the email."""
    from trading_agent.notify.approval_gateway import optimization_link
    from trading_agent.optimizations import open_options

    options = open_options()
    if not options:
        return [
            "_No open options right now. The Saturday weekly learning review proposes new ones, "
            "each with Apply/Dismiss links._"
        ]
    lines = []
    for option in options:
        flag = " (loosens a guardrail)" if option.get("loosens_guardrail") else ""
        diff = ", ".join(f"`{c['path']}` {c['from']} → {c['to']}" for c in option["changes"])
        apply_href, dismiss_href = optimization_link(option, "apply"), optimization_link(option, "dismiss")
        if apply_href and dismiss_href:
            label = "Review & apply" if option.get("loosens_guardrail") else "Apply this change"
            actions = f"[{label}]({apply_href}) · [Dismiss]({dismiss_href})"
        else:
            ack = " --acknowledge-loosening" if option.get("loosens_guardrail") else ""
            actions = f"`trading-agent optimizations apply {option['id']}{ack}`"
        lines.append(f"- **{option['title']}**{flag}: {diff}. {actions}")
    return lines


def build_weekly_report(week_start: str | None = None) -> Path:
    """Aggregate the 7 days starting week_start (YYYY-MM-DD, default: most
    recent Monday) into a weekly rollup, including realized P&L for the week
    (from confirmed order fills) and a live unrealized P&L snapshot (straight
    from Alpaca's own per-position figures).
    """
    days = week_dates(week_start)

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

    sell_results = window_sell_results(days[0], days[-1], week_trades)
    realized = _realized_from_sells(sell_results, week_trades)
    unrealized = _unrealized_pnl()

    pnl_lines = [f"- Realized this week: ${realized['total']:,.2f}"]
    if realized["by_symbol"]:
        pnl_lines += [f"  - {sym}: ${amt:,.2f}" for sym, amt in sorted(realized["by_symbol"].items())]
    if realized["pending_fills"]:
        pnl_lines.append(
            f"  - {realized['pending_fills']} sell order(s) not filled yet, excluded from the total"
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
        "## Sell orders executed — net result",
        *sell_result_lines(sell_results, "this week"),
        "",
        "## Daily breakdown",
        *(per_day_lines or ["_No activity this week._"]),
        "",
        "## Proposed model/config improvements",
        *_open_option_lines(),
        "",
        "## Notes",
        "Realized P&L and the sell-order results come from Alpaca's filled orders "
        "(average-cost basis per symbol over the account's whole fill history), not from "
        "data/trades/ — those records are written at submission, before any fill. A sell "
        "that hasn't filled yet is excluded and counted above, and one with no known cost "
        "basis is listed but kept out of the total, not guessed at. "
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


def _as_of_note(now: datetime | None = None) -> str:
    """When the figures were taken. The summary goes out from the pre_close
    run, a few minutes before the 4:00pm ET close, so today's return and the
    marked outcomes are not final-close numbers — say so rather than let
    them read as end-of-day."""
    from zoneinfo import ZoneInfo

    et = (now or datetime.now(ZoneInfo("America/New_York"))).astimezone(ZoneInfo("America/New_York"))
    note = f"Figures as of {et:%H:%M} ET"
    if et.weekday() < 5 and (et.hour, et.minute) < (16, 0):
        note += " — before the 4:00pm ET close, so today's return and outcomes are not final"
    return note


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
    from trading_agent.optimizations import options_awaiting_review
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
        # Proposed changes with an open PR (optimizations.propose_daily() +
        # record_pr()), each carrying its pr_url — what the email's "review &
        # approve" section links. Includes an earlier day's still-unmerged PR.
        "optimization_options": options_awaiting_review(day),
        "as_of_note": _as_of_note(),
        "halted_checkpoints": _day_halts(day),
        # Net result of every SELL that filled that day (Alpaca fills, average cost).
        "sell_results": window_sell_results(day, day, trades),
    }


# --- Weekly learning review (per user instruction 2026-10-02) ---------------
#
# "Evaluate all trades, portfolio status, trade orders not placed and
# opportunities lost. This should feed and optimize for following week runs.
# Email should be sent out based on analysis, what recommendations were made
# and why." A new Saturday-morning routine, separate from the Friday
# pre_close weekly report above — see routines/weekly_learning_review.md for
# the schedule and what the routine session should do with this.
#
# "Feed and optimize for following week runs" means exactly what
# propose_weight_adjustments() already means in this codebase (plan §6.3):
# surfaced for a human to read and decide on, never applied automatically.
# This review adds nothing that writes config/agent_config.yaml or
# config/risk_limits.yaml — it only reads more of the week's data than any
# other report here (recommendations with no trade behind them, not just
# the ones that executed) and renders what it finds.

_REFUSAL_CATEGORIES = [
    # Checked in order — "cannot verify" first, since a fail-closed guardrail's
    # message can also mention a cap by name (e.g. "Cannot verify the 5.0%
    # per-position cap (...)"), and "can't read account/quote state" is a
    # different, more useful bucket than "breached a real number".
    ("cannot verify", "data unavailable (failed closed)"),
    ("is an options contract", "options ban"),
    ("bar required to buy", "below min confidence to buy"),
    ("daily loss", "daily loss halt"),
    ("no existing long position to reduce", "short-sale block"),
    ("would open a short", "short-sale block"),
    ("would reach", "position size cap"),
    ("per-position cap", "position size cap"),
    ("concurrent-positions cap", "max concurrent positions"),
    ("sector concentration cap", "sector concentration cap"),
    ("daily trade cap", "daily trade count cap"),
    ("drift limit", "stale: price drift"),
    ("technical signal has flipped", "stale: technical reversal"),
    ("no approval record", "no approval record"),
    ("has expired", "approval expired"),
    ("does not match approved qty", "qty mismatch"),
    ("kill switch", "kill switch off"),
    ("not approve", "not approved"),
]


def _categorize_refusal(reason: str) -> str:
    """Buckets a guardrails.py / order_manager.py refusal string into a short
    label for the weekly review's refusal breakdown, matched by substring
    against the fixed phrases those modules actually raise (see
    guardrails.py). "other" is the honest fallback for a message that
    doesn't match any of them — e.g. a raw Alpaca API error reaching
    auto_apply() as an "error" status, not a guardrail refusal at all.
    """
    reason_lower = (reason or "").lower()
    for needle, label in _REFUSAL_CATEGORIES:
        if needle in reason_lower:
            return label
    return "other"


def _portfolio_status_snapshot() -> dict[str, Any]:
    """Live equity/cash/buying-power snapshot for the weekly learning
    review's portfolio-status section. Best-effort, same convention as every
    other live Alpaca read in this module: a failure reports itself rather
    than blocking the rest of the review.
    """
    try:
        from trading_agent.data.alpaca_client import get_account

        account = get_account()
        return {
            "equity": round(float(account.get("equity") or 0.0), 2),
            "cash": round(float(account.get("cash") or 0.0), 2),
            "buying_power": round(float(account.get("buying_power") or 0.0), 2),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - a report must still build without live Alpaca access
        return {"equity": None, "cash": None, "buying_power": None, "error": str(exc)}


MAX_MISSED_OPPORTUNITY_LOOKUPS = 30


def _missed_opportunity_counterfactuals(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For a sample of this week's unexecuted actionable recommendations
    (deduped by ticker, highest confidence first, capped at
    MAX_MISSED_OPPORTUNITY_LOOKUPS live quote lookups so a heavy week doesn't
    turn this into dozens of sequential Alpaca calls), reports what the price
    has actually done since vs. the recommendation's own reference_price —
    the same comparison journal.mark_outcomes() does for a decided-and-
    journaled call, extended here to calls that were never decided, never
    executed, or refused, which otherwise get no hindsight check at all: no
    journal entry exists for an undecided or refused recommendation (see
    journal.record_entry(), only ever called at decision time or by a
    successful auto-apply submission).

    `would_have_helped` is deliberately neutral, not "should have traded": a
    refusal or non-decision whose ticker then moved the wrong way was the
    right outcome in hindsight, same as the earlier chat counterfactual
    analysis of 2026-09's stale-recommendation refusals found (a modest,
    non-uniform result — not every blocked trade would have been a win).
    This reports both directions, not just the cases that make skipping the
    trade look like a mistake. A candidate with no reference_price or no
    live quote available is excluded rather than guessed at, same
    "exclude, don't fake" convention as every scoring component here.
    """
    seen_tickers: set[str] = set()
    deduped = []
    for rec in sorted(candidates, key=lambda r: r.get("confidence") or 0, reverse=True):
        ticker = rec.get("ticker")
        if not ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        deduped.append(rec)
        if len(deduped) >= MAX_MISSED_OPPORTUNITY_LOOKUPS:
            break

    from trading_agent.journal import _reference_price as _current_quote

    results = []
    for rec in deduped:
        reference = rec.get("reference_price")
        if not reference:
            continue
        current = _current_quote(rec["ticker"])
        if not current:
            continue
        pct_change = round((current - reference) / reference * 100, 2)
        action = rec.get("action")
        would_have_helped = (action == "BUY" and pct_change > 0) or (action == "SELL" and pct_change < 0)
        results.append(
            {
                "ticker": rec["ticker"],
                "action": action,
                "confidence": rec.get("confidence"),
                "checkpoint": rec.get("checkpoint"),
                "category": rec.get("_category"),
                "reference_price": reference,
                "current_price": current,
                "pct_change": pct_change,
                "would_have_helped": would_have_helped,
            }
        )
    return results


def _config_tuning_notes(
    attempts: list[dict[str, Any]],
    refusal_breakdown: dict[str, int],
    never_decided: list[dict[str, Any]],
    approved_not_executed: list[dict[str, Any]],
    auto_outcome_unlogged: list[dict[str, Any]],
    attempt_log_days: list[str],
    window_days: list[str],
) -> list[str]:
    """Qualitative, data-grounded observations for human review — never a
    config change applied by this function. Every note cites an actual count
    from the review's own lookback window, and says so when a count only
    covers the part of it that has attempt logs.
    """
    notes = []
    lookback_days = len(window_days)
    attempt_span = _attempt_log_span(attempt_log_days, window_days)
    total_refused = sum(1 for a in attempts if a.get("status") == "refused")
    stale_refused = refusal_breakdown.get("stale: price drift", 0) + refusal_breakdown.get(
        "stale: technical reversal", 0
    )
    if total_refused and stale_refused / total_refused >= 0.3:
        drift_pct = load_risk_limits().get("execution", {}).get("max_price_drift_pct")
        notes.append(
            f"{stale_refused} of {total_refused} logged auto-apply refusals "
            f"({stale_refused / total_refused:.0%}; logs cover {attempt_span}) were "
            "stale-recommendation blocks (price drift or "
            f"technical reversal) — current execution.max_price_drift_pct is {drift_pct}%. Consider "
            "whether that's too tight for these tickers' volatility, or whether they need a liquidity "
            "floor (position.candidate_min_price) like the one already applied to movers. Not applied "
            "here — a deliberate config edit, same as every other proposal in this review."
        )

    skipped = sum(1 for a in attempts if a.get("status") == "skipped")
    if skipped:
        notes.append(
            f"{skipped} logged auto-apply candidate(s) ({attempt_span}) were skipped with qty "
            "rounding to 0 — usually a low-priced ticker whose suggested_size_pct_of_portfolio "
            "is too small to buy even one share at current equity. No action needed unless this keeps "
            "recurring on the same tickers."
        )

    if never_decided:
        tickers = sorted({r["ticker"] for r in never_decided})
        shown = ", ".join(tickers[:10]) + ("..." if len(tickers) > 10 else "")
        notes.append(
            f"{len(never_decided)} actionable recommendation(s) over the last {lookback_days} days "
            f"expired with no human decision ever recorded ({shown}) — these never reached "
            "data/journal/ either, so the improvement loop never learned anything from them. Consider "
            "whether notify_digest()'s immediate email is being seen, or whether "
            "operational.approval_expiry_hours is too short."
        )

    if approved_not_executed:
        tickers = sorted({r["ticker"] for r in approved_not_executed})
        shown = ", ".join(tickers[:10]) + ("..." if len(tickers) > 10 else "")
        notes.append(
            f"{len(approved_not_executed)} recommendation(s) over the last {lookback_days} days were "
            f"approved but never submitted via `trading-agent execute` ({shown}) — approval and "
            "execution are deliberately separate steps (see .claude/skills/trading-trade), but an "
            "approval nobody follows up on is a lost opportunity either way, not a guardrail worth tuning."
        )

    if auto_outcome_unlogged:
        notes.append(
            f"{len(auto_outcome_unlogged)} auto-apply approval(s) over the last {lookback_days} days have "
            "no trade and no recorded outcome — they predate the per-attempt log, so why each one "
            "didn't go through (a guardrail refusal or an error) wasn't captured. This gap closes on "
            "its own as those days age out of the window."
        )

    return notes


def _attempt_log_coverage(attempt_log_days: list[str], window_days: list[str]) -> int:
    """How many days of the window the auto-apply attempt log covers. Logging
    started partway through history, so coverage runs from the first logged
    day to the window's end — after that point a day with no log file simply
    had no attempts."""
    if not attempt_log_days:
        return 0
    return sum(1 for day in window_days if day >= attempt_log_days[0])


def _attempt_log_span(attempt_log_days: list[str], window_days: list[str]) -> str:
    """Plain-language form of _attempt_log_coverage(), e.g. "3 of the last
    30 days, since 2026-09-30"."""
    covered = _attempt_log_coverage(attempt_log_days, window_days)
    if covered == 0:
        return f"no attempt logs in the last {len(window_days)} days"
    if covered == len(window_days):
        return f"all {len(window_days)} days"
    return f"{covered} of the last {len(window_days)} days, since {attempt_log_days[0]}"


def _write_learning_review_markdown(review: dict[str, Any]) -> Path:
    lines = [
        f"# Weekly Learning Review — last {review['lookback_days']} days "
        f"({review['window_start']} to {review['window_end']})",
        "",
        "## Trades over the lookback window",
        f"- Trades executed: {review['trades_count']}",
        f"- Realized P&L: ${review['realized_pnl']['total']:,.2f}",
    ]
    if review["realized_pnl"]["pending_fills"]:
        lines.append(f"  - {review['realized_pnl']['pending_fills']} sell order(s) not filled yet")
    if review["unrealized_pnl"]["error"]:
        lines.append(f"- Unrealized (live positions): unavailable — {review['unrealized_pnl']['error']}")
    else:
        lines.append(f"- Unrealized (live, open positions right now): ${review['unrealized_pnl']['total']:,.2f}")

    lines += [
        "",
        "## Sell orders executed — net result",
        *sell_result_lines(review["sell_results"], f"over the last {review['lookback_days']} days"),
    ]

    lines += ["", "## Portfolio status (live snapshot)"]
    status = review["portfolio_status"]
    if status["error"]:
        lines.append(f"_Unavailable — {status['error']}_")
    else:
        lines.append(
            f"- Equity: ${status['equity']:,.2f} | Cash: ${status['cash']:,.2f} | "
            f"Buying power: ${status['buying_power']:,.2f}"
        )
        lines.append(f"- Open positions: {len(review['unrealized_pnl']['positions'])}")

    lines += [
        "",
        "## Orders not placed over the lookback window",
        f"- Actionable recommendations: {review['actionable_count']} | Executed: {review['executed_count']}",
        f"- Never decided (expired, no approve/reject ever recorded): {len(review['never_decided'])}",
        f"- Human-approved but never submitted: {len(review['approved_not_executed'])}",
        f"- Auto-apply approved, outcome not logged (predates the attempt log): "
        f"{len(review['auto_outcome_unlogged'])}",
        f"- Human-rejected: {len(review['rejected'])}",
        f"- Auto-apply attempts by status ({review['attempt_log_span']}): "
        f"{review['auto_apply_attempts_by_status']}",
    ]
    if review["refusal_breakdown"]:
        lines.append(f"- Refusal breakdown ({review['attempt_log_span']}):")
        for label, count in sorted(review["refusal_breakdown"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  - {label}: {count}")

    lines += ["", "## Opportunities lost or avoided (hindsight check)"]
    if review["missed_opportunities"]:
        for m in review["missed_opportunities"]:
            verdict = "would have helped" if m["would_have_helped"] else "would NOT have helped"
            lines.append(
                f"- {m['ticker']} {m['action']} [{m['category']}] confidence {m['confidence']}: "
                f"{m['reference_price']:.2f} -> {m['current_price']:.2f} ({m['pct_change']:+.2f}%) — {verdict}"
            )
    else:
        lines.append("_No unexecuted actionable recommendations with a usable reference price over this window._")

    lines += ["", "## Signal performance so far"]
    if review["signal_hit_rates"]:
        for sig, stats in sorted(review["signal_hit_rates"].items()):
            lines.append(f"- {sig}: {stats.get('hit_rate', 0):.0%} over {stats.get('n', 0)} call(s)")
    else:
        lines.append("_Not enough outcome history yet._")

    lines += ["", "## Proposed model/config improvements (never applied automatically)"]
    for p in review["optimization_proposals"]:
        lines.append(f"- {p}")
    if review["learning_outcome"]:
        lines.append(f"- Possible outcome if incorporated: {review['learning_outcome']}")
    weight_str = ", ".join(f"{k} {v:g}" for k, v in review["scoring_weights"].items())
    lines.append(f"- Weights currently in effect: {weight_str or 'n/a'}")

    lines += ["", "## Optimization options (apply with one click from the email)"]
    if review.get("optimization_options"):
        for option in review["optimization_options"]:
            diff = ", ".join(f"{c['path']}: {c['from']} -> {c['to']}" for c in option["changes"])
            flag = " **[loosens a guardrail]**" if option.get("loosens_guardrail") else ""
            lines.append(f"- **{option['title']}**{flag} (`{option['id']}`)")
            lines.append(f"  - Change: {diff}")
            lines.append(f"  - Why: {option['why']}")
            lines.append(f"  - Effect: {option['effect']}")
            if option.get("risk"):
                lines.append(f"  - Risk: {option['risk']}")
            ack = " --acknowledge-loosening" if option.get("loosens_guardrail") else ""
            lines.append(f"  - Apply by hand: `trading-agent optimizations apply {option['id']}{ack}`")
    else:
        lines.append("_No option cleared its minimum-evidence bar over this window._")

    if review["config_tuning_notes"]:
        lines += ["", "## Other observations for next week"]
        for n in review["config_tuning_notes"]:
            lines.append(f"- {n}")

    lines += [
        "",
        "## Notes",
        f"This review looks back {review['lookback_days']} rolling calendar days (per user instruction "
        "2026-10-02, \"look broader ... to ensure analysis and predictions are more accurate\") — a "
        "window deliberately wider than the weekly cadence this report fires on, so a single quiet week "
        "doesn't starve the refusal-category and missed-opportunity patterns of data. Distinct from "
        "`trading-agent weekly-report`'s strict calendar-week P&L, which this doesn't replace. Every "
        "option above changes config only after a human clicks Apply (or runs the command); the next "
        "checkpoint routine then applies it within hard-coded bounds and commits it to git, so it can "
        "be reverted. Building this report changes nothing by itself.",
    ]

    out_dir = REPORTS_DIR / "learning"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{review['window_end']}.md"
    path.write_text("\n".join(lines))
    return path


DEFAULT_LEARNING_REVIEW_LOOKBACK_DAYS = 30


def _lookback_dates(as_of: str | None, lookback_days: int) -> list[str]:
    """`lookback_days` calendar days ending at `as_of` (inclusive, default
    today), oldest first — the rolling window build_weekly_learning_review()
    analyzes, as distinct from week_dates()'s fixed Monday-start 7 days used
    by build_weekly_report().
    """
    end = datetime.strptime(as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc) if as_of else datetime.now(timezone.utc)
    return [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(lookback_days - 1, -1, -1)]


def build_weekly_learning_review(as_of: str | None = None, lookback_days: int | None = None) -> dict[str, Any]:
    """Weekly retrospective (per user instruction 2026-10-02, "evaluate all
    trades, portfolio status, trade orders not placed and opportunities
    lost ... feed and optimize for following week runs"; broadened per
    user instruction 2026-10-02, "look broader into last 30 rolling days to
    ensure analysis and predictions are more accurate"): evaluates trades,
    current portfolio status, every actionable recommendation that did NOT
    result in a trade (and why — never decided, approved but never
    submitted, human-rejected, or refused by a guardrail/auto-apply), and a
    hindsight price check on those unexecuted calls, all over a rolling
    `lookback_days`-day window ending at `as_of` (default: today) — not the
    7-day calendar week build_weekly_report() uses. The wider window exists
    specifically so a single light week's small numbers don't make a
    refusal-category or missed-opportunity pattern look more or less
    significant than it really is; this still fires weekly (see
    routines/weekly_learning_review.md), it just looks further back each
    time.

    `lookback_days` defaults to `agent_config.yaml -> reporting.
    weekly_learning_review_lookback_days` (30) when not passed explicitly —
    same "config is the source of truth, a param is for ad hoc overrides"
    convention as every other default in this module.

    Distinct from build_weekly_report() (trade counts + realized/unrealized
    P&L only, strict calendar week) and build_daily_summary() (one day, no
    "what didn't happen" analysis) — this is the only report that looks at
    recommendations with no trade behind them at all. "Feed and optimize for
    following week runs" means exactly what propose_weight_adjustments()
    already means here (plan §6.3): surfaced for a human to read and decide
    on. This function never writes config/agent_config.yaml or
    config/risk_limits.yaml.
    """
    from trading_agent.scoring.recommendation_engine import propose_weight_adjustments

    lookback_days = lookback_days or load_agent_config().get("reporting", {}).get(
        "weekly_learning_review_lookback_days", DEFAULT_LEARNING_REVIEW_LOOKBACK_DAYS
    )
    days = _lookback_dates(as_of, lookback_days)

    recs_by_day: dict[str, list[dict]] = {}
    decisions_by_day: dict[str, list[dict]] = {}
    trades_by_day: dict[str, list[dict]] = {}
    attempts_by_day: dict[str, list[dict]] = {}
    for day in days:
        recs_by_day[day] = _day_recs(day)
        decisions_by_day[day] = _day_decisions(day)
        trades_by_day[day] = _day_trades(day)
        attempts_by_day[day] = _day_auto_apply_attempts(day)

    window_trades = [t for trades in trades_by_day.values() for t in trades]
    window_attempts = [{**a, "_day": day} for day, attempts in attempts_by_day.items() for a in attempts]

    rec_index: dict[tuple[str, str | None, str], dict] = {}
    for day, recs in recs_by_day.items():
        for rec in recs:
            rec_index[(day, rec.get("checkpoint"), rec.get("ticker"))] = rec

    decision_index: dict[tuple[str, str | None, str], dict] = {}
    for day, decisions in decisions_by_day.items():
        for d in decisions:
            decision_index[(day, d.get("checkpoint"), d.get("ticker"))] = d

    executed_keys = set()
    for day, trades in trades_by_day.items():
        for t in trades:
            rec = t.get("recommendation", {})
            executed_keys.add((day, rec.get("checkpoint"), rec.get("ticker")))

    attempted_keys = {(a["_day"], a.get("checkpoint"), a.get("ticker")) for a in window_attempts}

    # A checkpoint re-run on the same day appends a second copy of the same
    # recommendation; count each (day, checkpoint, ticker) once.
    actionable_keys = list(
        dict.fromkeys(
            (day, rec.get("checkpoint"), rec.get("ticker"))
            for day, recs in recs_by_day.items()
            for rec in recs
            if rec.get("action") in ("BUY", "SELL")
        )
    )

    never_decided: list[dict[str, Any]] = []
    approved_not_executed: list[dict[str, Any]] = []
    auto_outcome_unlogged: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for key in actionable_keys:
        if key in executed_keys or key in attempted_keys:
            continue
        rec = rec_index.get(key)
        if rec is None:
            continue
        decision = decision_index.get(key)
        if decision is None:
            never_decided.append({**rec, "_category": "never decided"})
        elif decision.get("decision") != "approve":
            rejected.append({**rec, "_category": "human-rejected"})
        elif (decision.get("terms") or {}).get("source") == "auto":
            # auto_apply() records its approval before submitting, so an auto
            # approval with no trade and no attempt record was refused or
            # errored on a day before _persist_attempt() existed — not a
            # human who forgot to run `trading-agent execute`.
            auto_outcome_unlogged.append({**rec, "_category": "auto-apply, outcome not logged"})
        else:
            approved_not_executed.append({**rec, "_category": "approved, never executed"})

    attempt_log_days = [day for day in days if attempts_by_day[day]]

    refused_candidates: list[dict[str, Any]] = []
    refusal_breakdown: dict[str, int] = {}
    for a in window_attempts:
        if a.get("status") == "capped":
            # Not a guardrail refusal (so not in refusal_breakdown), but still
            # an unexecuted call worth the hindsight check.
            rec = rec_index.get((a["_day"], a.get("checkpoint"), a.get("ticker")))
            if rec is not None:
                refused_candidates.append({**rec, "_category": "auto-capped: daily trade limit"})
            continue
        if a.get("status") != "refused":
            continue
        label = _categorize_refusal(a.get("reason", ""))
        refusal_breakdown[label] = refusal_breakdown.get(label, 0) + 1
        rec = rec_index.get((a["_day"], a.get("checkpoint"), a.get("ticker")))
        if rec is not None:
            refused_candidates.append({**rec, "_category": f"auto-refused: {label}"})

    missed_candidates = never_decided + approved_not_executed + auto_outcome_unlogged + rejected + refused_candidates
    missed_opportunities = _missed_opportunity_counterfactuals(missed_candidates)

    signal_hit_rates = _signal_type_hit_rates()

    sell_results = window_sell_results(days[0], days[-1], window_trades)

    review: dict[str, Any] = {
        "window_start": days[0],
        "window_end": days[-1],
        "lookback_days": lookback_days,
        "trades_count": len(window_trades),
        "realized_pnl": _realized_from_sells(sell_results, window_trades),
        "sell_results": sell_results,
        "unrealized_pnl": _unrealized_pnl(),
        "portfolio_status": _portfolio_status_snapshot(),
        "actionable_count": len(actionable_keys),
        "executed_count": sum(1 for key in actionable_keys if key in executed_keys),
        "never_decided": never_decided,
        "approved_not_executed": approved_not_executed,
        "auto_outcome_unlogged": auto_outcome_unlogged,
        "rejected": rejected,
        "attempt_log_days": attempt_log_days,
        "auto_apply_attempts_by_status": {
            status: sum(1 for a in window_attempts if a.get("status") == status)
            for status in ("submitted", "refused", "skipped", "error", "capped")
        },
        "refusal_breakdown": refusal_breakdown,
        "missed_opportunities": missed_opportunities,
        "signal_hit_rates": signal_hit_rates,
        "optimization_proposals": propose_weight_adjustments(),
        "scoring_weights": load_agent_config().get("scoring_weights", {}),
        "learning_outcome": _learning_outcome_note(signal_hit_rates),
    }
    review["attempt_log_coverage_days"] = _attempt_log_coverage(attempt_log_days, days)
    review["attempt_log_span"] = _attempt_log_span(attempt_log_days, days)

    from trading_agent.optimizations import build_options, save_options

    review["optimization_options"] = build_options(review)
    if review["optimization_options"]:
        save_options(review["optimization_options"], review["window_end"])
    review["config_tuning_notes"] = _config_tuning_notes(
        window_attempts,
        refusal_breakdown,
        never_decided,
        approved_not_executed,
        auto_outcome_unlogged,
        attempt_log_days,
        days,
    )
    review["report_path"] = str(_write_learning_review_markdown(review))
    return review
