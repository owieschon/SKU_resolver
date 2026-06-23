"""Fuzzy SKU matcher.

Given input text that's almost-but-not-canonical (typos, missing dashes,
case errors, transposed characters), find the closest matching SKU(s)
in the tenant's catalog.

Architecture
------------
The fuzzy matcher consumes a CatalogIndex directly. It does NOT maintain
its own copy of the catalog. The catalog index already provides:

  - is_canonical(sku) for exact membership
  - family_prefix_bucket(prefix) for bucket-scoped edit-distance scans

Matching strategy (cheapest-to-strictest)
-----------------------------------------
1. Exact match (case-insensitive) via catalog.is_canonical
2. Normalized match (strip dashes/spaces/case) — scan the family-prefix
   bucket for a normalized-form match
3. Edit-distance within the family-prefix bucket
4. Edit-distance against normalized forms within the bucket (catches
   missing-separator typos that bumped raw distance over the threshold)

Bucketing keeps the typical search space below ~750 SKUs for the biggest
family prefix. Levenshtein with early termination handles those buckets
in well under 5ms.

Confidence calibration
----------------------
This module returns ranked matches with distances. The orchestrator
(translator.py) decides what counts as "high confidence" based on the
distance and runner-up gap. Specifically, translate() promotes a fuzzy
match to RESOLVED iff:
  - distance == 0, OR
  - distance <= 1 AND runner-up distance >= top distance + 1

That keeps confidence calibration in one place rather than scattered.
"""
from __future__ import annotations

from dataclasses import dataclass

try:
    from sku_translator.catalog_index import CatalogIndex, family_prefix_for
except ImportError:
    from catalog_index import CatalogIndex, family_prefix_for


@dataclass
class FuzzyMatch:
    """A fuzzy match candidate from the catalog."""
    sku: str
    distance: int
    """Levenshtein distance from the query."""
    match_kind: str
    """'exact' / 'normalized' / 'edit'"""
    bucket: str
    """The family-prefix bucket the match came from."""

    def __str__(self) -> str:
        return f'FuzzyMatch({self.sku}, d={self.distance}, kind={self.match_kind})'


# ============================================================================
# Helpers
# ============================================================================

def _normalize_for_match(text: str) -> str:
    """Strip non-alphanumeric chars and uppercase. Used for separator-tolerant match."""
    return ''.join(c for c in text.upper() if c.isalnum())


