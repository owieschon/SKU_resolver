"""Deterministic fulfillment engine: definitive ship dates from inventory
state and business-calendar rules. PURE — no LLM, no network, no I/O in the
decision path (enforced by tests/test_fulfillment_purity.py).

Policy decisions D1–D4 in docs/DECISION_LOG.md govern this package; the
golden table in tests/test_ship_date_golden.py IS the business-rule spec.
"""
from fulfillment.calendar import (
    CALENDAR_HORIZON,
    FACILITY_TZ,
    HOLIDAYS,
    CalendarHorizonError,
    add_business_days,
    is_business_day,
    normalize_receipt,
)
from fulfillment.engine import (
    DEFAULT_RESTOCK_LEAD_DAYS,
    InventoryRecord,
    PartialPolicy,
    ShipDateResult,
    load_inventory,
    ship_date,
)

__all__ = [
    'CALENDAR_HORIZON', 'CalendarHorizonError', 'FACILITY_TZ', 'HOLIDAYS',
    'add_business_days', 'is_business_day', 'normalize_receipt',
    'DEFAULT_RESTOCK_LEAD_DAYS', 'InventoryRecord', 'PartialPolicy',
    'ShipDateResult', 'load_inventory', 'ship_date',
]
