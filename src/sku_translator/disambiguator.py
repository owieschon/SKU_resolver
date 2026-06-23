"""Disambiguator — turns a partial PartSpec into ranked candidate SKUs.

When the extractor produces a spec with ambiguities (missing body, missing
finish, generic family-word like "stack"), the disambiguator searches the
catalog for SKUs consistent with what IS pinned, ranks them, and returns
the top candidates.

The disambiguator consumes a CatalogIndex directly. It does NOT maintain
its own index. The catalog index already provides bucket(family, diameter)
which is the primary scoping primitive.

This module is stateless. Memory of prior rep choices lives in memory.py
and is consulted by the orchestrator before disambiguation.

Ranking signals
---------------
1. Match strength: how many of the spec's pinned fields agree with the
   candidate. Each match is +1; each contradiction is -1.
2. Popularity: log10-scaled lifetime sales count. Caps at +2 to prevent
   runaway domination by a single SKU.
3. Recency: SKUs ordered in last 90 days get +0.5.
4. In-stock bias: SKUs with positive quantity-on-hand get +0.25
   (small bias; we don't want to bury catalog SKUs that are simply OOS).

The disambiguator NEVER invents a SKU. Every returned candidate exists
in the catalog. If nothing matches, it returns confidence='none' with
an empty candidate list.

Proprietary handling
--------------------
The disambiguator does NOT enforce proprietary-customer policy. That's
the orchestrator's responsibility (and it needs the customer context
which the disambiguator doesn't have). The disambiguator returns
candidates including proprietary SKUs; the orchestrator filters them
based on the customer parameter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import log10
from typing import Any

try:
    from sku_translator.catalog_index import CatalogIndex, ParsedRow
    from sku_translator.extractor import PartSpec, Ambiguity
except ImportError:
    from catalog_index import CatalogIndex, ParsedRow
    from extractor import PartSpec, Ambiguity


@dataclass
class Candidate:
    """A ranked candidate SKU returned by the disambiguator."""
    sku: str
    """Canonical SKU string."""

    score: float
    """Composite ranking score (higher = better fit)."""

    matched_fields: list[str] = field(default_factory=list)
    """Spec fields that the candidate's parsed values agree with."""

    differing_fields: list[str] = field(default_factory=list)
    """Spec fields that the candidate fills in (spec was None, candidate
    has a value). These become the 'rep should confirm these' surface."""

    contradicting_fields: list[str] = field(default_factory=list)
    """Spec fields where spec and candidate disagree (both have values
    that don't match). Empty for high-confidence candidates."""

    reason: str = ''
    """Human-readable rationale for the rep UI."""

    parsed: ParsedRow | None = None
    """The parsed catalog row this candidate came from. Used by callers
    that need to inspect proprietary flag, in-stock state, etc."""

    @property
    def is_proprietary(self) -> bool:
        return bool(self.parsed and self.parsed.is_proprietary)

    @property
    def proprietary_customer(self) -> str | None:
        return self.parsed.proprietary_customer if self.parsed else None

    def __str__(self) -> str:
        return f'Candidate({self.sku}, score={self.score:.2f}, matches={self.matched_fields})'


@dataclass
class DisambiguationResult:
    """Result of disambiguating a partial PartSpec against a catalog."""
    candidates: list[Candidate]
    """Ranked candidates, highest score first."""

    open_questions: list[Ambiguity]
    """Ambiguities the rep still needs to resolve to narrow further."""

    confidence: str
    """'high' (single strong candidate), 'medium' (a few plausible),
    'low' (many candidates), 'none' (catalog has nothing matching)."""

    reasoning: str
    """One-line human summary."""


# ============================================================================
# Scoring
# ============================================================================

# Field-by-field comparison between PartSpec and ParsedRow.
# Each entry: (spec_field_name, parsed_row_attr_name)
# We keep this explicit so matching stays auditable.
_COMPARE_FIELDS = (
    ('family', 'family'),
    ('diameter', 'diameter'),
    ('length', 'length'),
    ('angle', 'angle'),
    ('finish', 'finish'),
    ('body', 'body'),
    ('outlet_diameter', 'outlet_diameter'),
    ('inlet_diameter', 'inlet_diameter'),
    ('oem', 'oem'),
)


