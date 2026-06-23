"""Onboarding orchestrator — composes two independent harnesses.

The ERP adapter harness and the catalog decoder are deliberately SEPARATE
harnesses with different jobs, trust models, and failure modes:

  - ERP adapter harness (recon -> discovery -> verified profile -> drift):
    the SCHEMA layer. "Which entity is the item master, which field is the SKU,
    which is the description" — verified by probes against sampled data + a
    human profile-approval gate.
  - Catalog decoder (grammar induction + multi-format ingestion): the SEMANTICS
    layer. "What do these SKU strings MEAN" — verified by SME confirmation of
    proposed assumptions.

They are kept apart because (1) the decoder is source-agnostic and has
standalone pre-sales value — a prospect can drop a PDF/Excel/web catalog before
any ERP connection exists; (2) each component's demonstrate-the-catch guards
stay sharp when its failure modes aren't entangled with the other's; (3) the
contract between them is narrow: discovery names the item master + SKU field;
the decoder consumes rows and emits a grammar report + SME questions.

This module is the thin "one onboarding agent" workflow that sequences them:

  ERP path:   recon/discovery/verify (harness)
              -> identify item master from the VERIFIED mapping (not hardcoded)
              -> decode that entity's rows (decoder, induction)
              -> one combined human-review/SME queue

  Standalone: a CatalogSource (PDF/Excel/web) -> decode. No ERP required.

Both honor the same spine: agent proposes, code verifies, human gates.
"""
from __future__ import annotations

from dataclasses import dataclass

from erp_harness.catalog_source import CatalogSource
from erp_harness.discovery import fetch_all_rows
from erp_harness.gaps import CANONICAL_CONTRACT
from erp_harness.grammar_induction import (
    CatalogGrammarReport, RoleProposer, decode_catalog,
)
from erp_harness.harness import OnboardingResult, run_onboarding
from erp_harness.models import ContractField, ERPDescriptor, TenantERPProfile


@dataclass(frozen=True)
class ItemMasterRef:
    """Where the SKUs live, derived from the harness's verified mappings."""
    entity: str
    sku_field: str
    description_field: str | None


@dataclass(frozen=True)
class OnboardingReport:
    erp_result: OnboardingResult
    item_master: ItemMasterRef | None
    grammar: CatalogGrammarReport | None
    review_queue: tuple[str, ...]   # combined human-review/SME items, ranked


def identify_item_master(profile: TenantERPProfile) -> ItemMasterRef | None:
    """Read the verified mappings for the canonical 'sku'/'description' contract
    fields to learn which discovered entity+fields hold the item master. Returns
    None if the SKU field was never verified (the decoder can't run blind)."""
    verified = {m.mapping.contract_field: m.mapping
                for m in profile.verified_mappings()}
    sku = verified.get('sku')
    if sku is None:
        return None
    desc = verified.get('description')
    return ItemMasterRef(
        entity=sku.entity, sku_field=sku.source_field,
        description_field=desc.source_field if desc else None)


def _review_queue(report: CatalogGrammarReport | None,
                  result: OnboardingResult) -> tuple[str, ...]:
    """One ranked queue a human works through: profile gaps first (they block
    sync), then the decoder's SME questions (they improve resolution)."""
    items: list[str] = []
    for g in result.profile.gaps:
        items.append(f'[profile gap] {g.contract_field}: {g.gap_class.value} '
                     f'— needs remediation before sync')
    if report:
        for q in report.sme_questions:
            items.append(f'[catalog SME] {q.question}')
    return tuple(items)


def run_full_onboarding(erp: ERPDescriptor, enforcer, explorer, clock, *,
                        contract: tuple[ContractField, ...] = CANONICAL_CONTRACT,
                        role_proposer: RoleProposer | None = None,
                        item_limit: int = 5000) -> OnboardingReport:
    """The full onboarding workflow: run the ERP harness, then point the catalog
    decoder at the verified item master using the DISCOVERED field names."""
    result = run_onboarding(erp, enforcer, explorer, clock, contract=contract)
    item_master = identify_item_master(result.profile)

    grammar = None
    if item_master is not None:
        rows = fetch_all_rows(enforcer, item_master.entity, limit=item_limit)
        grammar = decode_catalog(
            rows, sku_field=item_master.sku_field,
            description_field=item_master.description_field or item_master.sku_field,
            role_proposer=role_proposer)

    return OnboardingReport(erp_result=result, item_master=item_master,
                            grammar=grammar,
                            review_queue=_review_queue(grammar, result))


def decode_catalog_source(source: CatalogSource, *,
                          evidence_fields: list[str] | None = ('fitment',
                                                               'section', 'oem'),
                          role_proposer: RoleProposer | None = None
                          ) -> CatalogGrammarReport:
    """Standalone catalog decode — no ERP. A PDF/Excel/web catalog straight into
    the grammar decoder (the pre-sales / 'send us your catalog' path).

    By default it correlates segments against the fitment / section / OEM
    columns the ingestion layer captures (so a classifier segment like an
    engine-line or category code resolves itself); fields a given source doesn't
    provide are simply absent and ignored."""
    return decode_catalog(source.rows(), sku_field='sku',
                          description_field='description',
                          evidence_fields=list(evidence_fields or []),
                          role_proposer=role_proposer)
