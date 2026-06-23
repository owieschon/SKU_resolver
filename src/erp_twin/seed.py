"""Deterministic twin seeding.

Items come from the real catalog fixture (a 500-row deterministic subset, so
the golden path's final step — the translator's identity guarantee against
adapter-synced data — runs against real SKU shapes). Customers and orders are
synthetic, seeded. valueEntries exists but is HIDDEN from the API surface:
that is the documented BC gap (no standard v2.0 API for the cost ledger) the
gap detector must classify rather than discover.
"""
from __future__ import annotations

import random
from pathlib import Path

from erp_harness.transport import Clock, ManualClock
from erp_twin.twin import BCShapedTwin, TwinEntity, TwinField

SEED = 20260607
ITEM_SUBSET = 500

STANDARD_GRANTS = {'metadata', 'items', 'customers', 'salesOrders', 'status'}


def _item_rows(catalog_path: str | Path, limit: int) -> list[dict]:
    from sku_translator import FixtureCatalogIndex  # twin-only dependency
    cat = FixtureCatalogIndex(str(catalog_path), tenant_id='twin_seed')
    rows = []
    for r in sorted(cat.parsed_rows(), key=lambda r: r.sku)[:limit]:
        rows.append({
            'number': r.sku,
            'displayName': r.description or '',
            'inventoryQty': r.quantity_on_hand,
            'blocked': bool(r.is_obsolete),
            'lastModifiedDateTime': '2026-06-01T12:00:00Z',
        })
    return rows


def seeded_twin(catalog_path: str | Path, *, clock: Clock | None = None,
                granted: set[str] | None = None,
                throttle_per_minute: int | None = None,
                item_limit: int = ITEM_SUBSET) -> BCShapedTwin:
    rng = random.Random(SEED)
    items = TwinEntity(
        name='items',
        fields=[
            TwinField('number', 'Edm.String', False, 1),
            TwinField('displayName', 'Edm.String', True, 2),
            TwinField('inventoryQty', 'Edm.Decimal', True, 3),
            TwinField('blocked', 'Edm.Boolean', True, 4),
            TwinField('lastModifiedDateTime', 'Edm.DateTimeOffset', True, 5),
        ],
        rows=_item_rows(catalog_path, item_limit),
        nav_properties=['itemCategory'],
    )
    customers = TwinEntity(
        name='customers',
        fields=[
            TwinField('number', 'Edm.String', False, 1),
            TwinField('displayName', 'Edm.String', True, 2),
            TwinField('blocked', 'Edm.Boolean', True, 3),
        ],
        rows=[{'number': f'C{i:05d}',
               'displayName': f'CUSTOMER {i:05d} LLC',
               'blocked': False} for i in range(1, 51)],
    )
    orders = TwinEntity(
        name='salesOrders',
        fields=[
            TwinField('number', 'Edm.String', False, 1),
            TwinField('customerNumber', 'Edm.String', False, 2),
            TwinField('orderDate', 'Edm.Date', True, 3),
        ],
        rows=[{'number': f'SO{i:06d}',
               'customerNumber': f'C{rng.randint(1, 50):05d}',
               'orderDate': f'2026-0{rng.randint(1, 5)}-{rng.randint(10, 28)}'}
              for i in range(1, 101)],
    )
    value_entries = TwinEntity(
        name='valueEntries',
        fields=[TwinField('entryNo', 'Edm.Int32', False, 1),
                TwinField('costAmount', 'Edm.Decimal', True, 2)],
        rows=[],
        hidden=True,   # the documented BC standard-API gap
    )
    return BCShapedTwin(
        [items, customers, orders, value_entries],
        clock=clock or ManualClock(),
        granted=set(granted if granted is not None else STANDARD_GRANTS),
        throttle_per_minute=throttle_per_minute,
        posting_queue_depth=40,
        posting_drain_per_minute=10,
    )