def _score_candidate(
    spec: PartSpec,
    row: ParsedRow,
) -> tuple[float, list[str], list[str], list[str]]:
    """Score one ParsedRow against a spec.

    Returns
    -------
    (score, matched, differing, contradicting)
    """
    matched: list[str] = []
    differing: list[str] = []
    contradicting: list[str] = []
    score = 0.0

    for spec_attr, row_attr in _COMPARE_FIELDS:
        spec_val = getattr(spec, spec_attr, None)
        row_val = getattr(row, row_attr, None)
        if spec_val is None and row_val is None:
            continue
        if spec_val is None:
            # Spec didn't pin this field; row has a value — informational
            differing.append(spec_attr)
            continue
        if row_val is None:
            # Spec has a value; row doesn't — mild disagreement (the row
            # might be encoding-pattern-specific without explicit field)
            score -= 0.5
            continue
        # Both have values — compare
        if isinstance(spec_val, (int, float)) and isinstance(row_val, (int, float)):
            if abs(float(spec_val) - float(row_val)) < 0.001:
                score += 1.0
                matched.append(spec_attr)
            else:
                score -= 1.0
                contradicting.append(spec_attr)
        elif str(spec_val).upper() == str(row_val).upper():
            score += 1.0
            matched.append(spec_attr)
        else:
            score -= 1.0
            contradicting.append(spec_attr)

    # Popularity boost (log-scaled)
    if row.sales_count > 0:
        score += min(2.0, log10(row.sales_count + 1) / 2)

    # Trailing-year recency tiebreak
    if row.sales_qty_year > 0:
        score += 0.5

    # In-stock bias
    if row.quantity_on_hand > 0:
        score += 0.25

    return score, matched, differing, contradicting


def _is_sales_dominant(top, runner_up):
    top_sales = top.parsed.sales_count
    runner_sales = runner_up.parsed.sales_count
    if top_sales < 100:
        return False
    if runner_sales == 0:
        return True
    return top_sales >= 3 * runner_sales


def _format_reason(
    matched: list[str],
    differing: list[str],
    contradicting: list[str],
) -> str:
    parts = []
    if matched:
        parts.append(f'matches {", ".join(matched)}')
    if differing:
        parts.append(f'adds {", ".join(differing)}')
    if contradicting:
        parts.append(f'differs on {", ".join(contradicting)}')
    return '; '.join(parts) if parts else 'fallback'


# ============================================================================
# Disambiguate
# ============================================================================

