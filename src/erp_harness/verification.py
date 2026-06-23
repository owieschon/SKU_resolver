"""C7 verification probes — every explorer claim is checked against data.

A ProposedMapping reaches VERIFIED only if a fresh sample of rows (pulled
through the enforcer, never reused from the explorer's view) passes the
value-shape checks for the contract field's kind. Failures carry the failing
check by name and become REJECTED records — preserved as evidence, never
silently dropped (the C7 planted-fault test plants a wrong proposal and asserts
exactly this).

Check thresholds are value-shape invariants of the KINDS (identifier,
number, date, ...), not tenant-tuned numbers — the pattern-thresholds rule's
'universal ratio' exception, with the reasoning recorded here:
  - identifiers: near-total presence and uniqueness, short (p95 <= 30 chars
    — catalog SKUs run ~6-20 chars; free-text descriptions run far longer,
    which is exactly the wrong-mapping class this check catches)
  - numbers/dates/flags: values must parse as their kind at >= 99% of
    non-null samples
"""
from __future__ import annotations

from datetime import date, datetime

from erp_harness.discovery import sample_rows
from erp_harness.enforcer import SafetyEnforcer
from erp_harness.models import (
    ContractField,
    MappingRecord,
    MappingState,
    ProposedMapping,
    VerificationEvidence,
)

VERIFY_SAMPLE = 50
# The identifier-length ceiling is RELATIVE: a fraction of the longest OTHER
# string field's p95 in the same sample, floored. The relativity is what makes
# it tenant-adaptive AND self-correcting (R0 #3):
#   - candidate = the short key  -> "other" fields include the long
#     description -> high ceiling -> passes
#   - candidate = the long description masquerading as the key -> "other"
#     fields are the short ones -> low ceiling -> rejected
# The floor handles tables with too few sibling fields to derive from; it is
# low (not a generous backstop) so the description-as-key case still fails.
_ID_LEN_FLOOR = 24
_ID_RELATIVE_FRACTION = 0.6


def _parses_as_number(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _parses_as_date(v) -> bool:
    if not isinstance(v, str):
        return False
    try:
        datetime.fromisoformat(v.replace('Z', '+00:00'))
        return True
    except ValueError:
        try:
            date.fromisoformat(v)
            return True
        except ValueError:
            return False


def _p95(lengths: list[int]) -> int:
    s = sorted(lengths)
    return s[int(0.95 * (len(s) - 1))] if s else 0


def _checks_for(kind: str, values: list,
                *, relative_ceiling: int | None = None) -> dict[str, bool]:
    nonnull = [v for v in values if v is not None and v != '']
    n = len(values)
    present = len(nonnull) / n if n else 0.0
    if kind == 'identifier':
        strs = [str(v) for v in nonnull]
        p95 = _p95([len(s) for s in strs])
        # R0 #3: the length ceiling is TENANT-RELATIVE, not a hardcoded 30.
        # An identifier is short relative to the table's free-text fields
        # (descriptions); the ceiling is derived from the entity's own field
        # lengths (relative_ceiling), with a generous absolute backstop so a
        # tenant with genuinely long part numbers is not rejected. The
        # discrimination that actually catches description-as-SKU is the
        # combination: present + unique + shorter-than-the-free-text.
        ceiling = relative_ceiling if relative_ceiling is not None \
            else _ID_LEN_FLOOR
        return {
            'present_99': present >= 0.99,
            'unique_99': (len(set(strs)) / len(strs) >= 0.99) if strs else False,
            'short_relative': 0 < p95 <= ceiling,
        }
    if kind == 'text':
        return {'present_90': present >= 0.90,
                'stringy': all(isinstance(v, str) for v in nonnull)}
    if kind == 'number':
        return {'present_99': present >= 0.99,
                'numeric_99': (sum(_parses_as_number(v) for v in nonnull)
                               / len(nonnull) >= 0.99) if nonnull else False}
    if kind == 'date':
        return {'present_90': present >= 0.90,
                'date_99': (sum(_parses_as_date(v) for v in nonnull)
                            / len(nonnull) >= 0.99) if nonnull else False}
    if kind == 'flag':
        return {'boolish': all(isinstance(v, bool) or v in (0, 1)
                               for v in nonnull)}
    return {'unknown_kind': False}


def _relative_id_ceiling(rows: list[dict], source_field: str) -> int:
    """Tenant-relative identifier-length ceiling (R0 #3): a fraction of the
    longest free-text field's p95 length in the same sample, floored at the
    absolute backstop. Computed from sibling fields, so it adapts to a
    tenant whose part numbers are long."""
    other_fields = {k for r in rows for k in r.keys() if k != source_field}
    longest_p95 = 0
    for f in other_fields:
        strs = [str(r[f]) for r in rows if isinstance(r.get(f), str) and r[f]]
        longest_p95 = max(longest_p95, _p95([len(s) for s in strs]))
    return max(_ID_LEN_FLOOR, int(_ID_RELATIVE_FRACTION * longest_p95))


def verify_mapping(enforcer: SafetyEnforcer, proposal: ProposedMapping,
                   contract_field: ContractField) -> MappingRecord:
    # R0 #4: stratified sample across the entity, not the first N sorted rows.
    rows = sample_rows(enforcer, proposal.entity, VERIFY_SAMPLE)
    values = [r.get(proposal.source_field) for r in rows]
    ceiling = (_relative_id_ceiling(rows, proposal.source_field)
               if contract_field.kind == 'identifier' else None)
    checks = _checks_for(contract_field.kind, values, relative_ceiling=ceiling)
    evidence = VerificationEvidence(
        sampled=len(rows), checks=checks,
        detail=(f'{proposal.entity}.{proposal.source_field} as '
                f'{contract_field.kind} over {len(rows)} stratified rows'
                + (f'; id ceiling={ceiling}' if ceiling is not None else '')))
    state = MappingState.VERIFIED if evidence.passed else MappingState.REJECTED
    return MappingRecord(mapping=proposal, state=state, evidence=evidence)
