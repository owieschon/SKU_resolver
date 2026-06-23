"""Live ERP smoke — REAL tenant, credential-gated. NOT CI.

Runs the real onboarding discovery against a live ERP via build_live_enforcer.
Skipped unless SKU_ERP_KIND + connection env are set. Per the harness spec,
a live-tenant run is gated behind a green twin fault-injection check matrix —
this is the one-command launcher once that bar is met.

    SKU_ERP_KIND=bc SKU_ERP_BASE_URL=... SKU_ERP_TOKEN_URL=... \
    SKU_ERP_CLIENT_ID=... SKU_ERP_CLIENT_SECRET=... SKU_ERP_SCOPE=... \
    pytest tests/test_live_erp_smoke.py -v
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get('SKU_ERP_KIND'),
    reason='no SKU_ERP_KIND configured — live ERP smoke is opt-in, not CI.')


def test_live_discovery_finds_entities():
    from erp_harness.discovery import discover
    from erp_transport import build_live_enforcer

    enforcer = build_live_enforcer()
    surface = discover(enforcer)
    assert surface.entities                      # discovered a real surface
    # the item master should be present (the catalog we ultimately decode)
    names = {e.name.lower() for e in surface.entities}
    assert any('item' in n for n in names)
