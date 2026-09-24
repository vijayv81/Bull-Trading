"""DST-aware UTC cron scheduling for the four daily checkpoints (routines/trading_checkpoints.md).

The checkpoint times are fixed in US/Eastern local time, but the scheduled-routine
platform (`create_trigger` / `update_trigger`) only accepts a UTC cron expression —
there is no timezone field. Converting through `zoneinfo` (the IANA tz database)
rather than hand-coding a fixed UTC offset keeps the computed cron correct across
the twice-yearly DST transition without a maintained calendar of transition dates.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

CHECKPOINT_LOCAL_TIMES: dict[str, dt.time] = {
    "pre_open": dt.time(8, 45),
    "market_open": dt.time(9, 40),
    "midday": dt.time(12, 30),
    "pre_close": dt.time(15, 45),
}

WEEKDAYS_CRON_FIELD = "1-5"


def checkpoint_cron_utc(checkpoint: str, on: dt.date | None = None) -> str:
    """UTC 5-field weekday cron expression for `checkpoint`'s Eastern time on `on` (default: today)."""
    try:
        local_time = CHECKPOINT_LOCAL_TIMES[checkpoint]
    except KeyError:
        raise ValueError(f"unknown checkpoint: {checkpoint!r}") from None
    on = on or dt.datetime.now(EASTERN).date()
    local_dt = dt.datetime.combine(on, local_time, tzinfo=EASTERN)
    utc_dt = local_dt.astimezone(dt.timezone.utc)
    return f"{utc_dt.minute} {utc_dt.hour} * * {WEEKDAYS_CRON_FIELD}"


def all_checkpoint_crons_utc(on: dt.date | None = None) -> dict[str, str]:
    """UTC cron expressions for all four checkpoints, evaluated on the same date."""
    return {name: checkpoint_cron_utc(name, on) for name in CHECKPOINT_LOCAL_TIMES}


def is_dst_active(on: dt.date | None = None) -> bool:
    """Whether US Eastern DST (EDT, UTC-4) is in effect on `on` (default: today); False means EST (UTC-5)."""
    on = on or dt.datetime.now(EASTERN).date()
    probe = dt.datetime.combine(on, dt.time(12, 0), tzinfo=EASTERN)
    return probe.dst() != dt.timedelta(0)
