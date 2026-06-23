"""Golden table for ship_date() — THIS TABLE IS THE BUSINESS-RULE SPEC.

Each case is named, dated against the real 2026-27 calendar, and traces to a
decision-log entry (docs/DECISION_LOG.md D1-D4). Reviewed cases, not
generated ones: if a rule change breaks one of these, the spec changed and
the decision log must say why.

Calendar facts the cases rely on (verified against the holiday table):
  2026-06-09 Tue / 2026-06-12 Fri are plain business days
  2026-11-26 Thu = Thanksgiving; 2026-11-27 Fri is a business day
  2026-12-31 Thu; 2027-01-01 Fri = New Year observed -> next BD 2027-01-04
  2026-03-08 Sun = DST spring-forward in America/New_York
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from fulfillment import (
    InventoryRecord, PartialPolicy, ship_date,
)

NY = ZoneInfo('America/New_York')

IN_STOCK = InventoryRecord(sku='K5-24SBC', qty_on_hand=40, lead_time_days=None)
OUT_OF_STOCK = InventoryRecord(sku='M2-8-36-CHR', qty_on_hand=0, lead_time_days=5)
THIN_STOCK = InventoryRecord(sku='VB-5C', qty_on_hand=4, lead_time_days=None)


def _ny(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def _expect(res, y, m, d):
    assert res.ship_by == _ny(y, m, d, 17), (
        f'{res.basis}: expected {y}-{m:02d}-{d:02d} 17:00, got {res.ship_by}'
    )
    assert res.ship_by.tzinfo is not None


# --- D1: the core rule -----------------------------------------------------

def test_g01_in_stock_midweek_morning_ships_next_bd():
    res = ship_date(IN_STOCK, 5, _ny(2026, 6, 9, 10))      # Tue 10:00
    _expect(res, 2026, 6, 10)                               # Wed 17:00
    assert res.basis == 'in_stock_next_business_day_1700'


def test_g02_just_before_cutoff_counts_today():
    res = ship_date(IN_STOCK, 5, _ny(2026, 6, 8, 16, 59))  # Mon 16:59
    _expect(res, 2026, 6, 9)                                # Tue 17:00


def test_g03_exactly_1700_rolls_to_next_day():
    # D1: cutoff is >= 17:00 — the boundary instant rolls.
    res = ship_date(IN_STOCK, 5, _ny(2026, 6, 8, 17, 0))   # Mon 17:00:00
    _expect(res, 2026, 6, 10)                               # Wed 17:00


def test_g04_friday_afternoon_ships_monday():
    res = ship_date(IN_STOCK, 5, _ny(2026, 6, 12, 14))     # Fri 14:00
    _expect(res, 2026, 6, 15)                               # Mon 17:00


def test_g05_friday_evening_received_monday_ships_tuesday():
    res = ship_date(IN_STOCK, 5, _ny(2026, 6, 12, 18))     # Fri 18:00
    _expect(res, 2026, 6, 16)                               # Tue 17:00


def test_g06_saturday_order_received_monday_ships_tuesday():
    res = ship_date(IN_STOCK, 5, _ny(2026, 6, 13, 11))     # Sat 11:00
    _expect(res, 2026, 6, 16)                               # Tue 17:00


# --- D3: holidays and calendar edges ---------------------------------------

def test_g07_day_before_thanksgiving_skips_holiday():
    res = ship_date(IN_STOCK, 5, _ny(2026, 11, 25, 16))    # Wed 16:00
    _expect(res, 2026, 11, 27)                              # Fri (Thu = Thanksgiving)


def test_g08_thanksgiving_eve_after_cutoff_received_friday():
    res = ship_date(IN_STOCK, 5, _ny(2026, 11, 25, 18))    # Wed 18:00
    _expect(res, 2026, 11, 30)                              # received Fri -> Mon


def test_g09_dec31_order_skips_new_year_holiday():
    res = ship_date(IN_STOCK, 5, _ny(2026, 12, 31, 10))    # Thu 10:00
    _expect(res, 2027, 1, 4)                                # Fri = holiday -> Mon


def test_g10_dst_springforward_weekend_wall_clock_holds():
    # Sun 2026-03-08 02:30 EDT is inside the spring-forward gap; the rule
    # operates on the facility wall clock and the promise is still 17:00.
    res = ship_date(IN_STOCK, 5, _ny(2026, 3, 7, 10))      # Sat 10:00
    _expect(res, 2026, 3, 10)                               # Mon receipt -> Tue
    assert res.ship_by.tzname() == 'EDT'                    # post-transition


def test_g11_utc_input_converts_to_facility_clock():
    # 2026-06-08 20:30 UTC = 16:30 EDT (before cutoff) -> ships Tue.
    res = ship_date(IN_STOCK, 5,
                    datetime(2026, 6, 8, 20, 30, tzinfo=timezone.utc))
    _expect(res, 2026, 6, 9)


# --- D4: out-of-stock ------------------------------------------------------

def test_g12_out_of_stock_lead_time_governs():
    res = ship_date(OUT_OF_STOCK, 2, _ny(2026, 6, 8, 10))  # Mon, lead 5 BD
    _expect(res, 2026, 6, 15)                               # Tue..Mon = 5 BDs
    assert res.basis == 'restock_lead_time'


# --- D2: partial stock ------------------------------------------------------

def test_g13_partial_ship_complete_default():
    res = ship_date(THIN_STOCK, 10, _ny(2026, 6, 8, 10))   # qoh 4 < 10
    _expect(res, 2026, 6, 17)                               # +7 BD default lead
    assert res.basis == 'partial_ship_complete_restock'
    assert res.split is None


def test_g14_partial_split_ship_carries_both_dates():
    res = ship_date(THIN_STOCK, 10, _ny(2026, 6, 8, 10),
                    partial_policy=PartialPolicy.SPLIT_SHIP)
    assert res.basis == 'partial_split_ship'
    assert res.split.in_stock_qty == 4
    assert res.split.backorder_qty == 6
    assert res.split.in_stock_ship_by == _ny(2026, 6, 9, 17)
    assert res.split.backorder_ship_by == _ny(2026, 6, 17, 17)
    assert res.ship_by == res.split.backorder_ship_by  # line-complete promise


# --- Invalid input is rejected loudly, never guessed (D3) -------------------

def test_g15_naive_timestamp_rejected():
    with pytest.raises(ValueError, match='timezone-aware'):
        ship_date(IN_STOCK, 5, datetime(2026, 6, 9, 10))


def test_g16_zero_qty_rejected():
    with pytest.raises(ValueError, match='qty'):
        ship_date(IN_STOCK, 0, _ny(2026, 6, 9, 10))


def test_g17_inconsistent_record_rejected_at_construction():
    with pytest.raises(ValueError, match='lead time'):
        InventoryRecord(sku='X', qty_on_hand=5, lead_time_days=3)
    with pytest.raises(ValueError, match='lead_time_days'):
        InventoryRecord(sku='X', qty_on_hand=0, lead_time_days=None)


def test_g18_past_calendar_horizon_rejected():
    with pytest.raises(ValueError, match='horizon'):
        ship_date(IN_STOCK, 5, _ny(2027, 12, 31, 18))  # rolls past table end


def test_g19_oos_long_lead_crossing_horizon_raises_not_miscounts():
    """R0 #1 regression: an OOS item with a long lead time ordered near
    year-end must RAISE when the restock walk crosses the holiday-table
    horizon — never silently treat undefined-2028 holidays as business days.
    Before the fix this returned a wrong-but-plausible date in 2028."""
    from fulfillment import CalendarHorizonError
    long_lead = InventoryRecord(sku='M2-8-36-CHR', qty_on_hand=0,
                                lead_time_days=30)
    with pytest.raises(CalendarHorizonError):
        ship_date(long_lead, 2, _ny(2027, 12, 1, 10))   # +30 BD lands in 2028


def test_g20_oos_lead_within_horizon_still_works():
    # The guard must not over-trigger: an OOS item whose lead stays inside
    # the table computes normally.
    oos = InventoryRecord(sku='M2-8-36-CHR', qty_on_hand=0, lead_time_days=5)
    res = ship_date(oos, 2, _ny(2026, 6, 8, 10))
    _expect(res, 2026, 6, 15)
