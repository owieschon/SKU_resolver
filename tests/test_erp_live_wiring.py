"""Live-tenant ERP wiring (P3/D) — build the enforcer from env, no network.

The BC branch is stdlib (urllib) so it's fully CI-tested: the builder assembles
an HttpBackend under a SafetyEnforcer without touching the network (OAuth is
lazy). The NAV branch needs pyodbc and is gated. A separate credential-gated
live smoke (test_live_erp_smoke.py) runs discover() against a real tenant.
"""
from __future__ import annotations

import pytest

from erp_harness.enforcer import SafetyEnforcer
from erp_transport import WallClock, build_live_backend, build_live_enforcer
from erp_transport.http_backend import HttpBackend

_BC_ENV = {
    'SKU_ERP_KIND': 'bc',
    'SKU_ERP_BASE_URL': 'https://api.businesscentral.dynamics.com/v2.0/x/api',
    'SKU_ERP_TOKEN_URL': 'https://login.microsoftonline.com/x/oauth2/v2.0/token',
    'SKU_ERP_CLIENT_ID': 'cid', 'SKU_ERP_CLIENT_SECRET': 'sec',
    'SKU_ERP_SCOPE': 'https://api.businesscentral.dynamics.com/.default',
}


def test_bc_branch_builds_http_backend_under_enforcer():
    enforcer = build_live_enforcer(_BC_ENV, clock=WallClock())
    assert isinstance(enforcer, SafetyEnforcer)
    assert isinstance(build_live_backend(_BC_ENV), HttpBackend)   # no network


def test_missing_config_raises_named_error():
    incomplete = {'SKU_ERP_KIND': 'bc', 'SKU_ERP_BASE_URL': 'https://x'}
    with pytest.raises(RuntimeError) as e:
        build_live_backend(incomplete)
    assert 'SKU_ERP_TOKEN_URL' in str(e.value)


def test_unknown_kind_raises():
    with pytest.raises(RuntimeError):
        build_live_backend({'SKU_ERP_KIND': ''})


def test_wallclock_is_monotonic_and_sleeps_nonnegative():
    c = WallClock()
    t0 = c.now()
    c.sleep(0)                       # no-op, must not raise
    assert c.now() >= t0


def test_nav_branch_needs_pyodbc():
    pytest.importorskip('pyodbc')   # gated: only when the [erp] extra is present
    be = build_live_backend({'SKU_ERP_KIND': 'nav',
                             'SKU_ERP_SQL_DSN': 'Driver=x;Server=y'})
    from erp_transport import SqlBackend
    assert isinstance(be, SqlBackend)
