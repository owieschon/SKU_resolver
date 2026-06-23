"""The pluggable proposer (decision D9).

`Explorer` is the seam where intelligence plugs in: it reads the discovered
surface and PROPOSES mappings to the canonical contract. Proposals are
hearsay — nothing an Explorer says is trusted until C7's deterministic
verification probes confirm it against sampled data. Because every guarantee
lives outside this seam, the implementation can be:

  - HeuristicExplorer (below): deterministic name/type matching — what CI
    runs; fully functional for BC-shaped surfaces
  - An LLM-backed explorer: drop-in for messy real tenants, no guarantee
    changes required
  - AdversarialExplorer (tests/): deliberately proposes wrong mappings and
    attempts writes — the spec's planted-fault tests (C2/C7 E2E) run against it

This is the same division of labor the resolution service enforces: the
model is a good chooser and a dangerous author; here it cannot author at
all — only nominate.
"""
from __future__ import annotations

from typing import Protocol

from erp_harness.gaps import ROLE_OBJECTS
from erp_harness.models import ContractField, ProposedMapping, SurfaceProfile

# Synonym table: canonical contract field -> source-field name candidates
# (normalized lowercase, order = preference).
_SYNONYMS: dict[str, tuple[str, ...]] = {
    'sku': ('number', 'no', 'itemno', 'code', 'itemcode'),
    'description': ('displayname', 'description', 'desc', 'name'),
    'quantity_on_hand': ('inventoryqty', 'inventory', 'quantityonhand',
                         'qtyonhand', 'qty'),
    'is_blocked': ('blocked', 'inactive', 'isblocked'),
    'row_modified_at': ('lastmodifieddatetime', 'modifiedat', 'lastmodified'),
    'customer_id': ('number', 'no', 'customernumber', 'custno'),
    'customer_name': ('displayname', 'name', 'customername'),
    'order_id': ('number', 'no', 'ordernumber', 'orderno'),
    'order_date': ('orderdate', 'documentdate', 'date'),
}


class Explorer(Protocol):
    name: str
    def propose(self, surface: SurfaceProfile,
                contract: tuple[ContractField, ...]) -> list[ProposedMapping]: ...


class HeuristicExplorer:
    """Deterministic proposer: normalized-name synonym matching scoped to the
    contract field's entity role. No I/O, no model calls."""

    name = 'heuristic_v1'

    def propose(self, surface: SurfaceProfile,
                contract: tuple[ContractField, ...]) -> list[ProposedMapping]:
        out: list[ProposedMapping] = []
        for cf in contract:
            entity = surface.entity(ROLE_OBJECTS.get(cf.entity_role, ''))
            if entity is None:
                continue   # gap detector's territory, not ours
            by_norm = {f.name.lower().replace('_', ''): f.name
                       for f in entity.fields}
            for candidate in _SYNONYMS.get(cf.name, ()):
                if candidate in by_norm:
                    out.append(ProposedMapping(
                        contract_field=cf.name,
                        entity=entity.name,
                        source_field=by_norm[candidate],
                        rationale=f'name match {candidate!r} on '
                                  f'{entity.name} (synonym table)',
                        proposed_by=self.name,
                    ))
                    break
        return out


_MAP_SCHEMA = {
    'type': 'object',
    'properties': {
        'mappings': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'contract_field': {'type': 'string'},
                'entity': {'type': 'string'},
                'source_field': {'type': 'string'},
                'rationale': {'type': 'string'},
            },
            'required': ['contract_field', 'entity', 'source_field', 'rationale'],
            'additionalProperties': False}},
    },
    'required': ['mappings'],
    'additionalProperties': False,
}


class LLMExplorer:
    """Production proposer: an LLM reads the discovered surface and proposes
    field mappings — far more robust to messy real-tenant schemas than the
    synonym table. It only PROPOSES; C7's verification probes still bind every
    mapping against sampled data, so a wrong LLM proposal is caught and
    preserved as REJECTED, never trusted. Falls back to the heuristic explorer
    if the model is unavailable."""

    name = 'llm_v1'

    def __init__(self, llm, fallback: 'Explorer | None' = None) -> None:
        from model_provider import LLMClient  # local import keeps seam optional
        self._llm: LLMClient = llm
        self._fallback = fallback or HeuristicExplorer()

    def propose(self, surface: SurfaceProfile,
                contract: tuple[ContractField, ...]) -> list[ProposedMapping]:
        from model_provider import ModelUnavailable
        entities_desc = '\n'.join(
            f'- {e.name}: ' + ', '.join(f.name for f in e.fields)
            for e in surface.entities)
        fields_desc = '\n'.join(
            f'- {c.name} ({c.entity_role}, {c.kind})' for c in contract)
        try:
            resp = self._llm.propose(
                task='onboarding_map',
                system=('You map an ERP\'s discovered entity fields to a fixed '
                        'canonical contract. Propose source_field per '
                        'contract_field using ONLY field names that appear in '
                        'the entities. Omit fields with no plausible match.'),
                user=f'Entities:\n{entities_desc}\n\nContract fields:\n{fields_desc}',
                json_schema=_MAP_SCHEMA, max_tokens=1024)
        except ModelUnavailable:
            return self._fallback.propose(surface, contract)
        data = resp.data or {}
        out: list[ProposedMapping] = []
        valid_fields = {(e.name, f.name) for e in surface.entities
                        for f in e.fields}
        for m in data.get('mappings', []):
            # Guard: only keep proposals naming a field that actually exists.
            # (The probe layer verifies semantics; this just drops fabrications.)
            if (m.get('entity'), m.get('source_field')) in valid_fields:
                out.append(ProposedMapping(
                    contract_field=m['contract_field'], entity=m['entity'],
                    source_field=m['source_field'],
                    rationale=m.get('rationale', 'llm proposal'),
                    proposed_by=self.name))
        return out