def disambiguate(
    spec: PartSpec,
    catalog: CatalogIndex,
    *,
    max_candidates: int = 5,
    score_floor: float = 0.0,
    require_pinned_field: bool = True,
) -> DisambiguationResult:
    """Search the catalog for SKUs consistent with the spec.

    Strategy: bucket-then-score. Pick the smallest catalog bucket that
    respects what's pinned (typically family+diameter), score every
    entry in the bucket, sort, return the top N above the score floor.

    Parameters
    ----------
    spec : PartSpec produced by the extractor.
    catalog : a CatalogIndex.
    max_candidates : truncate result list at this length.
    score_floor : drop candidates scoring at or below this. Default 0.0
        means "must have at least one positive match"; negative values
        surface near-misses.
    require_pinned_field : if True (default), refuse to enumerate the
        whole catalog when no spec field is pinned. Returns confidence='none'.
        Set to False only for explicit catalog-wide search use cases.

    Returns
    -------
    DisambiguationResult.
    """
    # Refuse empty specs unless explicitly allowed
    if require_pinned_field:
        has_any_pinned = any(
            getattr(spec, f, None) is not None
            for f in ('family', 'diameter', 'length', 'angle', 'finish', 'body', 'oem')
        )
        if not has_any_pinned:
            return DisambiguationResult(
                candidates=[],
                open_questions=list(spec.ambiguities),
                confidence='none',
                reasoning='Spec has no pinned fields; cannot disambiguate from empty input',
            )

    # Choose the bucket from narrowest to broadest
    bucket: list[ParsedRow] = []
    subfamily_prefix = getattr(spec, 'subfamily_prefix', None)
    if spec.family is not None and spec.diameter is not None:
        bucket = catalog.bucket(family=spec.family, diameter=float(spec.diameter))
        if subfamily_prefix:
            narrowed = [r for r in bucket if r.sku.startswith(subfamily_prefix)]
            if narrowed:
                bucket = narrowed
        if not bucket:
            # Fall back to family-only
            bucket = catalog.bucket(family=spec.family)
    elif spec.family is not None:
        bucket = catalog.bucket(family=spec.family)
        if subfamily_prefix:
            narrowed = [r for r in bucket if r.sku.startswith(subfamily_prefix)]
            if narrowed:
                bucket = narrowed
    elif spec.diameter is not None:
        bucket = catalog.bucket(diameter=float(spec.diameter))
    else:
        # Pinned field is angle/finish/body/oem only — broad scan
        bucket = list(catalog.parsed_rows())

    if not bucket:
        return DisambiguationResult(
            candidates=[],
            open_questions=list(spec.ambiguities),
            confidence='none',
            reasoning=(
                f'No catalog entries match family={spec.family!r}, '
                f'diameter={spec.diameter!r}'
            ),
        )

    # Score every row in the bucket
    scored: list[Candidate] = []
    for row in bucket:
        score, matched, differing, contradicting = _score_candidate(spec, row)
        # A candidate with contradicting fields is not a candidate.
        # The spec is a constraint; if the rep said length=24 and the row
        # is length=30, that's substitution, not disambiguation.
        if contradicting:
            continue
        if score <= score_floor:
            continue
        cand = Candidate(
            sku=row.sku,
            score=score,
            matched_fields=matched,
            differing_fields=differing,
            contradicting_fields=contradicting,
            parsed=row,
        )
        cand.reason = _format_reason(matched, differing, contradicting)
        scored.append(cand)

    scored.sort(key=lambda c: (-c.score, c.sku))
    top = scored[:max_candidates]

    # Confidence calibration
    if not top:
        confidence = 'none'
        reasoning = f'No candidate in bucket of {len(bucket)} matched the spec above floor'
    elif len(top) == 1:
        confidence = 'high'
        reasoning = f'Single match: {top[0].sku}'
    elif top[0].score >= top[1].score + 1.5:
        confidence = 'high'
        reasoning = (
            f'Strong match: {top[0].sku} '
            f'(score gap {top[0].score - top[1].score:.1f})'
        )
    elif _is_sales_dominant(top[0], top[1]):
        confidence = 'high'
        reasoning = (
            f'Sales-dominant match: {top[0].sku} '
            f'({top[0].parsed.sales_count} lifetime sales vs {top[1].parsed.sales_count})'
        )
    elif len(top) <= 3:
        confidence = 'medium'
        reasoning = f'{len(top)} plausible candidates'
    else:
        confidence = 'low'
        reasoning = f'{len(top)}+ candidates; spec is too generic'

    return DisambiguationResult(
        candidates=top,
        open_questions=list(spec.ambiguities),
        confidence=confidence,
        reasoning=reasoning,
    )


# ============================================================================
# Self-test
# ============================================================================

