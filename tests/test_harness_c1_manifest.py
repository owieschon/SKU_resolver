"""C1 — manifest smoke + the sufficiency/minimality E2E pair.

Sufficiency: Phase B completes using exactly the manifest's grants.
Minimality: revoking ANY single grant produces a detected, NAMED failure —
either a hard OnboardingFailure naming the grant, or a permission_gap in the
profile naming the entity. Never a clean, full-coverage run.
"""
from __future__ import annotations

import pytest
from harness_fixtures import BC, make_rig

from erp_harness import (
    ERPClass,
    ERPDescriptor,
    GapClass,
    HeuristicExplorer,
    OnboardingFailure,
    UnsupportedERPError,
    generate_manifest,
    render_markdown,
    run_onboarding,
)

_WRITE_WORDS = ('write', 'create', 'modify', 'delete', 'admin', 'full')


# --- smoke -------------------------------------------------------------------

def test_manifest_is_deterministic_and_read_only():
    m1, m2 = generate_manifest(BC), generate_manifest(BC)
    assert m1 == m2
    for g in m1.grants:
        assert g.access == 'read'
        assert not any(w in g.scope.lower() for w in _WRITE_WORDS)
    assert m1.not_requested   # the explicit NOT-requested statement exists


def test_manifest_markdown_renders_every_grant_with_why():
    m = generate_manifest(BC)
    md = render_markdown(m)
    for g in m.grants:
        assert g.object_name in md and g.why in md
    assert 'NOT requested' in md


def test_unsupported_erp_classes_fail_loudly_with_reason():
    for cls in (ERPClass.P21, ERPClass.ECLIPSE):
        with pytest.raises(UnsupportedERPError, match='not supported in v1'):
            generate_manifest(ERPDescriptor(cls, 'x', 'onprem'))


# --- E2E behavioral ------------------------------------------------------------

def test_e2e_sufficiency_manifest_grants_complete_phase_b():
    manifest = generate_manifest(BC)
    granted = {g.object_name for g in manifest.grants}
    clock, twin, enforcer = make_rig(granted=granted)
    result = run_onboarding(BC, enforcer, HeuristicExplorer(), clock)
    assert result.profile.verified_mappings()   # exploration genuinely ran


def test_e2e_minimality_every_grant_is_load_bearing():
    manifest = generate_manifest(BC)
    granted = {g.object_name for g in manifest.grants}
    for grant in sorted(granted):
        clock, twin, enforcer = make_rig(granted=granted - {grant})
        try:
            result = run_onboarding(BC, enforcer, HeuristicExplorer(), clock)
        except OnboardingFailure as f:
            assert grant in f.cause, (grant, f.cause)   # named hard failure
            continue
        gap_names = [g for g in result.profile.gaps
                     if g.gap_class is GapClass.PERMISSION_GAP
                     and grant in g.detail]
        probe_names = [k for k, v in result.probe_findings.items()
                       if isinstance(v, dict) and grant in str(v.get('error', ''))]
        assert gap_names or probe_names, (
            f'revoking {grant!r} produced no NAMED detection '
            f'(gaps={result.profile.gaps}, probes={result.probe_findings}) '
            f'— silent degradation')
