"""Business calendar: facility timezone, observed-holiday table, and the
business-day arithmetic ship_date() promises against.

Decision D3 (docs/DECISION_LOG.md): the facility clock is America/New_York
and a business day is Mon-Fri minus the observed-US-federal-holiday table
below. Every function here is pure date/time arithmetic — no I/O, no clock
reads — so the engine that builds on it stays deterministic and testable.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

FACILITY_TZ = ZoneInfo('America/New_York')

# An order timestamped at or after this facility-local time counts as
# received the next business day (D1). SHIP_BY_HOUR is the wall-clock instant
# every promise lands on. Kept as separate constants even though policy makes
# them coincide today — they answer different questions.
RECEIPT_CUTOFF = time(17, 0)
SHIP_BY_HOUR = time(17, 0)

# US federal holidays AS OBSERVED (a holiday falling on a weekend is shifted
# to the adjacent weekday), covering every year the engine may quote into,
# plus the observed 2028 New Year (Fri 2027-12-31). This is a hand-maintained
# table by design (D3): when the horizon below is extended, this table is
# extended with it. ship_date() raises past the horizon rather than guess.
HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day (Thu)
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth (Fri)
    date(2026, 7, 3),    # Independence Day observed (Jul 4 = Sat)
    date(2026, 9, 7),    # Labor Day
    date(2026, 10, 12),  # Columbus Day
    date(2026, 11, 11),  # Veterans Day (Wed)
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas (Fri)
    # 2027
    date(2027, 1, 1),    # New Year's Day (Fri)
    date(2027, 1, 18),   # MLK Day
    date(2027, 2, 15),   # Washington's Birthday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth observed (Jun 19 = Sat)
    date(2027, 7, 5),    # Independence Day observed (Jul 4 = Sun)
    date(2027, 9, 6),    # Labor Day
    date(2027, 10, 11),  # Columbus Day
    date(2027, 11, 11),  # Veterans Day (Thu)
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas observed (Dec 25 = Sat)
    date(2027, 12, 31),  # New Year 2028 observed (Jan 1 = Sat)
})

# Last date the holiday table is authoritative for. A computed date beyond
# this point has no holiday data, so the engine refuses to quote past it.
CALENDAR_HORIZON = date(2027, 12, 31)


def is_business_day(d: date) -> bool:
    """A weekday that is not an observed holiday."""
    return d.weekday() < 5 and d not in HOLIDAYS


class CalendarHorizonError(ValueError):
    """A computed date fell past the holiday table (CALENDAR_HORIZON).

    Raised instead of returning a date the table cannot vouch for: past the
    horizon an undefined-year holiday would be silently counted as a shipping
    day (the R0 #1 defect — an out-of-stock item with a long lead ordered near
    year-end landing on, e.g., a 2028 New Year's Day). Extend HOLIDAYS and
    CALENDAR_HORIZON together before quoting this far out.
    """

    def __init__(self, d: date):
        self.date = d
        super().__init__(
            f'date {d} is past the holiday-table horizon ({CALENDAR_HORIZON}); '
            f'extend HOLIDAYS in calendar.py before quoting this far out'
        )


def next_business_day(d: date) -> date:
    """The first business day strictly after ``d``.

    Raises CalendarHorizonError when that day would fall past the horizon —
    there is no holiday data out there to trust.
    """
    candidate = d + timedelta(days=1)
    while not is_business_day(candidate):
        candidate += timedelta(days=1)
    if candidate > CALENDAR_HORIZON:
        raise CalendarHorizonError(candidate)
    return candidate


def add_business_days(d: date, n: int) -> date:
    """``d`` advanced by ``n`` business days, ``n >= 1``.

    Counting starts from the first business day after ``d`` (``d`` itself need
    not be a business day). Propagates CalendarHorizonError if the walk
    crosses the horizon at any step (R0 #1).
    """
    if n < 1:
        raise ValueError(f'add_business_days requires n >= 1, got {n}')
    result = d
    for _ in range(n):
        result = next_business_day(result)
    return result


def normalize_receipt(received_at: datetime) -> date:
    """The business day an order counts as received (D1).

    Convert to the facility clock; if the local time is at/after the cutoff,
    or the local date is not a business day, roll forward to the next business
    day. A timezone-aware input is required (D3) — a naive timestamp is
    rejected rather than guessed at.
    """
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError(
            'received_at must be timezone-aware (D3: naive timestamps are '
            'rejected, never guessed at)'
        )
    local = received_at.astimezone(FACILITY_TZ)
    day = local.date()
    if local.time() >= RECEIPT_CUTOFF or not is_business_day(day):
        return next_business_day(day)   # raises past the horizon
    if day > CALENDAR_HORIZON:
        raise CalendarHorizonError(day)  # in-window today but already past table
    return day


def ship_by_datetime(d: date) -> datetime:
    """The SHIP_BY_HOUR facility-local promise instant for business day ``d``."""
    return datetime.combine(d, SHIP_BY_HOUR, tzinfo=FACILITY_TZ)
