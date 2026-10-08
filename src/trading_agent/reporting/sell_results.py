"""What each executed SELL actually netted, and the total — the "net increase
or decrease for all sell orders executed" the daily and weekly summaries carry
(per user instruction 2026-10-08).

Source is Alpaca's own filled-order history (data.alpaca_client.
get_filled_orders()), walked oldest first with an average-cost basis per
symbol, the same accounting reporting.report_builder._realized_pnl() uses on
the trade files. It does not read data/trades/ for fills: those records are
written the instant an order is submitted (`pending_new`, no fill price) and
never updated, so they could only ever report "pending". The trade files are
used for one thing here — telling the user how many of the window's own SELLs
haven't filled yet.

"Exclude, don't fake": a SELL whose cost basis can't be established (no
earlier fill in the history for that symbol) is listed but kept out of the
total and counted, and an unreachable Alpaca reports an error, never $0.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_EPS = 1e-9


def _et_day(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")


def per_sell_results(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One result per SELL fill, from `fills` (filled orders, any order) walked
    in fill-time order with an average-cost basis per symbol. A BUY updates the
    running average; a SELL realizes (fill price - average cost) * qty."""
    held: dict[str, tuple[float, float]] = {}  # symbol -> (qty, avg cost)
    results: list[dict[str, Any]] = []
    for order in sorted(fills, key=lambda o: (o.get("filled_at") or "", o.get("id") or "")):
        symbol = order.get("symbol")
        side = str(order.get("side") or "").lower().split(".")[-1]
        qty = float(order.get("filled_qty") or 0)
        price = float(order.get("filled_avg_price") or 0)
        if not symbol or qty <= 0 or price <= 0:
            continue
        held_qty, avg_cost = held.get(symbol, (0.0, 0.0))
        if side == "buy":
            new_qty = held_qty + qty
            held[symbol] = (new_qty, (held_qty * avg_cost + qty * price) / new_qty)
        elif side == "sell":
            known = held_qty + _EPS >= qty and avg_cost > 0
            net = (price - avg_cost) * qty if known else None
            results.append(
                {
                    "order_id": order.get("id"),
                    "ticker": symbol,
                    "qty": qty,
                    "fill_price": price,
                    "avg_cost": round(avg_cost, 6) if known else None,
                    "net_usd": round(net, 2) if known else None,
                    "net_pct": round((price / avg_cost - 1) * 100, 2) if known else None,
                    "filled_at": order.get("filled_at"),
                    "day": _et_day(order["filled_at"]),
                }
            )
            held[symbol] = (max(held_qty - qty, 0.0), avg_cost)
    return results


def summarize(
    fills: list[dict[str, Any]], day_from: str, day_to: str, trades: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """SELLs that filled on an ET calendar day in [day_from, day_to]."""
    sources = {
        (t.get("order") or {}).get("id"): t.get("source", "human") for t in (trades or []) if t.get("order")
    }
    sells = [s for s in per_sell_results(fills) if day_from <= s["day"] <= day_to]
    for sell in sells:
        sell["source"] = sources.get(sell["order_id"])
    known = [s for s in sells if s["net_usd"] is not None]
    cost = sum(s["avg_cost"] * s["qty"] for s in known)
    by_symbol: dict[str, float] = {}
    for sell in known:
        by_symbol[sell["ticker"]] = round(by_symbol.get(sell["ticker"], 0.0) + sell["net_usd"], 2)

    filled_ids = {o.get("id") for o in fills}
    pending = sum(
        1
        for t in (trades or [])
        if str((t.get("order") or {}).get("side") or "").lower().split(".")[-1] == "sell"
        and (t.get("order") or {}).get("id") not in filled_ids
    )
    return {
        "error": None,
        "sells": sells,
        "count": len(sells),
        "total_net_usd": round(sum(s["net_usd"] for s in known), 2),
        "total_net_pct": round(sum(s["net_usd"] for s in known) / cost * 100, 2) if cost else None,
        "by_symbol": by_symbol,
        "unknown_basis": len(sells) - len(known),
        "pending": pending,
    }


def window_sell_results(day_from: str, day_to: str, trades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """summarize() over Alpaca's live fill history. Best-effort: an unreachable
    Alpaca returns {"error": ...} with no total, so a summary says "unavailable"
    instead of presenting a missing number as $0."""
    from trading_agent.data import alpaca_client

    try:
        fills = alpaca_client.get_filled_orders()
    except Exception as exc:  # noqa: BLE001 - a report must still build without it
        return {
            "error": str(exc), "sells": [], "count": 0, "total_net_usd": None, "total_net_pct": None,
            "by_symbol": {}, "unknown_basis": 0, "pending": 0,
        }
    return summarize(fills, day_from, day_to, trades)


def _usd(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _px(value: float) -> str:
    return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"


def sell_result_lines(sr: dict[str, Any], scope: str) -> list[str]:
    """Plain-text lines for the emails and markdown reports. `scope` reads as
    "today" / "this week" / "the last 30 days"."""
    if sr.get("error"):
        return [f"Net result on sell orders: unavailable — {sr['error']}"]
    if not sr["sells"]:
        lines = [f"No sell orders executed {scope}."]
    else:
        total = sr["total_net_usd"]
        kind = "net increase" if total > 0 else ("net decrease" if total < 0 else "no net change")
        pct = f" ({sr['total_net_pct']:+.2f}% on cost)" if sr["total_net_pct"] is not None else ""
        lines = [f"Net result on {sr['count']} sell order(s) executed {scope}: {_usd(total)}{pct} — {kind}"]
        for s in sr["sells"]:
            origin = f" [{s['source']}]" if s.get("source") else ""
            if s["net_usd"] is None:
                lines.append(
                    f"- {s['ticker']}: sold {s['qty']:g} @ {_px(s['fill_price'])} — cost basis unavailable, not counted{origin}"
                )
            else:
                lines.append(
                    f"- {s['ticker']}: sold {s['qty']:g} @ {_px(s['fill_price'])} vs avg cost {_px(s['avg_cost'])}"
                    f" → {_usd(s['net_usd'])} ({s['net_pct']:+.1f}%){origin}"
                )
    if sr["unknown_basis"]:
        lines.append(f"  ({sr['unknown_basis']} sell(s) without a known cost basis are not in the total.)")
    if sr["pending"]:
        lines.append(f"  ({sr['pending']} sell order(s) not filled yet are not included.)")
    return lines
