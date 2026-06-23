"""ERP Adapter Agent Harness — implementation of docs/ERP_ADAPTER_HARNESS_SPEC.md.

Three phases: recon -> least-privilege manifest (C1); budget-enforced
read-only exploration (C2-C6); typed, probe-verified, human-reviewed Tenant
ERP Profile (C7) consumed by a deterministic adapter, guarded against schema
drift (C8). The proposer is a pluggable Explorer protocol (D9); every
guarantee lives in code and is validated against planted faults on the
synthetic BC twin (D8) in the test matrix.
"""
from erp_harness.adapter import (
    AtomicCatalogRef, SyncedCatalogIndex, UnapprovedProfileError, sync_items,
    sync_items_incremental,
)
from erp_harness.catalog_decode import GrammarReadinessReport, analyze_items
from erp_harness.grammar_induction import (
    Assumption, CatalogGrammarReport, FamilyHypothesis, LLMRoleProposer,
    NoRoleProposer, RoleProposer, SegmentRole, decode_catalog, segment,
    shape_mask,
)
from erp_harness.catalog_source import (
    CatalogSource, ExcelCatalogSource, HtmlCatalogSource, HttpHtmlCatalogSource,
    LineCatalogSource, PdfCatalogSource, rows_from_catalog_lines,
    rows_from_html_tables, rows_from_worksheet,
)
from erp_harness.discovery import discover, surface_fingerprint
from erp_harness.drift import DriftGuard, SyncHalted, acknowledge_drift
from erp_harness.enforcer import (
    BudgetExhausted, Journal, SafetyEnforcer, WriteRefused,
)
from erp_harness.explorer import Explorer, HeuristicExplorer
from erp_harness.gaps import CANONICAL_CONTRACT
from erp_harness.harness import OnboardingFailure, OnboardingResult, run_onboarding
from erp_harness.onboarding import (
    ItemMasterRef, OnboardingReport, decode_catalog_source,
    identify_item_master, run_full_onboarding,
)
from erp_harness.models import (
    AuthExpiredError, ERPClass, ERPDescriptor, GapClass, MappingState,
    MissingGrantError, PermissionsManifest, TenantERPProfile,
    UnsupportedERPError,
)
from erp_harness.transport import TransportTimeout
from erp_harness.profile import ProfileIntegrityError, ReviewGate, render_review_checklist
from erp_harness.recon import generate_manifest, render_markdown
from erp_harness.transport import ManualClock

__all__ = [
    'SyncedCatalogIndex', 'UnapprovedProfileError', 'sync_items',
    'GrammarReadinessReport', 'analyze_items',
    'Assumption', 'CatalogGrammarReport', 'FamilyHypothesis', 'LLMRoleProposer',
    'NoRoleProposer', 'RoleProposer', 'SegmentRole', 'decode_catalog',
    'segment', 'shape_mask',
    'CatalogSource', 'ExcelCatalogSource', 'HtmlCatalogSource',
    'HttpHtmlCatalogSource', 'LineCatalogSource', 'PdfCatalogSource',
    'rows_from_catalog_lines', 'rows_from_html_tables', 'rows_from_worksheet',
    'discover',
    'surface_fingerprint', 'DriftGuard', 'SyncHalted', 'acknowledge_drift',
    'BudgetExhausted', 'Journal', 'SafetyEnforcer', 'WriteRefused',
    'Explorer', 'HeuristicExplorer', 'CANONICAL_CONTRACT',
    'OnboardingFailure', 'OnboardingResult', 'run_onboarding',
    'ItemMasterRef', 'OnboardingReport', 'decode_catalog_source',
    'identify_item_master', 'run_full_onboarding', 'ERPClass',
    'ERPDescriptor', 'GapClass', 'MappingState', 'MissingGrantError',
    'PermissionsManifest', 'TenantERPProfile', 'UnsupportedERPError',
    'ProfileIntegrityError', 'ReviewGate', 'render_review_checklist',
    'generate_manifest', 'render_markdown', 'ManualClock',
]