def _levenshtein(a: str, b: str, max_distance: int | None = None) -> int:
    """Standard Levenshtein distance with optional early termination.

    Returns ``max_distance + 1`` when early termination engages and the
    minimum possible distance exceeds ``max_distance``.

    Iterative DP with rolling rows; O(min(la, lb)) memory.
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if max_distance is not None and abs(la - lb) > max_distance:
        return max_distance + 1
    # Ensure b is the shorter for memory efficiency
    if la < lb:
        a, b = b, a
        la, lb = lb, la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        row_min = i
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,         # deletion
                curr[j - 1] + 1,     # insertion
                prev[j - 1] + cost,  # substitution
            )
            if curr[j] < row_min:
                row_min = curr[j]
        if max_distance is not None and row_min > max_distance:
            return max_distance + 1
        prev = curr
    return prev[lb]


# ============================================================================
# Match query
# ============================================================================

def _candidate_buckets(query: str, catalog: CatalogIndex) -> list[str]:
    """Return a list of bucket prefixes to search, ordered from most-specific
    to least-specific.

    For an input like 'PRKL790C', the natural bucket is 'PRKL' (prefix until
    first digit). But the canonical SKU is 'PRK-L790C' which buckets as 'PRK'.
    When the query loses a separator, the query's bucket can be longer than
    the canonical's. So we also try progressively shorter prefixes.

    This costs us scanning a few extra buckets per query, but each bucket
    is small (~50-300 rows) and Levenshtein is early-terminating, so the
    impact is negligible (<1ms per call in practice).
    """
    primary = family_prefix_for(query)
    candidates = [primary]
    # Add progressively shorter prefixes (down to 1 letter), but only if
    # they actually exist as buckets in the catalog. We don't fall through
    # to the universal NUM/OTHER buckets — those would scan the whole catalog.
    if len(primary) > 1 and primary not in ('NUM', 'OTHER'):
        for cut in range(len(primary) - 1, 0, -1):
            shorter = primary[:cut]
            if catalog.family_prefix_bucket(shorter):
                candidates.append(shorter)
    return candidates


def fuzzy_match(
    query: str,
    catalog: CatalogIndex,
    *,
    max_distance: int = 2,
    max_candidates: int = 5,
) -> list[FuzzyMatch]:
    """Find catalog SKUs matching ``query``.

    Returns matches sorted by distance ascending (best first). An empty
    list means nothing matched within ``max_distance``.

    Parameters
    ----------
    query : raw input text. Assumed to be at most one SKU. If you have
        free-form text, run it through the extractor first to get a
        canonical-SKU candidate before calling this.
    catalog : a CatalogIndex (FixtureCatalogIndex or ERPCatalogIndex).
    max_distance : maximum Levenshtein distance to consider. Default 2.
        Distances above 3 produce too many false positives in practice.
    max_candidates : truncate result list at this length.
    """
    if not query:
        return []
    q = query.strip()
    if not q:
        return []

    # Tier 1: exact catalog membership (case-insensitive)
    if catalog.is_canonical(q):
        canonical_row = catalog.lookup(q)
        return [FuzzyMatch(
            sku=canonical_row.sku,
            distance=0,
            match_kind='exact',
            bucket=family_prefix_for(canonical_row.sku),
        )]

    # Determine which buckets to search. We try the exact-prefix bucket first
    # plus progressively shorter prefixes (handles missing-dash cases like
    # 'PRKL790C' querying when 'PRK-L790C' is the canonical form).
    bucket_prefixes = _candidate_buckets(q, catalog)

    # Collect all rows from candidate buckets, deduped
    seen_skus: set[str] = set()
    bucket_rows: list = []
    bucket_used: dict[str, str] = {}  # sku -> bucket_prefix that found it
    for prefix in bucket_prefixes:
        for row in catalog.family_prefix_bucket(prefix):
            if row.sku not in seen_skus:
                seen_skus.add(row.sku)
                bucket_rows.append(row)
                bucket_used[row.sku] = prefix

    if not bucket_rows:
        return []  # No candidate buckets had any rows

    # Scope fuzzy CORRECTIONS to the customer-facing catalog: a mis-heard part
    # must never typo-correct onto an obsolete/proprietary/out-of-scope SKU.
    # (Tier-1 exact membership above is intentionally unscoped — an exact SKU
    # still resolves.) Fallback: a catalog with no customer-facing metadata
    # (e.g. a synthetic test catalog) keeps all bucket rows rather than none.
    scoped = [r for r in bucket_rows if getattr(r, 'is_customer_facing', False)]
    if scoped:
        bucket_rows = scoped

    q_normalized = _normalize_for_match(q)
    upper_q = q.upper()

    # Tier 2: normalized match across candidate buckets
    matches: list[FuzzyMatch] = []
    for row in bucket_rows:
        if _normalize_for_match(row.sku) == q_normalized:
            matches.append(FuzzyMatch(
                sku=row.sku,
                distance=0,
                match_kind='normalized',
                bucket=bucket_used.get(row.sku, 'OTHER'),
            ))
    if matches:
        matches.sort(key=lambda m: (m.distance, len(m.sku), m.sku))
        return matches[:max_candidates]

    # Tier 3: edit distance within candidate buckets (raw form)
    for row in bucket_rows:
        dist = _levenshtein(upper_q, row.sku.upper(), max_distance=max_distance)
        if dist <= max_distance:
            matches.append(FuzzyMatch(
                sku=row.sku,
                distance=dist,
                match_kind='edit',
                bucket=bucket_used.get(row.sku, 'OTHER'),
            ))

    # Tier 4: edit distance on normalized forms (catches missing-separator
    # typos whose raw form distance exceeds the threshold)
    if not matches:
        for row in bucket_rows:
            sku_norm = _normalize_for_match(row.sku)
            dist = _levenshtein(q_normalized, sku_norm, max_distance=max_distance)
            if dist <= max_distance:
                matches.append(FuzzyMatch(
                    sku=row.sku,
                    distance=dist,
                    match_kind='edit',
                    bucket=bucket_used.get(row.sku, 'OTHER'),
                ))

    matches.sort(key=lambda m: (m.distance, len(m.sku), m.sku))
    return matches[:max_candidates]


# ============================================================================
# Self-test
# ============================================================================

def _selftest() -> None:
    """Self-test against a minimal in-memory CatalogIndex implementation."""
    try:
        from sku_translator.catalog_index import ParsedRow
    except ImportError:
        from catalog_index import ParsedRow

    class _MockCatalog:
        """Minimal CatalogIndex impl for unit testing."""
        def __init__(self, skus):
            self._rows = [ParsedRow(sku=s) for s in skus]
            self._upper = {s.upper(): s for s in skus}

        def tenant_id(self):
            return 'mock'

        def is_canonical(self, sku):
            return bool(sku) and sku.strip().upper() in self._upper

        def lookup(self, sku):
            if not sku:
                return None
            canonical = self._upper.get(sku.strip().upper())
            return next((r for r in self._rows if r.sku == canonical), None)

        def parsed_rows(self):
            return iter(self._rows)

        def all_skus(self):
            return [r.sku for r in self._rows]

        def bucket(self, family=None, diameter=None):
            return list(self._rows)

        def family_prefix_bucket(self, prefix):
            return [r for r in self._rows if family_prefix_for(r.sku) == prefix.upper()]

        def reload(self):
            pass

        def size(self):
            return len(self._rows)

    cat = _MockCatalog([
        'K5-24SBC', 'K5-24SBA', 'K5-24EXC',
        'K5-30SBC', 'K5-36SBC',
        'BH5-30SBA',
        'SBR6-108EXC',
        'L590-1715SC',
        'PB-13056',
    ])

    # Exact match
    m = fuzzy_match('K5-24SBC', cat)
    assert len(m) == 1 and m[0].match_kind == 'exact', m

    # Case-insensitive
    m = fuzzy_match('k5-24sbc', cat)
    assert len(m) == 1 and m[0].sku == 'K5-24SBC' and m[0].match_kind == 'exact'

    # Missing dash → normalized match
    m = fuzzy_match('K524SBC', cat)
    assert len(m) == 1 and m[0].sku == 'K5-24SBC' and m[0].match_kind == 'normalized'

    # Single-character typo
    m = fuzzy_match('K5-24SBCC', cat)
    assert any(c.sku == 'K5-24SBC' for c in m), m
    assert m[0].sku == 'K5-24SBC' and m[0].distance == 1, m

    # Different bucket → empty result
    m = fuzzy_match('XYZ-99', cat)
    assert m == [], m

    # Multiple plausible in same bucket (missing finish letter)
    m = fuzzy_match('K5-24SB', cat)
    assert len(m) >= 2, m  # K5-24SBC, K5-24SBA both within distance 1

    # Empty / whitespace
    assert fuzzy_match('', cat) == []
    assert fuzzy_match('   ', cat) == []

    # Query with empty bucket (no rows match the prefix)
    cat2 = _MockCatalog(['K5-24SBC'])
    assert fuzzy_match('SBR6-108', cat2) == []

    print('fuzzy_matcher v2.0 — self-test passed')


if __name__ == '__main__':
    _selftest()
