"""SqliteCatalogIndex must be a behavioural drop-in for FixtureCatalogIndex:
the two backends, built from the same catalog, answer the CatalogIndex contract
identically. (The fixture is the reference; SQLite is the tested backend.)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sku_translator import FixtureCatalogIndex
from sku_translator.sqlite_catalog import SqliteCatalogIndex

CATALOG = os.environ.get(
    'SKU_CATALOG_PATH',
    str(Path(__file__).resolve().parent.parent / 'data' / 'catalog.csv'),
)

# Compared field-by-field on lookup. (raw_* dicts round-trip through JSON, so
# tuple-valued entries come back as lists; the promoted scalar fields are the
# contract downstream consumers read, so we compare those.)
_FIELDS = [
    'sku', 'pattern', 'family', 'family_meaning', 'diameter', 'length', 'angle',
    'leg1', 'leg2', 'body', 'finish', 'inlet_diameter', 'outlet_diameter',
    'is_reducer', 'oem', 'oem_meaning', 'description', 'is_proprietary',
    'proprietary_customer', 'sales_count', 'sales_qty_year', 'quantity_on_hand',
    'is_obsolete', 'unit_price', 'is_customer_facing',
]


@pytest.fixture(scope='module')
def fixture_idx():
    return FixtureCatalogIndex(CATALOG, tenant_id='t')


@pytest.fixture(scope='module')
def sqlite_idx():
    return SqliteCatalogIndex(CATALOG, tenant_id='t')


def test_size_matches(fixture_idx, sqlite_idx):
    assert sqlite_idx.size() == fixture_idx.size() > 0


def test_all_skus_match(fixture_idx, sqlite_idx):
    assert set(sqlite_idx.all_skus()) == set(fixture_idx.all_skus())
    # parsed_rows() yields the same population
    assert sum(1 for _ in sqlite_idx.parsed_rows()) == fixture_idx.size()


def test_is_canonical_matches(fixture_idx, sqlite_idx):
    for sku in ['K5-24SBC', 'k5-24sbc', '  K5-24SBC  '.strip(),
                'NOT-A-REAL-SKU', '']:
        assert sqlite_idx.is_canonical(sku) == fixture_idx.is_canonical(sku)


def test_lookup_returns_equivalent_rows(fixture_idx, sqlite_idx):
    sample = fixture_idx.all_skus()[:200] + ['k5-24sbc']  # incl. case-insensitive
    for sku in sample:
        fr, sr = fixture_idx.lookup(sku), sqlite_idx.lookup(sku)
        assert (fr is None) == (sr is None), sku
        if fr is None:
            continue
        for f in _FIELDS:
            assert getattr(sr, f) == getattr(fr, f), f'{sku}.{f}'


def test_lookup_miss_is_none(sqlite_idx):
    assert sqlite_idx.lookup('NOPE-9999') is None


def test_bucket_matches(fixture_idx, sqlite_idx):
    cases = [('K', 5.0), ('K', None), ('L', None), (None, None), ('NOSUCH', None)]
    for family, diameter in cases:
        fb = {r.sku for r in fixture_idx.bucket(family=family, diameter=diameter)}
        sb = {r.sku for r in sqlite_idx.bucket(family=family, diameter=diameter)}
        assert sb == fb, (family, diameter)


def test_family_prefix_bucket_matches(fixture_idx, sqlite_idx):
    for prefix in ['K', 'BH', 'L', 'k', 'ZZZ']:
        fb = {r.sku for r in fixture_idx.family_prefix_bucket(prefix)}
        sb = {r.sku for r in sqlite_idx.family_prefix_bucket(prefix)}
        assert sb == fb, prefix


def test_translate_works_through_sqlite_backend(sqlite_idx):
    # The goal: the resolution engine runs unchanged on this backend.
    from sku_translator import InMemoryStore, translate
    r = translate('5 inch chrome curved 24 long SB',
                  catalog=sqlite_idx, memory=InMemoryStore())
    assert r.sku == 'K5-24SBC' and r.state == 'resolved'
