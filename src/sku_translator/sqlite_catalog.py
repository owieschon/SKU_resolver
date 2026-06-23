"""SqliteCatalogIndex — a SQLite-backed implementation of the CatalogIndex
protocol.

A third backend alongside the CSV fixture and the (stubbed) ERP index. The
catalog is structured, read-heavy data with several access patterns — exact
lookup by SKU, bucket by family / (family, diameter), prefix scan — which is
exactly what an indexed relational table is for. This backend models the
catalog as one table with the indexes those access patterns need, and answers
every query through SQL rather than hand-maintained in-memory dicts.

It is a drop-in for ``FixtureCatalogIndex`` (proven by
``tests/test_sqlite_catalog.py``, which runs both behind the same contract).
Rows are parsed once by the canonical CSV loader, then served from SQLite.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator

try:
    from sku_translator.catalog_index import ParsedRow, family_prefix_for
    from sku_translator.fixture_catalog import FixtureCatalogIndex
except ImportError:  # pragma: no cover - import shim for src-on-path execution
    from catalog_index import ParsedRow, family_prefix_for
    from fixture_catalog import FixtureCatalogIndex


# Scalar ParsedRow fields stored as columns (the two dict fields are stored as
# JSON). Order matters: it drives both the schema and row reconstruction.
_COLUMNS = [
    'sku', 'pattern', 'family', 'family_meaning', 'diameter', 'length', 'angle',
    'leg1', 'leg2', 'body', 'finish', 'inlet_diameter', 'outlet_diameter',
    'is_reducer', 'oem', 'oem_meaning', 'description', 'is_proprietary',
    'proprietary_customer', 'sales_count', 'sales_qty_year', 'quantity_on_hand',
    'is_obsolete', 'unit_price', 'is_customer_facing',
]
_BOOL_FIELDS = {'is_reducer', 'is_proprietary', 'is_obsolete', 'is_customer_facing'}

_SCHEMA = """
CREATE TABLE catalog (
    sku                  TEXT PRIMARY KEY,
    sku_upper            TEXT NOT NULL,          -- case-insensitive lookup key
    family_prefix        TEXT NOT NULL,          -- fuzzy-matcher prefix bucket
    pattern              TEXT,
    family               TEXT,
    family_meaning       TEXT,
    diameter             REAL,
    length               REAL,
    angle                INTEGER,
    leg1                 REAL,
    leg2                 REAL,
    body                 TEXT,
    finish               TEXT,
    inlet_diameter       REAL,
    outlet_diameter      REAL,
    is_reducer           INTEGER NOT NULL DEFAULT 0,
    oem                  TEXT,
    oem_meaning          TEXT,
    description          TEXT NOT NULL DEFAULT '',
    is_proprietary       INTEGER NOT NULL DEFAULT 0,
    proprietary_customer TEXT,
    sales_count          INTEGER NOT NULL DEFAULT 0,
    sales_qty_year       INTEGER NOT NULL DEFAULT 0,
    quantity_on_hand     INTEGER NOT NULL DEFAULT 0,
    is_obsolete          INTEGER NOT NULL DEFAULT 0,
    unit_price           REAL NOT NULL DEFAULT 0.0,
    is_customer_facing   INTEGER NOT NULL DEFAULT 0,
    raw_parser_json      TEXT NOT NULL DEFAULT '{}',
    raw_erp_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX idx_catalog_sku_upper      ON catalog (sku_upper);
CREATE INDEX        idx_catalog_family          ON catalog (family);
CREATE INDEX        idx_catalog_family_diameter ON catalog (family, diameter);
CREATE INDEX        idx_catalog_family_prefix   ON catalog (family_prefix);
"""


class SqliteCatalogIndex:
    """CatalogIndex backed by SQLite. Drop-in for FixtureCatalogIndex.

    Holds only ACTIVE rows (obsolete/battery already filtered by the loader),
    so ``size()`` is ``COUNT(*)`` and every query is naturally scoped.
    """

    def __init__(
        self,
        catalog_path: str | Path,
        *,
        tenant_id: str = 'tenant_fixture',
        db_path: str | Path = ':memory:',
    ) -> None:
        self._catalog_path = Path(catalog_path)
        self._tenant_id = tenant_id
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self.reload()

    # ----- Lifecycle ----------------------------------------------------

    def reload(self) -> None:
        """(Re)build the table from the catalog source.

        Parses rows with the canonical CSV loader, then loads them into a fresh
        table. All-or-nothing: a failure leaves the prior table in place.
        """
        rows = list(FixtureCatalogIndex(
            self._catalog_path, tenant_id=self._tenant_id).parsed_rows())

        cur = self._conn.cursor()
        cur.executescript('DROP TABLE IF EXISTS catalog;' + _SCHEMA)
        cols = (['sku', 'sku_upper', 'family_prefix']
                + [c for c in _COLUMNS if c != 'sku']
                + ['raw_parser_json', 'raw_erp_json'])
        insert = f"INSERT INTO catalog ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        cur.executemany(insert, (self._to_record(r) for r in rows))
        self._conn.commit()

    @staticmethod
    def _to_record(r: ParsedRow) -> tuple:
        vals = [r.sku, r.sku.upper(), family_prefix_for(r.sku)]
        for c in _COLUMNS:
            if c == 'sku':
                continue
            v = getattr(r, c)
            vals.append(int(v) if c in _BOOL_FIELDS else v)
        vals.append(json.dumps(r.raw_parser_result, default=str))
        vals.append(json.dumps(r.raw_erp_row, default=str))
        return tuple(vals)

    @staticmethod
    def _to_row(row: sqlite3.Row) -> ParsedRow:
        kw = {c: row[c] for c in _COLUMNS}
        for b in _BOOL_FIELDS:
            kw[b] = bool(kw[b])
        kw['raw_parser_result'] = json.loads(row['raw_parser_json'])
        kw['raw_erp_row'] = json.loads(row['raw_erp_json'])
        return ParsedRow(**kw)

    # ----- CatalogIndex protocol ---------------------------------------

    def tenant_id(self) -> str:
        return self._tenant_id

    def is_canonical(self, sku: str) -> bool:
        if not sku:
            return False
        cur = self._conn.execute(
            'SELECT 1 FROM catalog WHERE sku_upper = ? LIMIT 1',
            (sku.strip().upper(),))
        return cur.fetchone() is not None

    def lookup(self, sku: str) -> ParsedRow | None:
        if not sku:
            return None
        row = self._conn.execute(
            'SELECT * FROM catalog WHERE sku_upper = ?',
            (sku.strip().upper(),)).fetchone()
        return self._to_row(row) if row is not None else None

    def parsed_rows(self) -> Iterator[ParsedRow]:
        for row in self._conn.execute('SELECT * FROM catalog'):
            yield self._to_row(row)

    def all_skus(self) -> list[str]:
        return [r['sku'] for r in self._conn.execute('SELECT sku FROM catalog')]

    def bucket(
        self,
        family: str | None = None,
        diameter: float | None = None,
    ) -> list[ParsedRow]:
        sql = 'SELECT * FROM catalog'
        clauses, params = [], []
        if family is not None:
            clauses.append('family = ?')
            params.append(family)
        if diameter is not None:
            clauses.append('diameter = ?')
            params.append(float(diameter))
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        return [self._to_row(r) for r in self._conn.execute(sql, params)]

    def family_prefix_bucket(self, prefix: str) -> list[ParsedRow]:
        return [self._to_row(r) for r in self._conn.execute(
            'SELECT * FROM catalog WHERE family_prefix = ?', (prefix.upper(),))]

    def size(self) -> int:
        return self._conn.execute('SELECT COUNT(*) AS n FROM catalog').fetchone()['n']

    def __repr__(self) -> str:
        return (f'<SqliteCatalogIndex tenant={self._tenant_id!r} '
                f'db={self._db_path!r} size={self.size()}>')
