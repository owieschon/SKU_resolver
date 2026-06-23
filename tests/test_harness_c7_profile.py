"""C7 — profile state machine + the planted-fault test.

The planted-fault test: an adversarial explorer deliberately proposes a WRONG
mapping (description as the SKU identifier). The verification probe must
catch it, the profile must preserve it as REJECTED evidence (never silently
drop it), and the review checklist must render the rejection.
"""
from __future__ import annotations

import pytest

from erp_harness import (
    CANONICAL_CONTRACT, ERPClass, ERPDescriptor, MappingState,
    ProfileIntegrityError, ReviewGate, render_review_checklist,
)
from erp_harness.models import (
    MappingRecord, ProposedMapping, SurfaceProfile, VerificationEvidence,
)
from erp_harness.profile import build_profile
from erp_harness.verification import verify_mapping
from harness_fixtures import BC, make_rig, onboard


def _proposal(field='sku', entity='items', source='number', by='test'):
    return ProposedMapping(contract_field=field, entity=entity,
                           source_field=source, rationale='r', proposed_by=by)


def _evidence(ok=True):
    return VerificationEvidence(sampled=50, checks={'c': ok}, detail='d')


# --- smoke: the state machine rejects illegal profiles ------------------------------

def test_profile_rejects_unresolved_proposals():
    rec = MappingRecord(_proposal(), MappingState.PROPOSED, None)
    with pytest.raises(ProfileIntegrityError, match='unresolved proposal'):
        build_profile(erp=BC, records=[rec], gaps=[], fingerprint='f' * 16,
                      rate_budget_per_minute=300, probe_findings={})


def test_profile_rejects_verified_without_passing_evidence():
    rec = MappingRecord(_proposal(), MappingState.VERIFIED, _evidence(ok=False))
    with pytest.raises(ProfileIntegrityError, match='failing evidence'):
        build_profile(erp=BC, records=[rec], gaps=[], fingerprint='f' * 16,
                      rate_budget_per_minute=300, probe_findings={})


def test_review_gate_requires_named_reviewer_and_reason():
    rec = MappingRecord(_proposal(), MappingState.VERIFIED, _evidence())
    profile = build_profile(erp=BC, records=[rec], gaps=[],
                            fingerprint='f' * 16, rate_budget_per_minute=300,
                            probe_findings={})
    with pytest.raises(ProfileIntegrityError):
        ReviewGate.approve(profile, reviewer='', reason='x')
    approved = ReviewGate.approve(profile, reviewer='sme',
                                  reason='mappings read correctly')
    assert approved.approval.approved
    rejected = ReviewGate.reject(profile, reviewer='sme',
                                 reason='qty mapping looks wrong')
    assert rejected.approval is not None and not rejected.approval.approved


def test_known_bad_mapping_rejected_by_probe():
    _, _, enforcer = make_rig(item_limit=100)
    bad = _proposal(field='quantity_on_hand', source='displayName')  # text as number
    cf = next(c for c in CANONICAL_CONTRACT if c.name == 'quantity_on_hand')
    rec = verify_mapping(enforcer, bad, cf)
    assert rec.state is MappingState.REJECTED
    assert not rec.evidence.checks['numeric_99']


def test_long_sku_tenant_verifies_correctly():
    """R0 #3: the identifier ceiling is tenant-relative, so a tenant whose
    part numbers are genuinely long (28 chars) still verifies — while a long
    description on the same entity is still rejected as the key. A hardcoded
    30-char ceiling would have wrongly rejected the real long SKU."""
    from erp_harness.verification import verify_mapping as vm
    from erp_harness.transport import ManualClock, TransportRequest, TransportResponse

    class _LongSkuBackend:
        # SKUs ~28 chars (unique), descriptions ~80 chars.
        def __init__(self):
            self.rows = [{'partNo': f'LONGFORM-PART-NUMBER-{i:07d}',
                          'desc': 'A very long free-text marketing description '
                                  f'for product number {i} with extra words'}
                         for i in range(60)]
        def handle(self, req: TransportRequest) -> TransportResponse:
            return TransportResponse(status=200, json={'value': self.rows})

    from erp_harness import SafetyEnforcer
    enf = SafetyEnforcer(_LongSkuBackend(), ManualClock(),
                         rate_per_minute=1000, total_call_budget=50)
    cf = next(c for c in CANONICAL_CONTRACT if c.name == 'sku')

    good = _proposal(field='sku', entity='parts', source='partNo')
    assert vm(enf, good, cf).state is MappingState.VERIFIED   # 28-char SKU OK

    bad = _proposal(field='sku', entity='parts', source='desc')
    assert vm(enf, bad, cf).state is MappingState.REJECTED    # 80-char desc not


# --- E2E behavioral: the adversarial proposer is caught and preserved ----------------

class AdversarialMappingExplorer:
    """Proposes the wrong-but-plausible mapping class: free-text description
    masquerading as the SKU identifier."""
    name = 'adversarial_v1'

    def propose(self, surface: SurfaceProfile, contract):
        return [ProposedMapping(
            contract_field='sku', entity='items', source_field='displayName',
            rationale='displayName uniquely identifies items (CLAIMED)',
            proposed_by=self.name)]


def test_e2e_adversarial_wrong_mapping_caught_and_preserved_as_evidence():
    from erp_harness import run_onboarding
    clock, twin, enforcer = make_rig(item_limit=200)
    result = run_onboarding(BC, enforcer, AdversarialMappingExplorer(), clock)

    rejected = result.profile.rejected_mappings()
    assert len(rejected) == 1, 'the wrong mapping must be PRESERVED, not dropped'
    rec = rejected[0]
    assert rec.mapping.source_field == 'displayName'
    assert rec.mapping.proposed_by == 'adversarial_v1'      # provenance kept
    failed = [k for k, v in rec.evidence.checks.items() if not v]
    assert 'short_relative' in failed or 'unique_99' in failed   # the catch, named

    # 'sku' must NOT be verified — it lands in gaps instead (totality holds).
    assert 'sku' not in {m.mapping.contract_field
                         for m in result.profile.verified_mappings()}
    assert 'sku' in {g.contract_field for g in result.profile.gaps}

    # And the review checklist renders the rejection for human eyes.
    checklist = render_review_checklist(result.profile)
    assert 'Rejected proposals' in checklist
    assert 'displayName' in checklist
