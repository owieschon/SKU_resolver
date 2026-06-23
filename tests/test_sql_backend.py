"""On-prem NAV SqlBackend (P3/B) — fully tested with an injected query runner.

Proves the SQL backend is a true drop-in for the BC twin: the SAME discover()
runs over it (EDMX synthesized from INFORMATION_SCHEMA, table rows served as
OData pages), plus read-only enforcement and SQL-identifier anti-injection.
"""
from __future__ import annotations

import pytest

from erp_harness.discovery import discover, fetch_all_rows
from erp_harness.enforcer import SafetyEnforcer
from erp_harness.transport import ManualClock, TransportRequest
from erp_transport import SqlBackend

_INFO = [
    {'TABLE_NAME': 'items', 'COLUMN_NAME': 'No_', 'DATA_TYPE': 'nvarchar',
     'IS_NULLABLE': 'NO'},
    {'TABLE_NAME': 'items', 'COLUMN_NAME': 'Description', 'DATA_TYPE': 'nvarchar',
     'IS_NULLABLE': 'YES'},
    {'TABLE_NAME': 'items', 'COLUMN_NAME': 'Inventory', 'DATA_TYPE': 'decimal',
     'IS_NULLABLE': 'YES'},
]
_ITEMS = [{'No_': f'K{i}-24SBC', 'Description': f'part {i}', 'Inventory': i}
          for i in range(250)]


def _runner(sql, params):
    if 'INFORMATION_SCHEMA' in sql:
        return list(_INFO)
    if '[items]' in sql:
        offset, page = params
        return _ITEMS[offset:offset + page]
    raise AssertionError(f'unexpected sql: {sql}')


def _backend(page_size=100):
    return SqlBackend(_runner, schema='dbo', page_size=page_size)


# --- metadata / EDMX ------------------------------------------------------------

def test_metadata_builds_parseable_edmx():
    resp = _backend().handle(TransportRequest('GET', '$metadata'))
    assert resp.status == 200 and '<EntityType Name="items">' in resp.text
    assert 'Edm.Decimal' in resp.text and 'Nullable="false"' in resp.text


# --- entity paging --------------------------------------------------------------

def test_entity_page_emits_nextlink_until_exhausted():
    be = _backend(page_size=100)
    r1 = be.handle(TransportRequest('GET', '$metadata'))  # populate known tables
    assert r1.status == 200
    p1 = be.handle(TransportRequest('GET', 'items', {}))
    assert len(p1.json['value']) == 100 and '@odata.nextLink' in p1.json
    p3 = be.handle(TransportRequest('GET', 'items', {'$skiptoken': '200'}))
    assert len(p3.json['value']) == 50 and '@odata.nextLink' not in p3.json


# --- security: read-only + injection --------------------------------------------

def test_write_methods_refused():
    for m in ('POST', 'PATCH', 'PUT', 'DELETE'):
        with pytest.raises(PermissionError):
            _backend().handle(TransportRequest(m, 'items'))


def test_unknown_or_injection_table_is_404_not_executed():
    be = _backend()
    be.handle(TransportRequest('GET', '$metadata'))
    # not a known table / contains illegal chars -> never reaches a query
    assert be.handle(TransportRequest('GET', 'items;DROP TABLE x')).status == 404
    assert be.handle(TransportRequest('GET', 'secrets')).status == 404


# --- the SAME discovery runs over NAV SQL ---------------------------

def test_discover_runs_over_sql_backend_like_the_twin():
    enforcer = SafetyEnforcer(_backend(), ManualClock(),
                              rate_per_minute=10_000, total_call_budget=10_000)
    surface = discover(enforcer)
    items = surface.entity('items')
    assert items is not None
    assert {f.name for f in items.fields} == {'No_', 'Description', 'Inventory'}
    # rows paginate through the enforcer exactly as for the OData twin
    rows = fetch_all_rows(enforcer, 'items')
    assert len(rows) == 250 and rows[0]['No_'] == 'K0-24SBC'
