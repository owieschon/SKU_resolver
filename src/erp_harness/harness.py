"""The orchestrator: Phase A -> B -> C wired with the totality invariant.

run_onboarding() is deliberately boring — the elegance budget was spent on
the components. Its one piece of real logic is the totality reconciliation:
every contract field ends as exactly one of (verified mapping | named gap),
checked mechanically before the profile is built. Hard failures (missing
metadata grant, budget exhaustion) surface as typed OnboardingFailure with
the cause named — never a silent partial success.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from erp_harness.catalog_decode import GrammarReadinessReport, analyze_items
from erp_harness.discovery import discover, fetch_all_rows
from erp_harness.enforcer import BudgetExhausted, SafetyEnforcer
from erp_harness.explorer import Explorer
from erp_harness.gaps import CANONICAL_CONTRACT, classify_unmapped
from erp_harness.models import (
    ContractField, ERPDescriptor, InvariantViolation, MappingState,
    MissingGrantError, PermissionsManifest, SurfaceProfile, TenantERPProfile,
)
from erp_harness.probes import run_all as run_probes
from erp_harness.profile import build_profile
from erp_harness.recon import generate_manifest
from erp_harness.verification import verify_mapping

from observability import set_attr, tracer


@dataclass(frozen=True)
class OnboardingResult:
    manifest: PermissionsManifest
    surface: SurfaceProfile
    profile: TenantERPProfile           # unapproved — the review gate is the caller's job
    grammar_report: GrammarReadinessReport | None
    probe_findings: Mapping[str, Any]


@dataclass(frozen=True)
class OnboardingFailure(Exception):
    cause: str                          # names the grant/budget that failed
    phase: str

    def __str__(self) -> str:
        return f'onboarding failed in {self.phase}: {self.cause}'


def run_onboarding(erp: ERPDescriptor, enforcer: SafetyEnforcer,
                   explorer: Explorer, clock, *,
                   contract: tuple[ContractField, ...] = CANONICAL_CONTRACT,
                   ) -> OnboardingResult:
    manifest = generate_manifest(erp)                      # Phase A

    try:                                                   # Phase B
        with tracer.start_as_current_span('onboard.discovery') as sp:
            set_attr(sp, 'svc.task', 'erp_onboard')
            set_attr(sp, 'svc.phase', 'discovery')
            surface = discover(enforcer)
            set_attr(sp, 'svc.entity_count', len(surface.entities))
    except MissingGrantError as e:
        raise OnboardingFailure(cause=e.object_name, phase='discovery') from e
    except BudgetExhausted as e:
        raise OnboardingFailure(cause=str(e), phase='discovery') from e

    granted_objects = {e.name for e in surface.entities} | {'metadata'}

    grammar_report = None
    if surface.entity('items') is not None:
        item_rows = fetch_all_rows(enforcer, 'items')
        grammar_report = analyze_items(item_rows, sku_field='number',
                                       description_field='displayName')

    probe_findings = run_probes(enforcer, clock)

    with tracer.start_as_current_span('onboard.verify') as sp:  # Phase C
        set_attr(sp, 'svc.phase', 'verify')
        proposals = explorer.propose(surface, contract)
        records = [verify_mapping(enforcer, p,
                                  next(c for c in contract
                                       if c.name == p.contract_field))
                   for p in proposals]
        set_attr(sp, 'svc.proposed', len(proposals))
        set_attr(sp, 'svc.verified',
                 sum(1 for r in records if r.state is MappingState.VERIFIED))

    verified_fields = {r.mapping.contract_field for r in records
                       if r.state is MappingState.VERIFIED}
    gaps = [classify_unmapped(cf, erp_class=erp.erp_class, surface=surface,
                              granted_objects=granted_objects)
            for cf in contract if cf.name not in verified_fields]

    # Totality: mapped ∪ gapped must equal the contract, exactly. Explicit
    # raise, not assert — this guarantee must survive `python -O` (R0 #2).
    covered = verified_fields | {g.contract_field for g in gaps}
    contract_names = {c.name for c in contract}
    if covered != contract_names:
        raise InvariantViolation(
            f'contract totality violated: uncovered={contract_names - covered}, '
            f'phantom={covered - contract_names}')

    profile = build_profile(
        erp=erp, records=records, gaps=gaps,
        fingerprint=surface.fingerprint,
        rate_budget_per_minute=enforcer._rate,
        probe_findings=probe_findings)

    return OnboardingResult(manifest=manifest, surface=surface,
                            profile=profile, grammar_report=grammar_report,
                            probe_findings=probe_findings)
