"""C8 — drift guard: determinism, halt-with-named-diff, acknowledge -> v+1
with approval RESET (the re-review is the feature), and no false halts.
"""
from __future__ import annotations

import pytest

from erp_harness import DriftGuard, ReviewGate, SyncHalted, acknowledge_drift
from erp_harness.discovery import crawl_metadata, surface_fingerprint
from erp_twin import faults
from harness_fixtures import make_rig, onboard


def _guard_and_rig(**kw):
    clock, twin, enforcer = make_rig(**kw)
    baseline = crawl_metadata(enforcer)
    return clock, twin, enforcer, DriftGuard(baseline,
                                             surface_fingerprint(baseline))


# --- smoke ----------------------------------------------------------------------

def test_fingerprint_determinism_two_runs_identical():
    _, _, e1, _ = _guard_and_rig(item_limit=50)
    _, _, e2, _ = _guard_and_rig(item_limit=50)
    assert (surface_fingerprint(crawl_metadata(e1))
            == surface_fingerprint(crawl_metadata(e2)))


def test_no_false_halts_across_unchanged_cycles():
    _, _, enforcer, guard = _guard_and_rig(item_limit=50)
    for _ in range(5):
        report = guard.check(enforcer)
        assert not report.drifted and not report.changes


# --- E2E behavioral ------------------------------------------------------------------

def test_e2e_rename_halts_with_named_diff_then_ack_resumes_at_v_plus_1():
    clock, twin, enforcer, guard = _guard_and_rig(item_limit=100)
    _, _, _, result = onboard(item_limit=100)
    profile = ReviewGate.approve(result.profile, reviewer='sme',
                                 reason='baseline approval')
    assert profile.profile_version == 1

    faults.rename_field(twin, 'items', 'inventoryQty', 'qtyAvailable')

    with pytest.raises(SyncHalted) as exc:
        guard.check_or_halt(enforcer)
    msg = str(exc.value)
    assert 'removed field items.inventoryQty' in msg
    assert 'added field items.qtyAvailable' in msg

    report = guard.check(enforcer)
    bumped = acknowledge_drift(profile, report, reviewer='sme',
                               reason='tenant renamed qty column upstream')
    assert bumped.profile_version == 2
    assert bumped.fingerprint == report.current_fingerprint
    assert bumped.approval is None, \
        'acknowledgment must RESET approval — re-review is mandatory'

    # Re-approved v2 passes a fresh guard built on the new baseline.
    new_guard = DriftGuard(crawl_metadata(enforcer), bumped.fingerprint)
    assert not new_guard.check(enforcer).drifted


def test_e2e_acknowledge_requires_drift_and_named_reviewer():
    _, _, enforcer, guard = _guard_and_rig(item_limit=50)
    _, _, _, result = onboard(item_limit=50)
    clean = guard.check(enforcer)
    with pytest.raises(ValueError, match='no drift'):
        acknowledge_drift(result.profile, clean, reviewer='sme', reason='x')
