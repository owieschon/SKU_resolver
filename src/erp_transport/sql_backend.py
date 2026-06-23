"""On-prem NAV SQL Backend — read-only, OData-shaped, a drop-in for the twin.

NAV on-prem has no OData surface; you reach the SQL Server directly. But the
harness's discovery is written once, against the OData contract (`$metadata` ->
EDMX; an entity -> {'value': [...], '@odata.nextLink'}). So instead of a second
discovery path, this backend *translates*: it builds EDMX from
INFORMATION_SCHEMA and serves table rows as OData pages. The SAME `discover()` /
`run_onboarding` then runs over NAV SQL unchanged — the on-prem answer to the
access-topology gap.

Lives in erp_transport (not erp_harness) so the harness stays import-pure. The
DB call is a single injected `runner(sql, params) -> list[dict]`, so request
building, EDMX synthesis, paging, read-only enforcement, and SQL-identifier
validation are all unit-tested with a fake runner — no database. `from_pyodbc`
wires a real connection (gated, `[erp]` extra); live-tenant runs stay gated
behind the twin matrix.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

from erp_harness.transport import (
    Backend, TransportRequest, TransportResponse, TransportTimeout,
)

_IDENT = re.compile(r'^[A-Za-z0-9_]+$')   # SQL identifier allowlist (anti-injection)
_DEFAULT_PAGE = 100

_METADATA_SQL = (
    'SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE '
    'FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? '
    'ORDER BY TABLE_NAME, ORDINAL_POSITION')

# SQL Server type -> OData EDM type (enough for discovery/profiling).
_EDM = {
    'int': 'Edm.Int32', 'bigint': 'Edm.Int64', 'smallint': 'Edm.Int16',
    'tinyint': 'Edm.Byte', 'bit': 'Edm.Boolean',
    'decimal': 'Edm.Decimal', 'numeric': 'Edm.Decimal', 'money': 'Edm.Decimal',
    'float': 'Edm.Double', 'real': 'Edm.Single',
    'datetime': 'Edm.DateTimeOffset', 'datetime2': 'Edm.DateTimeOffset',
    'date': 'Edm.Date',
}


def _edm_type(sql_type: str) -> str:
    return _EDM.get((sql_type or '').lower(), 'Edm.String')


def _build_edmx(columns: list[dict]) -> str:
    """INFORMATION_SCHEMA.COLUMNS rows -> EDMX XML in the OData edm namespace
    that crawl_metadata() already parses (one EntityType per table)."""
    by_table: dict[str, list[dict]] = {}
    for c in columns:
        by_table.setdefault(c['TABLE_NAME'], []).append(c)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" '
        'Version="4.0"><edmx:DataServices>',
        '<Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="NAV">',
    ]
    for table, cols in by_table.items():
        parts.append(f'<EntityType Name="{escape(table)}">')
        for c in cols:
            nullable = 'true' if str(c.get('IS_NULLABLE', 'YES')).upper() != 'NO' \
                else 'false'
            parts.append(
                f'<Property Name="{escape(c["COLUMN_NAME"])}" '
                f'Type="{_edm_type(c.get("DATA_TYPE"))}" Nullable="{nullable}"/>')
        parts.append('</EntityType>')
    parts.append('</Schema></edmx:DataServices></edmx:Edmx>')
    return ''.join(parts)


class SqlBackend:
    """erp_harness.transport.Backend over a SQL Server (NAV on-prem)."""

    def __init__(self, runner, *, schema: str = 'dbo',
                 page_size: int = _DEFAULT_PAGE) -> None:
        # runner(sql, params) -> list[dict] (column-name-keyed rows).
        self._runner = runner
        self._schema = schema if _IDENT.match(schema) else 'dbo'
        self._page = page_size
        self._tables: set[str] | None = None

    def _known_tables(self, columns: list[dict]) -> set[str]:
        return {c['TABLE_NAME'] for c in columns}

    def handle(self, req: TransportRequest) -> TransportResponse:
        if req.method not in ('GET', 'HEAD'):
            raise PermissionError(f'SqlBackend is read-only: {req.method}')
        try:
            if req.path == '$metadata':
                cols = self._runner(_METADATA_SQL, (self._schema,))
                self._tables = self._known_tables(cols)
                return TransportResponse(status=200, headers={}, json=None,
                                         text=_build_edmx(cols))
            return self._entity_page(req)
        except TransportTimeout:
            raise
        except PermissionError:
            raise
        except Exception as e:                 # a DB/driver error
            return TransportResponse(status=502, headers={},
                                     json={'error': str(e)}, text=str(e))

    def _entity_page(self, req: TransportRequest) -> TransportResponse:
        table = req.path
        # Anti-injection: identifier-shaped AND a real table we discovered.
        if self._tables is None:
            self._tables = self._known_tables(
                self._runner(_METADATA_SQL, (self._schema,)))
        if not _IDENT.match(table) or table not in self._tables:
            return TransportResponse(status=404, headers={},
                                     json={'value': []}, text=None)
        page = _int(req.params.get('$top'), self._page)
        offset = _int(req.params.get('$skiptoken'), 0)
        # Identifiers are validated above, so this interpolation is safe; values
        # are bound parameters.
        sql = (f'SELECT * FROM [{self._schema}].[{table}] '
               f'ORDER BY 1 OFFSET ? ROWS FETCH NEXT ? ROWS ONLY')
        rows = self._runner(sql, (offset, page))
        body = {'value': rows}
        if len(rows) == page:                  # a full page -> there may be more
            body['@odata.nextLink'] = f'{table}?$skiptoken={offset + page}'
        return TransportResponse(status=200, headers={}, json=body, text=None)

    @classmethod
    def from_pyodbc(cls, connection_string: str, *, schema: str = 'dbo',
                    query_timeout_s: int = 30, **kw) -> 'SqlBackend':
        """Wire a real read-only pyodbc connection (gated; needs the [erp]
        extra). Sets a query timeout that surfaces as TransportTimeout."""
        import pyodbc   # pragma: no cover - live only

        conn = pyodbc.connect(connection_string, readonly=True,
                              timeout=query_timeout_s)
        conn.timeout = query_timeout_s

        def runner(sql, params):   # pragma: no cover - live only
            try:
                cur = conn.cursor()
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description] if cur.description else []
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except pyodbc.OperationalError as e:
                raise TransportTimeout(str(e)) from e

        return cls(runner, schema=schema, **kw)


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
