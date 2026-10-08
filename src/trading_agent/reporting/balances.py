"""Opening balance, closing balance and the net difference — the first thing
the daily and weekly summaries say (per user instruction 2026-10-08).

"Balance" is the account's equity (cash + positions at market), the same
measure the daily-loss guardrail and the daily summary's portfolio return use.
Opening is the equity at the close of the last session before the window;
closing is the equity at the window's last close, or the live equity when the
window includes today (labelled "as of HH:MM ET", since a summary sent before
the 4:00pm close isn't final). Sources are Alpaca's own daily equity history
and account, never reconstructed from this project's trade files.

"Exclude, don't fake": an unreachable Alpaca reports an error, and a window
that starts before the account existed opens at the account's first recorded
balance, and says so, rather than inventing a starting figure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _error(reason: str) -> dict[str, Any]:
    return {
        "error": reason, "opening": None, "opening_note": None, "closing": None, "closing_note": None,
        "net_usd": None, "net_pct": None,
    }


def window_balances(day_from: str, day_to: str, now: datetime | None = None) -> dict[str, Any]:
    from trading_agent.data import alpaca_client

    now = (now or datetime.now(ET)).astimezone(ET)
    try:
        history = alpaca_client.get_equity_by_close()
    except Exception as exc:  # noqa: BLE001 - a summary must still build without it
        return _error(str(exc))

    before = [d for d in history if d < day_from]
    if before:
        opening_day = max(before)
        opening_note = f"close of {opening_day}"
    elif history:
        opening_day = min(history)
        opening_note = f"the account's first recorded balance, {opening_day}"
    else:
        return _error("no equity history available")
    opening = history[opening_day]

    if day_to >= now.strftime("%Y-%m-%d"):
        try:
            closing = float(alpaca_client.get_account()["equity"])
        except Exception as exc:  # noqa: BLE001
            result = _error(str(exc))
            result.update(opening=round(opening, 2), opening_note=opening_note)
            return result
        closing_note = f"as of {now:%H:%M} ET"
    else:
        closed = [d for d in history if d <= day_to]
        if not closed:
            result = _error(f"no closing balance recorded on or before {day_to}")
            result.update(opening=round(opening, 2), opening_note=opening_note)
            return result
        closing_day = max(closed)
        closing = history[closing_day]
        closing_note = f"close of {closing_day}"

    net = closing - opening
    return {
        "error": None,
        "opening": round(opening, 2), "opening_note": opening_note,
        "closing": round(closing, 2), "closing_note": closing_note,
        "net_usd": round(net, 2), "net_pct": round(net / opening * 100, 2) if opening else None,
    }


def _usd(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def balance_lines(b: dict[str, Any]) -> list[str]:
    """Plain-text lines (emails; the markdown reports prefix them with "- ")."""
    if b.get("error") and b.get("opening") is None:
        return [f"Account balance: unavailable — {b['error']}"]
    lines = [f"Opening balance: ${b['opening']:,.2f} ({b['opening_note']})"]
    if b.get("error"):
        lines.append(f"Closing balance: unavailable — {b['error']}")
        return lines
    lines.append(f"Closing balance: ${b['closing']:,.2f} ({b['closing_note']})")
    kind = "net increase" if b["net_usd"] > 0 else ("net decrease" if b["net_usd"] < 0 else "no net change")
    pct = f" ({b['net_pct']:+.2f}%)" if b["net_pct"] is not None else ""
    lines.append(f"Net difference: {_usd(b['net_usd'])}{pct} — {kind}")
    return lines