def _selftest() -> None:
    """Self-test using a minimal in-memory CatalogIndex impl."""
    try:
        from sku_translator.catalog_index import family_prefix_for
    except ImportError:
        from catalog_index import family_prefix_for

    class _MockCatalog:
        def __init__(self, rows):
            self._rows = rows
            self._upper = {r.sku.upper(): r for r in rows}

        def tenant_id(self):
            return 'mock'

        def is_canonical(self, sku):
            return bool(sku) and sku.upper() in self._upper

        def lookup(self, sku):
            return self._upper.get(sku.upper()) if sku else None

        def parsed_rows(self):
            return iter(self._rows)

        def all_skus(self):
            return [r.sku for r in self._rows]

        def bucket(self, family=None, diameter=None):
            out = self._rows
            if family is not None:
                out = [r for r in out if r.family == family]
            if diameter is not None:
                out = [r for r in out if r.diameter == diameter]
            return list(out)

        def family_prefix_bucket(self, prefix):
            return [r for r in self._rows if family_prefix_for(r.sku) == prefix.upper()]

        def reload(self):
            pass

        def size(self):
            return len(self._rows)

    rows = [
        ParsedRow(sku='K5-24SBC', family='K', diameter=5.0, length=24.0, body='SB', finish='C', sales_count=1500),
        ParsedRow(sku='K5-24SBA', family='K', diameter=5.0, length=24.0, body='SB', finish='A', sales_count=50),
        ParsedRow(sku='K5-24EXC', family='K', diameter=5.0, length=24.0, body='EX', finish='C', sales_count=100),
        ParsedRow(sku='K5-30SBC', family='K', diameter=5.0, length=30.0, body='SB', finish='C', sales_count=800),
        ParsedRow(sku='K5-36SBC', family='K', diameter=5.0, length=36.0, body='SB', finish='C', sales_count=200),
        ParsedRow(sku='K6-24SBC', family='K', diameter=6.0, length=24.0, body='SB', finish='C', sales_count=300),
        ParsedRow(sku='BH5-30SBA', family='BH', diameter=5.0, length=30.0, body='SB', finish='A', sales_count=100),
    ]
    cat = _MockCatalog(rows)

    # Test 1: full spec, single strong match expected
    spec1 = PartSpec(family='K', diameter=5.0, length=24.0, finish='C', body='SB',
                    raw_input='K 5 24 chrome SB')
    r1 = disambiguate(spec1, cat)
    assert r1.confidence == 'high', r1
    assert r1.candidates[0].sku == 'K5-24SBC', r1.candidates

    # Test 2: missing length — multiple candidates, popular SKU first
    spec2 = PartSpec(family='K', diameter=5.0, finish='C', body='SB', raw_input='K 5 chrome SB')
    r2 = disambiguate(spec2, cat)
    skus_2 = [c.sku for c in r2.candidates]
    assert skus_2[0] == 'K5-24SBC', f'expected popular SKU first, got {skus_2}'
    assert len(r2.candidates) >= 2, skus_2

    # Test 3: spec with diameter+length, no family — should narrow
    spec3 = PartSpec(diameter=5.0, length=24.0, raw_input='5 inch 24 long')
    r3 = disambiguate(spec3, cat)
    assert len(r3.candidates) >= 1
    assert all(c.parsed.diameter == 5.0 and c.parsed.length == 24.0 for c in r3.candidates)

    # Test 4: empty spec — must refuse to enumerate
    spec4 = PartSpec(raw_input='whatever')
    r4 = disambiguate(spec4, cat)
    assert r4.confidence == 'none', r4
    assert r4.candidates == [], r4

    # Test 5: nothing matches
    spec5 = PartSpec(family='ZZZ', diameter=99.0, raw_input='unknown')
    r5 = disambiguate(spec5, cat)
    assert r5.confidence == 'none', r5

    # Test 6: contradicting fields exclude candidates entirely.
    # spec says body=EX; only rows with body=EX (or body=None) should appear.
    spec6 = PartSpec(family='K', diameter=5.0, length=24.0, body='EX', finish='C',
                    raw_input='K 5 24 ID chrome')
    r6 = disambiguate(spec6, cat)
    # K5-24EXC has body=EX, finish=C — should be the match
    skus_6 = [c.sku for c in r6.candidates]
    assert 'K5-24EXC' in skus_6, skus_6
    # K5-24SBC must NOT appear (contradicts body)
    assert 'K5-24SBC' not in skus_6, skus_6
    # No candidate should have body in contradicting_fields
    for c in r6.candidates:
        assert 'body' not in c.contradicting_fields, c

    print('disambiguator v2.0 — self-test passed')


if __name__ == '__main__':
    _selftest()
