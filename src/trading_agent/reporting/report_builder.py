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
