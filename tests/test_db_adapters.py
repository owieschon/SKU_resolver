"""SQLite-backed CustomerDB + PriceBook (P3) — parity with the in-memory/synthetic
defaults, plus the gateway running end-to-end against them. SQLite is stdlib, so
this production adapter is fully CI-tested (no credentials, unlike ERP/LLM)."""
from __future__ import annotations

from gateway import (
    Account, SqliteCustomerDB, SqlitePriceBook, SyntheticPriceBook,
)
from gateway.customer_db import InMemoryCustomerDB

ACCOUNTS = [
    Account('1001', 'DEMO TRUCK CENTER', '5550100100'),
    Account('3300', 'TRUCK PARTS COMPANY', None),
    Account('3301', 'TRUCK PARTS WEST', None),
]


# --- CustomerDB parity ----------------------------------------------------------

def test_customerdb_by_number_exact():
    db = SqliteCustomerDB.build(':memory:', ACCOUNTS)
    assert db.by_number('1001').name == 'DEMO TRUCK CENTER'
    assert db.by_number(' 1001 ').account_id == '1001'   # trimmed
    assert db.by_number('9999') is None


def test_customerdb_by_name_zero_one_many_matches_inmemory():
    sql = SqliteCustomerDB.build(':memory:', ACCOUNTS)
    mem = InMemoryCustomerDB(ACCOUNTS)
    for q in ['demo', 'truck parts', 'nonexistent', '']:
        sql_ids = [a.account_id for a in sql.by_name(q)]
        mem_ids = [a.account_id for a in mem.by_name(q)]
        assert sql_ids == mem_ids, q
    # 'truck parts' is a 'many' case (caller refuses); order is deterministic.
    assert [a.account_id for a in sql.by_name('truck parts')] == ['3300', '3301']


def test_customerdb_name_wildcards_are_escaped_not_injected():
    # A query containing LIKE wildcards must not match everything.
    db = SqliteCustomerDB.build(':memory:', ACCOUNTS)
    assert db.by_name('%') == []
    assert db.by_name('_____') == []


# --- PriceBook parity -----------------------------------------------------------

def test_pricebook_matches_synthetic_values():
    syn = SyntheticPriceBook.seeded(['K5-24SBC', 'M7-10'])
    db = SqlitePriceBook.build(':memory:', syn.base_by_sku, syn.tier_multiplier)
    for sku in ['K5-24SBC', 'M7-10']:
        for tier in ['standard', 'preferred', 'distributor']:
            assert db.price(sku, tier) == syn.price(sku, tier)


def test_pricebook_unknown_sku_none_unknown_tier_falls_back():
    syn = SyntheticPriceBook.seeded(['K5-24SBC'])
    db = SqlitePriceBook.build(':memory:', syn.base_by_sku, syn.tier_multiplier)
    assert db.price('NOPE', 'standard') is None
    # unknown tier -> multiplier 1.0 (the base price)
    assert db.price('K5-24SBC', 'mystery') == round(syn.base_by_sku['K5-24SBC'], 2)


# --- gateway end-to-end on the sqlite adapters ----------------------------------

def test_gateway_verifies_and_prices_against_sqlite(tmp_path):
    from gateway_fixtures import build_gateway
    from gateway import Channel
    gw, sessions, journal, clk = build_gateway(tmp_path)

    # Swap in sqlite-backed customer db + price book, same data.
    cdb = SqliteCustomerDB.build(tmp_path / 'cust.db', ACCOUNTS)
    syn = SyntheticPriceBook.seeded(gw.catalog.all_skus())
    pbook = SqlitePriceBook.build(tmp_path / 'price.db', syn.base_by_sku,
                                  syn.tier_multiplier)
    sessions._customer_db = cdb            # session verification source
    gw.pricebook = pbook                   # gateway pricing source

    sid = 'web-db'
    tok = sessions.open(sid, sid)
    v = gw.turn(sid, tok, 'my account number is 1001', channel=Channel.TYPED)
    assert v.session_state == 'verified'
    p = gw.turn(sid, tok, 'how much is K5-24SBC?', channel=Channel.TYPED)
    assert p.price is not None                      # priced from the sqlite book
    assert p.price.source == 'verified_account_self'
    # the price matches what the sqlite book returns for this account's tier
    assert p.price.unit_price == pbook.price('K5-24SBC', 'preferred')
