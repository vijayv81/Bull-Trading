"""Entry/exit timing check: is now a good price to act, or better to wait?

Per user instruction 2026-10-10: before deciding to BUY or SELL, look at the
last week and the last month of prices and decide whether the price is right
or whether waiting would plausibly get a better one (more profit on a buy,
less loss on a sell).

Deliberately simple and explainable — a mean-reversion check, not a forecast:

- BUY: wait when the price sits in the top of its 1-month range *and* is at
  least `min_improvement_pct` above its 1-week average (the pullback target).
- SELL: wait when the price sits in the bottom of its 1-month range *and* is
  at least `min_improvement_pct` below its 1-week average (selling at the low
  of the range, with a bounce to the weekly average on the table).
- A stop-loss exit is never deferred (`defer_stop_loss_exits: false`): the
  point of a stop-loss is not to wait, and "it might bounce" is exactly the
  reasoning it exists to override. Take-profit exits are not deferred either
  unless they fit the SELL rule above, which a price at the top of its range
  never does.

Returns None when there aren't enough bars — "exclude, don't fake", same as
every other signal here — and the caller treats None as "no objection".
"""

from __future__ import annotations

from typing import Any

import pandas as pd

WEEK_BARS = 5
MONTH_BARS = 21  # ~1 calendar month of trading days

DEFAULTS = {
    "enabled": True,
    "min_improvement_pct": 3.0,
    "buy_wait_above_range_pct": 80.0,
    "sell_wait_below_range_pct": 20.0,
    "defer_stop_loss_exits": False,
}


def timing_check(
    action: str,
    price: float | None,
    bars: pd.DataFrame,
    *,
    stop_loss_hit: bool = False,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    cfg = {**DEFAULTS, **(cfg or {})}
    if not cfg["enabled"] or action not in ("BUY", "SELL") or not price or price <= 0:
        return None
    if bars is None or len(bars) < MONTH_BARS or "close" not in bars:
        return None

    week = bars.tail(WEEK_BARS)
    month = bars.tail(MONTH_BARS)
    week_avg = float(week["close"].mean())
    month_avg = float(month["close"].mean())
    high_col = "high" if "high" in bars else "close"
    low_col = "low" if "low" in bars else "close"
    month_low = min(float(month[low_col].min()), price)
    month_high = max(float(month[high_col].max()), price)
    week_low = min(float(week[low_col].min()), price)
    week_high = max(float(week[high_col].max()), price)
    if month_high <= month_low:
        return None
    position = (price - month_low) / (month_high - month_low) * 100

    out: dict[str, Any] = {
        "verdict": "proceed",
        "price": round(price, 4),
        "week_avg": round(week_avg, 4),
        "week_low": round(week_low, 4),
        "week_high": round(week_high, 4),
        "month_avg": round(month_avg, 4),
        "month_low": round(month_low, 4),
        "month_high": round(month_high, 4),
        "pct_of_month_range": round(position, 1),
        "target_price": None,
        "potential_improvement_pct": 0.0,
    }
    where = f"{position:.0f}% of the way up its 1-month range (${month_low:.2f}-${month_high:.2f})"

    if action == "BUY":
        gain = (price - week_avg) / price * 100  # how much cheaper the weekly average is
        if gain >= cfg["min_improvement_pct"] and position >= cfg["buy_wait_above_range_pct"]:
            out.update(
                verdict="wait",
                target_price=round(week_avg, 4),
                potential_improvement_pct=round(gain, 1),
                reason=(
                    f"Price ${price:.2f} is {where} and {gain:.1f}% above its 1-week average "
                    f"${week_avg:.2f} — waiting for a pullback toward ${week_avg:.2f} would buy it cheaper."
                ),
            )
        else:
            out["reason"] = f"Price ${price:.2f} is {where}; no clear cheaper entry in the last week/month."
    else:
        gain = (week_avg - price) / price * 100  # how much more the weekly average would fetch
        if stop_loss_hit and not cfg["defer_stop_loss_exits"]:
            out["reason"] = "Stop-loss exit — not deferred."
        elif gain >= cfg["min_improvement_pct"] and position <= cfg["sell_wait_below_range_pct"]:
            out.update(
                verdict="wait",
                target_price=round(week_avg, 4),
                potential_improvement_pct=round(gain, 1),
                reason=(
                    f"Price ${price:.2f} is {where} and {gain:.1f}% below its 1-week average "
                    f"${week_avg:.2f} — selling at the low of the range; waiting for a bounce toward "
                    f"${week_avg:.2f} would lose less."
                ),
            )
        else:
            out["reason"] = f"Price ${price:.2f} is {where}; no clear better exit in the last week/month."
    return out
