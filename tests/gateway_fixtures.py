"""Shared gateway construction for the conversational-surface tests."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sku_translator import FixtureCatalogIndex, InMemoryStore
from resolution import ResolutionService, catalog_content_version
from fulfillment import load_inventory
from gateway import (
    Account, ConversationJournal, Gateway, InMemoryCustomerDB, SessionManager,
    SyntheticPriceBook,
)

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / 'data' / 'catalog.csv'
INVENTORY = REPO / 'data' / 'inventory.json'
NY = ZoneInfo('America/New_York')

ACCOUNTS = [
    Account('1001', 'DEMO TRUCK CENTER', '5550100100'),
    Account('2055', 'NORTH FLEET TRUCK', '5550100200'),
    Account('3300', 'TRUCK PARTS COMPANY', None),
    Account('3301', 'TRUCK PARTS WEST', None),   # name-collision with 3300
]

_catalog = None
_version = None


def _shared_catalog():
    global _catalog, _version
    if _catalog is None:
        _catalog = FixtureCatalogIndex(str(CATALOG), tenant_id='tenant_001')
        _version = catalog_content_version(CATALOG)
    return _catalog, _version


def build_gateway(tmp_path, *, now=None, clock_start=0.0):
    cat, ver = _shared_catalog()
    svc = ResolutionService(cat, InMemoryStore(), catalog_version=ver)
    inv = load_inventory(INVENTORY)
    db = InMemoryCustomerDB(ACCOUNTS)
    pb = SyntheticPriceBook.seeded(cat.all_skus())
    clk = {'t': clock_start}
    sessions = SessionManager(secret=b'gateway-test-secret', customer_db=db,
                              now_fn=lambda: clk['t'])
    journal = ConversationJournal(path=Path(tmp_path) / 'journal.jsonl',
                                  now_fn=lambda: '2026-06-08T10:00:00')
    received = now or datetime(2026, 6, 8, 10, 0, tzinfo=NY)  # Mon 10am
    gw = Gateway(service=svc, catalog=cat, inventory=inv, catalog_version=ver,
                 sessions=sessions, journal=journal, pricebook=pb,
                 account_tier_of=lambda aid: 'preferred',
                 now_fn=lambda: received)
    return gw, sessions, journal, clk


def in_stock_sku(gw) -> str:
    """A SKU known to be in stock (for deterministic availability assertions)."""
    for sku, rec in gw.inventory.items():
        if rec.qty_on_hand > 0 and gw.catalog.is_canonical(sku):
            return sku
    raise AssertionError('no in-stock canonical SKU in fixture')
