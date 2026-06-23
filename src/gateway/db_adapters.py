"""SQLite-backed CustomerDB + PriceBook — production adapters (P3).

The in-memory CustomerDB and synthetic PriceBook are the CI defaults. These are
the persistent implementations behind the same protocols: a real deployment
points at a SQLite file (or, swapping the connection, any DB-API source). SQLite
is stdlib, so unlike the credentialed ERP/LLM adapters this one is fully tested
in CI with no external dependency.

Same posture as everywhere else: the adapter implements the protocol exactly,
adds nothing to the gateway's contract, and is selected by config — "go to
production" is pointing SKU_CUSTOMER_DB / SKU_PRICEBOOK_DB at a file, not a code
change. Read-only on the query path; the `build_*` helpers materialize a DB from
the in-memory/synthetic sources for seeding and tests.
"""
from __future__ import annotations

import sqlite3

from gateway.models import Account


def _connect(source) -> sqlite3.Connection:
    if isinstance(source, sqlite3.Connection):
        return source
    conn = sqlite3.connect(str(source), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class SqliteCustomerDB:
    """CustomerDB over a SQLite `accounts(account_id, name, phone)` table.
    Same match semantics as InMemoryCustomerDB; the 0/1/many rule and the
    no-existence-oracle refusal stay in the gateway, not here."""

    def __init__(self, source) -> None:
        self._conn = _connect(source)
        self._conn.row_factory = sqlite3.Row

    def by_number(self, account_no: str) -> Account | None:
        row = self._conn.execute(
            'SELECT account_id, name, phone FROM accounts WHERE account_id = ?',
            (account_no.strip(),)).fetchone()
        return self._account(row) if row else None

    def by_name(self, name: str) -> list[Account]:
        q = name.strip().lower()
        if not q:
            return []
        # Substring match, deterministic order (account_id) — the caller applies
        # the 0/1/many rule. LIKE param is escaped against wildcard injection.
        like = '%' + q.replace('%', r'\%').replace('_', r'\_') + '%'
        rows = self._conn.execute(
            "SELECT account_id, name, phone FROM accounts "
            "WHERE LOWER(name) LIKE ? ESCAPE '\\' ORDER BY account_id",
            (like,)).fetchall()
        return [self._account(r) for r in rows]

    @staticmethod
    def _account(row) -> Account:
        return Account(account_id=row['account_id'], name=row['name'],
                       phone=row['phone'])

    @classmethod
    def build(cls, path, accounts: list[Account]) -> 'SqliteCustomerDB':
        conn = _connect(path)
        conn.execute('CREATE TABLE IF NOT EXISTS accounts '
                     '(account_id TEXT PRIMARY KEY, name TEXT, phone TEXT)')
        conn.execute('DELETE FROM accounts')
        conn.executemany('INSERT INTO accounts VALUES (?, ?, ?)',
                         [(a.account_id, a.name, a.phone) for a in accounts])
        conn.commit()
        return cls(conn)


class SqlitePriceBook:
    """PriceBook over SQLite: `prices(sku, base)` × `tiers(tier, multiplier)`.
    Returns None for an unknown SKU (same contract as SyntheticPriceBook); an
    unknown tier falls back to multiplier 1.0."""

    def __init__(self, source) -> None:
        self._conn = _connect(source)
        self._conn.row_factory = sqlite3.Row

    def price(self, sku: str, tier: str) -> float | None:
        base = self._conn.execute(
            'SELECT base FROM prices WHERE sku = ?', (sku,)).fetchone()
        if base is None:
            return None
        mult = self._conn.execute(
            'SELECT multiplier FROM tiers WHERE tier = ?', (tier,)).fetchone()
        m = mult['multiplier'] if mult else 1.0
        return round(base['base'] * m, 2)

    @classmethod
    def build(cls, path, base_by_sku: dict, tier_multiplier: dict
              ) -> 'SqlitePriceBook':
        conn = _connect(path)
        conn.execute('CREATE TABLE IF NOT EXISTS prices '
                     '(sku TEXT PRIMARY KEY, base REAL)')
        conn.execute('CREATE TABLE IF NOT EXISTS tiers '
                     '(tier TEXT PRIMARY KEY, multiplier REAL)')
        conn.execute('DELETE FROM prices')
        conn.execute('DELETE FROM tiers')
        conn.executemany('INSERT INTO prices VALUES (?, ?)',
                         list(base_by_sku.items()))
        conn.executemany('INSERT INTO tiers VALUES (?, ?)',
                         list(tier_multiplier.items()))
        conn.commit()
        return cls(conn)
