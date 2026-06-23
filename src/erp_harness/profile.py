"""C7 — the Tenant ERP Profile and its review gate.

The profile is the contract between probabilistic discovery and binding
execution — the PartSpec of onboarding. Construction enforces the state
machine (a consumable profile cannot contain an unverified mapping by
construction); the review gate makes human approval an explicit, recorded
artifact with no auto-approve path.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from erp_harness.models import (
    Approval,
    ERPDescriptor,
    MappingRecord,
    MappingState,
    NamedGap,
    TenantERPProfile,
)


class ProfileIntegrityError(Exception):
    pass


def build_profile(*, erp: ERPDescriptor, records: list[MappingRecord],
                  gaps: list[NamedGap], fingerprint: str,
                  rate_budget_per_minute: int,
                  probe_findings: Mapping[str, Any],
                  version: int = 1) -> TenantERPProfile:
    """Validates the C7 invariants at construction time:
    every record carries evidence; no record is left PROPOSED (the
    orchestrator must resolve every proposal through verification)."""
    for r in records:
        if r.state is MappingState.PROPOSED:
            raise ProfileIntegrityError(
                f'unresolved proposal in profile: {r.mapping.contract_field} '
                f'(every proposal must pass through verification)')
        if r.evidence is None:
            raise ProfileIntegrityError(
                f'{r.mapping.contract_field}: state {r.state.value} without '
                f'verification evidence')
        if r.state is MappingState.VERIFIED and not r.evidence.passed:
            raise ProfileIntegrityError(
                f'{r.mapping.contract_field}: VERIFIED with failing evidence')
    return TenantERPProfile(
        profile_version=version, erp=erp, mappings=tuple(records),
        gaps=tuple(gaps), fingerprint=fingerprint,
        rate_budget_per_minute=rate_budget_per_minute,
        probe_findings=dict(probe_findings), approval=None)


def render_review_checklist(profile: TenantERPProfile) -> str:
    """The human-review artifact: every mapping, every gap, every probe stat
    as a reviewable checklist. Plain language by DoD."""
    lines = [f'# Tenant ERP Profile v{profile.profile_version} — review',
             '', f'ERP: {profile.erp.erp_class.value} '
                 f'({profile.erp.product_version})',
             f'Surface fingerprint: `{profile.fingerprint}`', '',
             '## Verified mappings', '',
             '| Canonical field | Source | Evidence |', '|---|---|---|']
    for r in profile.verified_mappings():
        lines.append(f'| {r.mapping.contract_field} | '
                     f'`{r.mapping.entity}.{r.mapping.source_field}` | '
                     f'{r.evidence.detail}; all checks passed |')
    lines += ['', '## Rejected proposals (preserved as evidence)', '']
    if profile.rejected_mappings():
        lines += ['| Canonical field | Source | Failing checks |', '|---|---|---|']
        for r in profile.rejected_mappings():
            failed = [k for k, v in r.evidence.checks.items() if not v]
            lines.append(f'| {r.mapping.contract_field} | '
                         f'`{r.mapping.entity}.{r.mapping.source_field}` | '
                         f'{", ".join(failed)} |')
    else:
        lines.append('(none)')
    lines += ['', '## Named gaps', '']
    for g in profile.gaps:
        lines.append(f'- **{g.contract_field}** — `{g.gap_class.value}`: '
                     f'{g.detail}')
    lines += ['', '## Probe findings', '']
    for k, v in profile.probe_findings.items():
        lines.append(f'- {k}: {v}')
    lines += ['', '---',
              'Approve only if every verified mapping reads correctly and '
              'every gap has an acceptable remediation path.', '']
    return '\n'.join(lines)


class ReviewGate:
    """Approval is explicit, named, and recorded. There is no auto-approve
    path: an unapproved profile is unusable by the adapter (adapter.py
    refuses it), and rejection is itself a recorded artifact."""

    @staticmethod
    def approve(profile: TenantERPProfile, *, reviewer: str,
                reason: str) -> TenantERPProfile:
        if not reviewer or not reason:
            raise ProfileIntegrityError('approval requires a named reviewer '
                                        'and a stated reason')
        return replace(profile, approval=Approval(reviewer=reviewer,
                                                  reason=reason,
                                                  approved=True))

    @staticmethod
    def reject(profile: TenantERPProfile, *, reviewer: str,
               reason: str) -> TenantERPProfile:
        if not reviewer or not reason:
            raise ProfileIntegrityError('rejection requires a named reviewer '
                                        'and a stated reason')
        return replace(profile, approval=Approval(reviewer=reviewer,
                                                  reason=reason,
                                                  approved=False))
