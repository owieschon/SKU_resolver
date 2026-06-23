"""Property sweep: ship_date() is TOTAL over the valid domain.

Every catalog SKU's inventory record x boundary timestamps, plus a seeded
random sample of (sku, timestamp, qty) triples. Zero undefined results: every
call returns a ShipDateResult whose promise is tz-aware, at 17:00 facility
wall clock, on a business day, strictly after receipt — or raises the
documented ValueError for invalid input. No exceptions of any other kind.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fulfillment import load_inventory, ship_date
from fulfillment.calendar import HOLIDAYS, is_business_day

NY = ZoneInfo('America/New_York')
REPO = Path(__file__).resolve().parent.parent
SEED = 20260606

BOUNDARY_TIMES = [
    datetime(2026, 6, 8, 16, 59, tzinfo=NY),    # just before cutoff
    datetime(2026, 6, 8, 17, 0, tzinfo=NY),     # exact cutoff
    datetime(2026, 6, 12, 23, 59, tzinfo=NY),   # Friday last minute
    datetime(2026, 6, 13, 0, 0, tzinfo=NY),     # Saturday midnight
    datetime(2026, 11, 25, 18, 0, tzinfo=NY),   # holiday eve, after cutoff
    datetime(2026, 12, 31, 16, 59, tzinfo=NY),  # year-end before cutoff
    datetime(2026, 3, 7, 12, 0, tzinfo=NY),     # DST spring-forward weekend
    datetime(2026, 11, 1, 1, 30, tzinfo=NY),    # DST fall-back ambiguous hour
]


def _inventory():
    return load_inventory(REPO / 'data' / 'inventory.json')


from fulfillment import CALENDAR_HORIZON, CalendarHorizonError


def _assert_valid(res, received_at):
    assert res.ship_by.tzinfo is not None
    local = res.ship_by.astimezone(NY)
    assert (local.hour, local.minute) == (17, 0), res
    assert is_business_day(local.date()), res
    assert res.ship_by > received_at, res
    assert res.basis, res
    # R0 #1: a returned date can never be past the holiday table — past the
    # horizon the engine must raise, not return a silently-miscounted date.
    assert local.date() <= CALENDAR_HORIZON, res


def test_every_sku_loads_into_a_valid_record():
    inv = _inventory()
    # count derived at runtime; record consistency enforced in __post_init__
    assert len(inv) > 9000
    in_stock = sum(1 for r in inv.values() if r.qty_on_hand > 0)
    assert 0.80 < in_stock / len(inv) < 0.90  # D4 target band


def test_boundary_timestamps_total_over_all_skus():
    inv = _inventory()
    for ts in BOUNDARY_TIMES:
        for rec in inv.values():
            _assert_valid(ship_date(rec, 1, ts), ts)


def test_seeded_random_sweep():
    from fulfillment import CalendarHorizonError
    inv = _inventory()
    rng = random.Random(SEED)
    skus = sorted(inv)
    base = datetime(2026, 6, 6, tzinfo=NY)
    span_minutes = 60 * 24 * 540   # window to mid-2027
    for _ in range(2000):
        rec = inv[rng.choice(skus)]
        ts = base + timedelta(minutes=rng.randint(0, span_minutes))
        qty = rng.randint(1, 200)
        try:
            _assert_valid(ship_date(rec, qty, ts), ts)
        except CalendarHorizonError:
            pass  # correct near the edge — a loud raise, never a wrong date


def test_horizon_guard_is_live_not_dormant():
    """Deterministic R0 #1 liveness: an OOS record with a 30-BD lead, ordered
    on the last business week the table covers, must raise — proving the
    guard fires rather than returning a silently-miscounted 2028 date."""
    from fulfillment import InventoryRecord
    oos = InventoryRecord(sku='PROBE', qty_on_hand=0, lead_time_days=30)
    near_edge = datetime(2027, 12, 1, 10, tzinfo=NY)
    try:
        ship_date(oos, 1, near_edge)
        assert False, 'expected CalendarHorizonError; got a date past the table'
    except CalendarHorizonError as e:
        assert e.date.year == 2028   # the would-be-wrong date, caught


def test_holiday_table_is_internally_consistent():
    # Every holiday is a weekday (observed dates, by design) and the
    # table covers both calendar years the engine quotes into.
    for h in HOLIDAYS:
        assert h.weekday() < 5, f'{h} is a weekend day; observed table broken'
    years = {h.year for h in HOLIDAYS}
    assert {2026, 2027} <= years
