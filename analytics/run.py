#!/usr/bin/env python3
"""Run the catalog analytics queries.

Loads the synthetic catalog (parsed) + the inventory snapshot into an in-memory
SQLite database, then executes each query in catalog_analytics.sql and prints a
labelled table. Reproducible: same data + same queries => same output.

    python analytics/run.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src'))

from sku_translator import FixtureCatalogIndex

SQL_FILE = Path(__file__).resolve().parent / 'catalog_analytics.sql'


def build_db() -> sqlite3.Connection:
    """catalog (parsed) + inventory tables in one in-memory database."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE catalog (
            sku TEXT PRIMARY KEY, family TEXT, diameter REAL, finish TEXT,
            body TEXT, sales_count INTEGER, unit_price REAL,
            quantity_on_hand INTEGER, is_customer_facing INTEGER,
            is_proprietary INTEGER)
    """)
    rows = FixtureCatalogIndex(REPO / 'data' / 'catalog.csv',
                               tenant_id='analytics').parsed_rows()
    conn.executemany(
        'INSERT INTO catalog VALUES (?,?,?,?,?,?,?,?,?,?)',
        [(r.sku, r.family, r.diameter, r.finish, r.body, r.sales_count,
          r.unit_price, r.quantity_on_hand, int(r.is_customer_facing),
          int(r.is_proprietary)) for r in rows])

    conn.execute('CREATE TABLE inventory (sku TEXT PRIMARY KEY, '
                 'qty_on_hand INTEGER, lead_time_days INTEGER)')
    inv = json.loads((REPO / 'data' / 'inventory.json').read_text())['records']
    conn.executemany(
        'INSERT INTO inventory VALUES (?,?,?)',
        [(sku, v['qty_on_hand'], v['lead_time_days']) for sku, v in inv.items()])
    conn.execute('CREATE INDEX idx_cat_family ON catalog(family)')
    conn.commit()
    return conn


def split_queries(sql: str) -> list[tuple[str, str]]:
    """Split the .sql file on '-- name: <title>' markers into (title, query)."""
    parts = re.split(r'(?m)^-- name:\s*(.+)$', sql)
    out = []
    for i in range(1, len(parts), 2):
        title, body = parts[i].strip(), parts[i + 1].strip().rstrip(';').strip()
        if body:
            out.append((title, body))
    return out


def render(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return '  (no rows)'
    cols = rows[0].keys()
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    head = '  ' + '  '.join(c.rjust(widths[c]) for c in cols)
    sep = '  ' + '  '.join('-' * widths[c] for c in cols)
    body = ['  ' + '  '.join(str(r[c]).rjust(widths[c]) for c in cols) for r in rows]
    return '\n'.join([head, sep, *body])


def main() -> int:
    conn = build_db()
    n_cat = conn.execute('SELECT COUNT(*) AS n FROM catalog').fetchone()['n']
    n_inv = conn.execute('SELECT COUNT(*) AS n FROM inventory').fetchone()['n']
    print(f'Loaded {n_cat} catalog rows + {n_inv} inventory rows\n')
    for title, query in split_queries(SQL_FILE.read_text()):
        print(f'== {title} ==')
        print(render(conn.execute(query).fetchall()))
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
