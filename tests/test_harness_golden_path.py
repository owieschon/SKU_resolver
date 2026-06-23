"""Whole-system E2E: the golden path, the unapproved-profile refusal, the
rubber-stamp mitigation, and the destination-side zero-write proof.

Acceptance criterion (spec §4): not 'a profile was produced' but THE SKU
TRANSLATOR'S IDENTITY GUARANTEE HOLDS AGAINST THE ADAPTER-SYNCED CATALOG.
Onboarding is done when the downstream guarantee survives the new data path.
"""
from __future__ import annotations

import pytest
from harness_fixtures import BC, make_rig

from erp_harness import (
    DriftGuard,
    HeuristicExplorer,
    ReviewGate,
    SyncHalted,
    UnapprovedProfileError,
    run_onboarding,
    sync_items,
)
from erp_twin import faults

ITEMS = 300


def _onboarded():
    clock, twin, enforcer = make_rig(item_limit=ITEMS, total_call_budget=900)
    result = run_onboarding(BC, enforcer, HeuristicExplorer(), clock)
    return clock, twin, enforcer, result


def test_e2e_golden_path_translator_identity_holds_on_synced_catalog():
    clock, twin, enforcer, result = _onboarded()

    # Review gate: explicit, named approval.
    profile = ReviewGate.approve(result.profile, reviewer='sme',
                                 reason='all mappings verified; gaps acceptable')

    # Deterministic adapter syncs items through verified mappings.
    guard = DriftGuard(result.surface.entities, result.surface.fingerprint)
    synced = sync_items(profile, enforcer, tenant_id='tenant_001_via_adapter',
                        drift_guard=guard)
    assert synced.size() == ITEMS

    # THE acceptance criterion: identity through the full translator,
    # against adapter-synced data, for every synced SKU.
    from sku_translator import RESOLVED, InMemoryStore, translate
    mem = InMemoryStore()
    misses = []
    for sku in synced.all_skus():
        r = translate(sku, catalog=synced, memory=mem)
        if r.state != RESOLVED or r.sku != sku:
            misses.append((sku, r.state, r.sku))
    assert not misses, f'identity broke on synced catalog: {misses[:5]}'

    # Destination-side proof across the ENTIRE run: zero writes ever arrived.
    assert twin.write_attempts() == []


def test_e2e_unapproved_profile_cannot_sync():
    clock, twin, enforcer, result = _onboarded()
    with pytest.raises(UnapprovedProfileError, match='review gate'):
        sync_items(result.profile, enforcer, tenant_id='x')   # no approval


def test_e2e_rejected_profile_cannot_sync_rubber_stamp_mitigation():
    """Spec §7: the CI fixture set must include a profile that SHOULD be
    rejected — and the test asserts rejection blocks sync. Approval that
    cannot say no is not a gate."""
    clock, twin, enforcer, result = _onboarded()
    rejected = ReviewGate.reject(result.profile, reviewer='sme',
                                 reason='probe findings show throttle too '
                                        'low for production sync cadence')
    with pytest.raises(UnapprovedProfileError):
        sync_items(rejected, enforcer, tenant_id='x')


def test_e2e_drift_between_approval_and_sync_halts_before_any_read():
    clock, twin, enforcer, result = _onboarded()
    profile = ReviewGate.approve(result.profile, reviewer='sme', reason='ok')
    guard = DriftGuard(result.surface.entities, result.surface.fingerprint)

    faults.add_custom_field(twin, 'items', 'GRX_NewField', field_number=50002)

    with pytest.raises(SyncHalted, match='GRX_NewField'):
        sync_items(profile, enforcer, tenant_id='x', drift_guard=guard)
