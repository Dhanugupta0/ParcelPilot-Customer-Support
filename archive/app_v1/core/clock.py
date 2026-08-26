"""Time is the single most error-prone axis in this dataset.

Three things make it hard, and all three are deliberate traps in the data pack:

1. The reference time is NOT wall-clock time. The workbook README pins the
   dataset snapshot to 2026-08-16 11:00 IST. Every "is this breached?",
   "how late was the pickup?" and "was it within 30 minutes?" must be measured
   against that instant.

2. 2026-08-16 is a SUNDAY. Targets expressed in *business* hours therefore have
   not started counting, while targets marked 24x7 have been running all along.
   A naive wall-clock implementation reports the wrong tickets as breached.

3. The pack mixes units in the same table. Support Policy v3 writes Enterprise
   P1 as "30 minutes, 24x7" but Enterprise P2 as plain "2 hours", and Growth P1
   as "2 business hours". That contrast is meaningful, so we honour it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

from app import config

# A "business day" as a duration. ASSUMPTION: "1 business day" is treated as one
# full working day of business time (09:00-18:00 = 9h), added on the business
# clock. So a Sunday ticket with a 2-business-day target is due end of Tuesday.
BUSINESS_DAY_MINUTES = (
    (config.BUSINESS_DAY_END[0] * 60 + config.BUSINESS_DAY_END[1])
    - (config.BUSINESS_DAY_START[0] * 60 + config.BUSINESS_DAY_START[1])
)


class ClockType(str, Enum):
    """Which clock an SLA target runs on."""

    CONTINUOUS = "24x7"       # wall-clock; runs through nights and weekends
    BUSINESS = "business"     # only advances inside business hours


# --------------------------------------------------------------------------
# Snapshot clock
# --------------------------------------------------------------------------

_SNAPSHOT: datetime | None = None


def set_snapshot(dt: datetime) -> None:
    global _SNAPSHOT
    _SNAPSHOT = ensure_tz(dt)


def now() -> datetime:
    """The reference 'now' for the whole system -- the dataset snapshot."""
    if _SNAPSHOT is None:
        return ensure_tz(datetime.fromisoformat(config.SNAPSHOT_FALLBACK))
    return _SNAPSHOT


def ensure_tz(dt: datetime) -> datetime:
    """Attach the dataset timezone to naive datetimes; never silently shift."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=config.TIMEZONE)
    return dt.astimezone(config.TIMEZONE)


