"""Daily/weekly markdown report generation (plan §10).

Reports are generated as files and are meant to be committed alongside the
day's/week's data snapshot (plan §7) so the report and the data behind it
stay linked in git history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading_agent.config import APPROVALS_DIR, RECOMMENDATIONS_DIR, REPORTS_DIR, TRADES_DIR
from trading_agent.utils import load_json_list, today


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
    recent Monday) into a weekly rollup. Win-rate/Sharpe-style performance
    metrics need realized-outcome tracking (plan §6.3/§11 phase 8) which
    isn't built yet — this reports activity counts, not P&L, until then.
    """
    if week_start:
        start = datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=now.weekday())

    days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    total_recs = total_approved = total_rejected = total_expired = total_trades = 0
    per_day_lines = []
    for day in days:
        recs = _day_recs(day)
        decisions = {d["ticker"]: d["decision"] for d in _day_decisions(day)}
        trades = _day_trades(day)
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

    lines = [
        f"# Weekly Report — week of {days[0]}",
        "",
        "## Summary",
        f"- Total recommendations: {total_recs}",
        f"- Approved: {total_approved} | Rejected: {total_rejected} | Expired: {total_expired}",
        f"- Trades executed (paper): {total_trades}",
        "",
        "## Daily breakdown",
        *(per_day_lines or ["_No activity this week._"]),
        "",
        "## Proposed model/config improvements",
        "_Pending your review — see `trading-agent propose-weights`._",
        "",
        "## Notes",
        "P&L/win-rate metrics require realized-outcome tracking, which is not "
        "yet built (plan §11 phase 8) — see CLAUDE.md.",
    ]

    out_dir = REPORTS_DIR / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    iso = datetime.strptime(days[0], "%Y-%m-%d").isocalendar()
    path = out_dir / f"{iso.year}-W{iso.week:02d}.md"
    path.write_text("\n".join(lines))
    return path
