"""Typed domain model for the ERP adapter harness.

Everything that crosses a component boundary is a frozen dataclass defined
here. The profile's mapping lifecycle (proposed -> verified | rejected) is a
real state machine enforced in profile.py — these types make illegal states
unrepresentable where Python allows it, and loudly rejected where it doesn't.

Spec: docs/ERP_ADAPTER_HARNESS_SPEC.md (components C1, C3, C6, C7, C8).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

# --- C1: recon ---------------------------------------------------------------

class ERPClass(Enum):
    BC_SAAS = 'bc_saas'           # Business Central SaaS (OAuth/Entra, OData v4)
    NAV_ONPREM = 'nav_onprem'     # NAV on-prem (read-only SQL user)
    P21 = 'p21'                   # unsupported v1 (no instance access)
    ECLIPSE = 'eclipse'           # unsupported v1 (no instance access)


@dataclass(frozen=True)
class ERPDescriptor:
    erp_class: ERPClass
    product_version: str          # e.g. 'BC 24', 'NAV 2018'
    deployment: str               # 'saas' | 'onprem'
    endpoint_hint: str | None = None


@dataclass(frozen=True)
class Grant:
    """One least-privilege grant request. `object_name` is the surface unit
    the twin/tenant enforces (entity name or 'metadata'); `scope` is the
    IT-facing grant string; `why` is the one-line justification."""
    object_name: str
    scope: str
    why: str
    access: str = 'read'          # exploration manifests are read-only by DoD


@dataclass(frozen=True)
class PermissionsManifest:
    erp: ERPDescriptor
    grants: tuple[Grant, ...]
    not_requested: tuple[str, ...]  # explicit statement of what we do NOT ask for
    manifest_version: str = '1'


# --- C3: surface discovery ----------------------------------------------------

@dataclass(frozen=True)
class FieldSchema:
    name: str
    edm_type: str                 # 'Edm.String', 'Edm.Decimal', ...
    nullable: bool
    field_number: int | None      # NAV/BC field number; >= 50000 => tenant custom
    is_custom: bool


@dataclass(frozen=True)
class EntitySchema:
    name: str
    fields: tuple[FieldSchema, ...]
    nav_properties: tuple[str, ...] = ()

    def field(self, name: str) -> FieldSchema | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True)
class FieldProfile:
    """Empirical per-field stats from sampled rows — evidence, not assertion."""
    entity: str
    name: str
    sampled: int
    null_rate: float
    distinct_ratio: float
    p95_len: int | None           # strings only
    sample_values: tuple[str, ...]


@dataclass(frozen=True)
class SurfaceProfile:
    entities: tuple[EntitySchema, ...]
    field_profiles: tuple[FieldProfile, ...]
    fingerprint: str              # C8 baseline — content hash of the granted surface

    def entity(self, name: str) -> EntitySchema | None:
        return next((e for e in self.entities if e.name == name), None)

    def profile_for(self, entity: str, fld: str) -> FieldProfile | None:
        return next((p for p in self.field_profiles
                     if p.entity == entity and p.name == fld), None)


# --- C6: canonical contract + gaps --------------------------------------------

@dataclass(frozen=True)
class ContractField:
    """One field the downstream canonical layer requires, with the value-shape
    the verification probe will hold a proposed mapping against."""
    name: str                     # canonical name, e.g. 'sku'
    entity_role: str              # 'item' | 'customer' | 'sales_order'
    kind: str                     # 'identifier' | 'text' | 'number' | 'date' | 'flag'
    required: bool


class GapClass(Enum):
    CUSTOM_API_PAGE_REQUIRED = 'custom_api_page_required'
    ALTERNATIVE_ENTITY = 'alternative_entity'
    PERMISSION_GAP = 'permission_gap'
    UNAVAILABLE = 'unavailable'


@dataclass(frozen=True)
class NamedGap:
    contract_field: str
    gap_class: GapClass
    detail: str


# --- C7: profile --------------------------------------------------------------

class MappingState(Enum):
    PROPOSED = 'proposed'
    VERIFIED = 'verified'
    REJECTED = 'rejected'


@dataclass(frozen=True)
class ProposedMapping:
    contract_field: str
    entity: str
    source_field: str
    rationale: str                # the explorer's claim — hearsay until probed
    proposed_by: str              # explorer implementation name (provenance)


@dataclass(frozen=True)
class VerificationEvidence:
    sampled: int
    checks: Mapping[str, bool]    # check name -> pass
    detail: str

    @property
    def passed(self) -> bool:
        return all(self.checks.values()) and self.sampled > 0


@dataclass(frozen=True)
class MappingRecord:
    mapping: ProposedMapping
    state: MappingState
    evidence: VerificationEvidence | None  # present iff state != PROPOSED


@dataclass(frozen=True)
class Approval:
    reviewer: str
    reason: str
    approved: bool                # an explicit rejection is also an Approval record


@dataclass(frozen=True)
class TenantERPProfile:
    profile_version: int
    erp: ERPDescriptor
    mappings: tuple[MappingRecord, ...]
    gaps: tuple[NamedGap, ...]
    fingerprint: str
    rate_budget_per_minute: int
    probe_findings: Mapping[str, Any]
    approval: Approval | None = None

    def verified_mappings(self) -> tuple[MappingRecord, ...]:
        return tuple(m for m in self.mappings if m.state is MappingState.VERIFIED)

    def rejected_mappings(self) -> tuple[MappingRecord, ...]:
        return tuple(m for m in self.mappings if m.state is MappingState.REJECTED)


# --- C8: drift ----------------------------------------------------------------

@dataclass(frozen=True)
class DriftReport:
    drifted: bool
    baseline_fingerprint: str
    current_fingerprint: str
    changes: tuple[str, ...]      # human-readable, names the exact change


# --- harness-level errors -----------------------------------------------------

class HarnessError(Exception):
    """Base class. Every harness failure is typed and named."""


class MissingGrantError(HarnessError):
    def __init__(self, object_name: str):
        self.object_name = object_name
        super().__init__(f'missing grant: {object_name!r} '
                         f'(present in manifest; not granted or revoked)')


class UnsupportedERPError(HarnessError):
    pass


class InvariantViolation(HarnessError):
    """A critical harness invariant was violated (least-privilege
    manifest, contract totality). These are explicit raises, NOT asserts —
    asserts are stripped under `python -O`, which would silently void the
    guarantee (R0 #2)."""


class DiscoveryError(HarnessError):
    """A discovery request returned an unexpected (non-200, non-403) status."""


class AuthExpiredError(HarnessError):
    """A 401 that auth refresh could not recover (R2 #9). Distinct from
    MissingGrantError (403 = never granted): a 401 means credentials that
    once worked have expired or been revoked mid-run, a transient-auth
    condition, not a permission gap."""