def parse_dt(value) -> datetime | None:
    """Tolerant parser for the mixed datetime representations in the workbook."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_tz(value)
    if isinstance(value, date):
        return ensure_tz(datetime.combine(value, time(0, 0)))
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return ensure_tz(datetime.strptime(text, fmt))
        except ValueError:
            continue
    try:
        return ensure_tz(datetime.fromisoformat(text))
    except ValueError:
        return None


def fmt(dt: datetime | None) -> str | None:
    return None if dt is None else ensure_tz(dt).strftime("%Y-%m-%d %H:%M %Z")


# --------------------------------------------------------------------------
# Business-hours calendar
# --------------------------------------------------------------------------

def _is_business_day(d: date) -> bool:
    return d.weekday() in config.BUSINESS_DAYS and d.isoformat() not in config.HOLIDAYS


def _window(d: date) -> tuple[datetime, datetime]:
    """The (open, close) business window for a given date."""
    open_ = ensure_tz(datetime.combine(d, time(*config.BUSINESS_DAY_START)))
    close = ensure_tz(datetime.combine(d, time(*config.BUSINESS_DAY_END)))
    return open_, close


def is_within_business_hours(dt: datetime) -> bool:
    dt = ensure_tz(dt)
    if not _is_business_day(dt.date()):
        return False
    open_, close = _window(dt.date())
    return open_ <= dt < close


def next_business_open(dt: datetime) -> datetime:
    """The first instant at or after `dt` when the business clock is running."""
    dt = ensure_tz(dt)
    cursor = dt
    for _ in range(400):  # generous bound; also guards against a bad holiday set
        if _is_business_day(cursor.date()):
            open_, close = _window(cursor.date())
            if cursor < open_:
                return open_
            if cursor < close:
                return cursor
        cursor = ensure_tz(datetime.combine(cursor.date() + timedelta(days=1),
                                            time(*config.BUSINESS_DAY_START)))
    raise RuntimeError("No business day found within 400 days")


def business_minutes_between(start: datetime, end: datetime) -> float:
    """Business minutes elapsed between two instants. Zero if end <= start."""
    start, end = ensure_tz(start), ensure_tz(end)
    if end <= start:
        return 0.0
    total = 0.0
    cursor = start.date()
    while cursor <= end.date():
        if _is_business_day(cursor):
            open_, close = _window(cursor)
            lo, hi = max(start, open_), min(end, close)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60.0
        cursor += timedelta(days=1)
    return total


def add_business_minutes(start: datetime, minutes: float) -> datetime:
    """Advance `minutes` of business time from `start`, skipping closed periods."""
    cursor = next_business_open(ensure_tz(start))
    remaining = float(minutes)
    for _ in range(400):
        _, close = _window(cursor.date())
        available = (close - cursor).total_seconds() / 60.0
        if remaining <= available:
            return cursor + timedelta(minutes=remaining)
        remaining -= available
        cursor = next_business_open(
            ensure_tz(datetime.combine(cursor.date() + timedelta(days=1), time(0, 0)))
        )
    raise RuntimeError("SLA deadline did not resolve within 400 business days")


# --------------------------------------------------------------------------
# SLA targets
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SLATarget:
    """A parsed first-response target, e.g. '15 minutes, 24x7'."""

    raw: str
    minutes: float
    clock: ClockType

    def deadline_from(self, start: datetime) -> datetime:
        if self.clock is ClockType.CONTINUOUS:
            return ensure_tz(start) + timedelta(minutes=self.minutes)
        return add_business_minutes(start, self.minutes)

    def elapsed_since(self, start: datetime, ref: datetime | None = None) -> float:
        ref = ref or now()
        if self.clock is ClockType.CONTINUOUS:
            return max(0.0, (ensure_tz(ref) - ensure_tz(start)).total_seconds() / 60.0)
        return business_minutes_between(start, ref)

    def describe(self) -> str:
        return f"{self.raw} ({'continuous' if self.clock is ClockType.CONTINUOUS else 'business hours only'})"


_UNIT_MINUTES = {
    "minute": 1.0,
    "hour": 60.0,
    "day": float(BUSINESS_DAY_MINUTES),
}


def parse_sla_target(text: str) -> SLATarget:
    """Parse the target strings used across the policy and the two agreements.

    Handles: '15 minutes, 24x7' | '30 minutes, 24x7' | '2 hours' |
             '2 business hours' | '1 business day' | '2 business days'

    Unit choice is load-bearing. Support Policy v3 writes Enterprise P1 as
    '30 minutes, 24x7' and Growth P1 as '2 business hours' in the same table,
    so the presence or absence of the word 'business' is treated as intentional:
    plain hours/minutes run continuously, 'business' units run on the work
    calendar. '24x7' forces continuous regardless.
    """
    raw = (text or "").strip()
    low = raw.lower()

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:business\s+)?(minute|hour|day)", low)
    if not m:
        raise ValueError(f"Unparseable SLA target: {text!r}")
    qty, unit = float(m.group(1)), m.group(2)

    is_business = "business" in low
    forced_continuous = "24x7" in low or "24/7" in low
    clock = ClockType.CONTINUOUS if (forced_continuous or not is_business) else ClockType.BUSINESS

    # 'N days' is only ever meaningful on the business calendar in this pack.
    if unit == "day":
        clock = ClockType.BUSINESS if not forced_continuous else ClockType.CONTINUOUS

    return SLATarget(raw=raw, minutes=qty * _UNIT_MINUTES[unit], clock=clock)


def humanise_minutes(minutes: float) -> str:
    minutes = abs(float(minutes))
    if minutes < 60:
        return f"{minutes:.0f} min"
    if minutes < BUSINESS_DAY_MINUTES * 2:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / BUSINESS_DAY_MINUTES:.1f} business days"
