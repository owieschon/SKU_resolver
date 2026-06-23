"""End-to-end integration tests for the SKU translator pipeline.

Runs against the full synthetic catalog (data/catalog.csv). Covers all five resolution paths, proprietary-customer
policy, memory replay, and edge cases.

Structured as a pytest seed for the production codebase migration. Each test_*
function is an independent assertion. Self-test runner at the bottom
runs all of them when invoked directly.

Run as a script:
    pytest tests/test_integration.py

Run via pytest (after migration):
    pytest sku_translator/test_integration.py -v
"""
from __future__ import annotations

from pathlib import Path

try:
    from sku_translator import (
        PENDING_DISAMBIGUATION,
        RESOLVED,
        UNRESOLVABLE,
        FixtureCatalogIndex,
        InMemoryStore,
        record_translation_choice,
        translate,
    )
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from sku_translator import (
        PENDING_DISAMBIGUATION,
        RESOLVED,
        UNRESOLVABLE,
        FixtureCatalogIndex,
        InMemoryStore,
        record_translation_choice,
        translate,
    )


import os as _os

# Catalog fixture resolution: env override first, then repo-relative default.
# (Dead container paths from the original build session removed 2026-06-06;
# see docs/MIGRATION_NOTES.md.)
FIXTURE_PATH = _os.environ.get(
    'SKU_CATALOG_PATH',
    str(Path(__file__).resolve().parent.parent / 'data' / 'catalog.csv'),
)


# ============================================================================
# Fixture builder (cached at module level for speed)
# ============================================================================

_catalog: FixtureCatalogIndex | None = None


def _get_catalog() -> FixtureCatalogIndex:
    global _catalog
    if _catalog is None:
        _catalog = FixtureCatalogIndex(FIXTURE_PATH, tenant_id='tenant_001_test')
    return _catalog


# ============================================================================
# Section 1: Catalog index integrity
# ============================================================================

def test_catalog_loads_active_skus_only():
    """Catalog should exclude OBSOLETE and BATTERY rows."""
    cat = _get_catalog()
    assert cat.size() == 9487, f'Expected 9487 active SKUs, got {cat.size()}'


def test_catalog_excludes_known_battery_skus():
    """Known battery SKU (4D31G) must not be in the catalog."""
    cat = _get_catalog()
    assert not cat.is_canonical('4D31G')
    assert cat.lookup('4D31G') is None


def test_catalog_finds_known_canonical_sku():
    """Known canonical SKU should be resolvable case-insensitively."""
    cat = _get_catalog()
    assert cat.is_canonical('K5-24SBC')
    assert cat.is_canonical('k5-24sbc')
    assert cat.is_canonical('K5-24sbc')
    row = cat.lookup('k5-24sbc')
    assert row is not None
    assert row.sku == 'K5-24SBC'  # returns canonical casing
    assert row.family == 'K'
    assert row.diameter == 5.0
    assert row.length == 24.0


def test_catalog_proprietary_count():
    """Proprietary rows are flagged from the product-group code (any typo variant)."""
    cat = _get_catalog()
    proprietary = sum(1 for r in cat.parsed_rows() if r.is_proprietary)
    assert proprietary == 331, f'Expected 331 proprietary, got {proprietary}'


def test_catalog_sales_data_present():
    """Sales data should be populated for most of the catalog."""
    cat = _get_catalog()
    with_sales = sum(1 for r in cat.parsed_rows() if r.sales_count > 0)
    assert with_sales > 7000, f'Expected ~7730 with sales, got {with_sales}'


def test_catalog_bucket_returns_consistent_data():
    """bucket(family='K', diameter=5.0) returns only matching rows."""
    cat = _get_catalog()
    rows = cat.bucket(family='K', diameter=5.0)
    assert len(rows) > 0
    for r in rows:
        assert r.family == 'K'
        assert r.diameter == 5.0


# ============================================================================
# Section 2: Parser passthrough (tier 1 of orchestrator)
# ============================================================================

def test_canonical_sku_passthrough():
    """Canonical SKU verified against catalog."""
    r = translate('K5-24SBC', _get_catalog())
    assert r.state == RESOLVED, r
    assert r.sku == 'K5-24SBC'
    assert r.source == 'parser'
    assert r.confidence == 'high'


def test_canonical_sku_lowercase():
    """Case-insensitive canonical match."""
    r = translate('k5-24sbc', _get_catalog())
    assert r.state == RESOLVED
    assert r.sku == 'K5-24SBC'  # returned in canonical casing
    assert r.source == 'parser'


def test_canonical_sku_mixed_case():
    """Mixed case canonical match."""
    r = translate('K5-24sBC', _get_catalog())
    assert r.state == RESOLVED
    assert r.sku == 'K5-24SBC'


def test_canonical_sku_with_whitespace():
    """Whitespace around the SKU should be handled."""
    r = translate('  K5-24SBC  ', _get_catalog())
    assert r.state == RESOLVED
    assert r.sku == 'K5-24SBC'


def test_real_reducer_sku():
    """A reducer SKU resolves cleanly via the verbatim path."""
    r = translate('R5-4C', _get_catalog())
    assert r.state == RESOLVED
    assert r.sku == 'R5-4C'


def test_real_elbow_sku():
    """Real elbow SKU (L590-1515SC actually exists)."""
    r = translate('L590-1515SC', _get_catalog())
    assert r.state == RESOLVED
    assert r.sku == 'L590-1515SC'


def test_real_bullhorn_sku():
    """Real bullhorn SKU."""
    r = translate('BH5-36SBC', _get_catalog())
    assert r.state == RESOLVED
    assert r.sku == 'BH5-36SBC'


# ============================================================================
# Section 3: Catalog membership enforcement
# ============================================================================

def test_grammatically_valid_but_not_in_catalog():
    """A SKU that parses cleanly but doesn't exist must NOT resolve via parser path.

    K5-99SBC: grammatically valid (K family, 5" diameter, 99" length, SB body, C finish)
    but not in the actual catalog. Should fall through to fuzzy.
    """
    r = translate('K5-99SBC', _get_catalog())
    # Either fuzzy finds a real K5-* SKU or it pends
    if r.state == RESOLVED:
        assert r.source == 'fuzzy', r
        # The resolved SKU MUST be in the actual catalog
        cat = _get_catalog()
        assert cat.is_canonical(r.sku), f'Resolved {r.sku} not in catalog'


def test_phantom_sku_l590_1715sc_fuzzy_corrects():
    """L590-1715SC parses cleanly but isn't in catalog. Fuzzy should find L590-1515SC or similar."""
    cat = _get_catalog()
    # Confirm setup: L590-1715SC is NOT in catalog, L590-1515SC IS
    assert not cat.is_canonical('L590-1715SC')
    assert cat.is_canonical('L590-1515SC')

    r = translate('L590-1715SC', cat)
    # Should not silently invent — either fuzzy correction or unresolvable
    if r.state == RESOLVED:
        assert cat.is_canonical(r.sku), f'Returned phantom SKU {r.sku}'
        assert r.source == 'fuzzy'


# ============================================================================
# Section 4: Fuzzy matching
# ============================================================================

def test_typo_extra_character():
    """Single-char typo resolves."""
    r = translate('K5-24SBCC', _get_catalog())
    assert r.state == RESOLVED, r
    assert r.sku == 'K5-24SBC', r
    assert r.source == 'fuzzy', r


def test_typo_missing_character():
    """Missing-char typo resolves."""
    r = translate('K-24SBC', _get_catalog())  # K5 missing the 5
    # Should either fuzzy-match or pend; must not invent
    if r.state == RESOLVED:
        cat = _get_catalog()
        assert cat.is_canonical(r.sku)


def test_missing_dash_normalizes():
    """Missing dash matches via normalized form."""
    r = translate('K524SBC', _get_catalog())
    assert r.state == RESOLVED, r
    assert r.sku == 'K5-24SBC', r
    assert r.source == 'fuzzy'


def test_double_typo_falls_through():
    """Two-character typo at the same SKU may still match if no confusion."""
    r = translate('K5-24SBXX', _get_catalog())
    # Either fuzzy resolves to K5-24SBC at distance 2, or pends due to multiple
    # equidistant candidates. Either way, can't invent.
    if r.state == RESOLVED:
        cat = _get_catalog()
        assert cat.is_canonical(r.sku)


def test_completely_wrong_family_unresolvable():
    """A family code that doesn't exist returns unresolvable."""
    r = translate('XYZZY-9999', _get_catalog())
    assert r.state == UNRESOLVABLE


# ============================================================================
# Section 5: Free-text construction
# ============================================================================

def test_freetext_full_spec():
    """Free text with all required fields constructs and verifies."""
    r = translate('curved 5 inch 24 long chrome SB', _get_catalog())
    assert r.state == RESOLVED, r
    assert r.sku == 'K5-24SBC', r
    assert r.source == 'construct'


def test_freetext_using_family_code():
    """Free text using the bare family code."""
    r = translate('K 5 inch 24 long chrome SB', _get_catalog())
    assert r.state == RESOLVED, r
    assert r.sku == 'K5-24SBC'


def test_freetext_aussie():
    """Aussie family-word resolves to A family."""
    r = translate('aussie 5 36 chrome SB', _get_catalog())
    assert r.state == RESOLVED, r
    # A5-36SBC must be in the actual catalog
    cat = _get_catalog()
    assert cat.is_canonical(r.sku)
    assert r.sku.startswith('A5-36')


def test_freetext_constructed_sku_must_be_in_catalog():
    """A constructed SKU that isn't actually in the catalog should NOT auto-resolve.

    K 5 99 chrome SB constructs to K5-99SBC which doesn't exist.
    """
    r = translate('K 5 99 chrome SB', _get_catalog())
    if r.state == RESOLVED:
        cat = _get_catalog()
        assert cat.is_canonical(r.sku), f'Constructed phantom {r.sku}'


# ============================================================================
# Section 6: Disambiguation (pending state)
# ============================================================================

def test_ambiguous_missing_length_pends():
    """Missing length yields multiple candidates."""
    r = translate('K 5 chrome SB', _get_catalog())
    assert r.state == PENDING_DISAMBIGUATION, r
    assert len(r.candidates) >= 2
    # Top candidate should be in catalog
    cat = _get_catalog()
    for c in r.candidates:
        assert cat.is_canonical(c.sku)


def test_ambiguous_top_candidate_is_popular():
    """Popular SKU should rank first when tied on spec match."""
    r = translate('K 5 chrome SB', _get_catalog())
    assert r.state == PENDING_DISAMBIGUATION
    # K5-24SBC is by far the most-sold; it should be first
    assert r.candidates[0].sku == 'K5-24SBC', [c.sku for c in r.candidates]


def test_disambiguator_excludes_contradicting():
    """If spec says body=EX, no SB-body SKUs should appear as candidates."""
    r = translate('K 5 24 chrome EX', _get_catalog())
    if r.candidates:
        for c in r.candidates:
            if c.parsed and c.parsed.body:
                assert c.parsed.body == 'EX', f'{c.sku} has body={c.parsed.body}, expected EX'


# ============================================================================
# Section 7: Memory replay
# ============================================================================

def test_memory_replay_after_three_choices():
    """Three identical rep choices for a customer = auto-resolve next time."""
    cat = _get_catalog()
    mem = InMemoryStore()
    spec = translate('K 5 chrome SB', cat).spec
    for _ in range(3):
        record_translation_choice(spec, 'K5-24SBC', mem, customer='DEMO')
    r = translate('K 5 chrome SB', cat, memory=mem, customer='DEMO')
    assert r.state == RESOLVED
    assert r.sku == 'K5-24SBC'
    assert r.source == 'memory_replay'


def test_memory_replay_scoped_to_customer():
    """Memory does NOT leak across customers."""
    cat = _get_catalog()
    mem = InMemoryStore()
    spec = translate('K 5 chrome SB', cat).spec
    for _ in range(3):
        record_translation_choice(spec, 'K5-24SBC', mem, customer='DEMO')
    # Different customer should not get the replay
    r = translate('K 5 chrome SB', cat, memory=mem, customer='FOURCORNERS')
    assert r.state == PENDING_DISAMBIGUATION


def test_memory_replay_below_threshold():
    """Two prior choices is not enough to trigger replay."""
    cat = _get_catalog()
    mem = InMemoryStore()
    spec = translate('K 5 chrome SB', cat).spec
    for _ in range(2):  # only 2, not 3
        record_translation_choice(spec, 'K5-24SBC', mem, customer='DEMO')
    r = translate('K 5 chrome SB', cat, memory=mem, customer='DEMO')
    assert r.state == PENDING_DISAMBIGUATION


def test_memory_replay_only_when_dominant():
    """Memory requires dominance ratio met (3 of 3 same SKU = 100% > 70%)."""
    cat = _get_catalog()
    mem = InMemoryStore()
    spec = translate('K 5 chrome SB', cat).spec
    # 3 different choices = 33% dominance, no replay
    record_translation_choice(spec, 'K5-24SBC', mem, customer='DEMO')
    record_translation_choice(spec, 'K5-30SBC', mem, customer='DEMO')
    record_translation_choice(spec, 'K5-36SBC', mem, customer='DEMO')
    r = translate('K 5 chrome SB', cat, memory=mem, customer='DEMO')
    assert r.state == PENDING_DISAMBIGUATION


# ============================================================================
# Section 8: Proprietary-customer policy
# ============================================================================

def test_proprietary_warning_no_customer():
    """Proprietary SKU resolves but with warning when no customer specified."""
    cat = _get_catalog()
    # Find a proprietary SKU we know about
    proprietary_sku = next(
        (r.sku for r in cat.parsed_rows()
         if r.is_proprietary and r.proprietary_customer),
        None
    )
    assert proprietary_sku is not None, 'Need at least one proprietary SKU with attribution'

    r = translate(proprietary_sku, cat)
    assert r.state == RESOLVED
    assert r.proprietary_warning is not None


def test_proprietary_match_customer():
    """Proprietary SKU + matching customer = clean resolve."""
    cat = _get_catalog()
    # Find a proprietary SKU with attribution
    target = None
    for row in cat.parsed_rows():
        if row.is_proprietary and row.proprietary_customer:
            target = row
            break
    assert target is not None
    customer = target.proprietary_customer

    r = translate(target.sku, cat, customer=customer)
    assert r.state == RESOLVED
    assert not r.proprietary_violation


def test_proprietary_wrong_customer():
    """Proprietary SKU + wrong customer = violation flagged, confidence downgraded."""
    cat = _get_catalog()
    # Find a NORCO-attributed SKU
    norco_sku = next(
        (r.sku for r in cat.parsed_rows()
         if r.is_proprietary and r.proprietary_customer and 'NORCO' in r.proprietary_customer.upper()),
        None
    )
    assert norco_sku is not None, 'Need a NORCO-attributed SKU for this test'

    r = translate(norco_sku, cat, customer='DEMO')
    assert r.state == RESOLVED  # We don't refuse outright; we flag
    assert r.proprietary_violation
    assert r.confidence == 'low'
    assert 'NORCO' in r.proprietary_warning


def test_proprietary_unattributed_warns():
    """Proprietary but unattributed SKU should warn, not block."""
    cat = _get_catalog()
    # Find a proprietary SKU with no attribution
    unattributed = next(
        (r.sku for r in cat.parsed_rows()
         if r.is_proprietary and not r.proprietary_customer),
        None
    )
    assert unattributed is not None, 'Need an unattributed proprietary SKU'

    r = translate(unattributed, cat, customer='DEMO')
    assert r.state == RESOLVED
    # No violation (we don't have attribution to compare)
    assert not r.proprietary_violation
    # But there IS a warning
    assert r.proprietary_warning is not None


# ============================================================================
# Section 9: Unresolvable cases (must never invent SKUs)
# ============================================================================

def test_empty_input_unresolvable():
    """Empty string returns UNRESOLVABLE without crashing."""
    r = translate('', _get_catalog())
    assert r.state == UNRESOLVABLE


def test_whitespace_input_unresolvable():
    """Whitespace-only input is unresolvable."""
    r = translate('   ', _get_catalog())
    assert r.state == UNRESOLVABLE


def test_none_input_unresolvable():
    """None input handled without crashing."""
    r = translate(None, _get_catalog())  # type: ignore
    assert r.state == UNRESOLVABLE


def test_garbage_input_unresolvable():
    """Random garbage doesn't crash and doesn't resolve."""
    r = translate('jklasdjklasd', _get_catalog())
    assert r.state == UNRESOLVABLE


def test_translator_never_invents_sku():
    """Full: every resolved result MUST reference a real catalog SKU."""
    cat = _get_catalog()
    test_inputs = [
        'K5-24SBC',          # canonical
        'k5-24sbc',          # case
        'K524SBC',           # no dash
        'K5-24SBCC',         # typo
        'K5-99SBC',          # phantom
        'L590-1715SC',       # phantom L
        'curved 5 inch 24 long chrome SB',
        'aussie 5 36 chrome SB',
        'K 5 99 chrome SB',  # constructs to phantom
        'XYZZY-9999',        # garbage
        'BH5-36SBC',         # real
        'R5-4C',       # real
    ]
    for text in test_inputs:
        r = translate(text, cat)
        if r.state == RESOLVED:
            assert cat.is_canonical(r.sku), (
                f'Input {text!r} resolved to {r.sku!r} which is NOT in catalog. '
                f'source={r.source}, reasoning={r.reasoning}'
            )


# ============================================================================
# Section 10: Edge cases and stress
# ============================================================================

def test_very_long_input():
    """Very long input doesn't crash."""
    r = translate('K5-24SBC ' + 'x' * 1000, _get_catalog())
    # Won't resolve cleanly but mustn't crash
    assert r.state in (RESOLVED, PENDING_DISAMBIGUATION, UNRESOLVABLE)


def test_special_characters_in_input():
    """Special characters don't crash the pipeline."""
    for bad in ['K5\n24SBC', 'K5\t24SBC', 'K5"24"SBC', 'K5/24/SBC']:
        r = translate(bad, _get_catalog())
        assert r.state in (RESOLVED, PENDING_DISAMBIGUATION, UNRESOLVABLE)


def test_unicode_input():
    """Unicode input doesn't crash."""
    r = translate('K5-24SBC ★', _get_catalog())
    # K5-24SBC plus garbage; may or may not resolve, but mustn't crash
    assert r.state in (RESOLVED, PENDING_DISAMBIGUATION, UNRESOLVABLE)


def test_no_catalog_provided():
    """Translator runs in parse-only mode when no catalog."""
    r = translate('K5-24SBC', None)
    assert r.state == RESOLVED
    assert r.sku == 'K5-24SBC'
    assert r.confidence == 'medium'  # downgraded — no catalog verification


def test_proprietary_customer_filter_in_disambiguation():
    """Proprietary candidates whose customer doesn't match get filtered.

    Pick a NORCO-attributed SKU directly, then run a partial-spec query
    that would surface it. Confirm DEMO (the wrong customer) doesn't see
    NORCO SKUs in the candidate list.
    """
    cat = _get_catalog()
    # Find any NORCO SKU
    norco_sku_row = next(
        (r for r in cat.parsed_rows()
         if r.is_proprietary and r.proprietary_customer
         and 'NORCO' in r.proprietary_customer.upper()),
        None
    )
    if norco_sku_row is None:
        return  # no NORCO SKUs in fixture; skip silently

    # Construct a query that matches NORCO's family/diameter but is partial
    # so disambiguation runs (rather than parser-passthrough).
    family = norco_sku_row.family
    diameter = norco_sku_row.diameter
    if family is None or diameter is None:
        return  # can't construct a useful partial query

    # Build query that should produce candidates including the NORCO SKU
    # if we don't filter
    query = f'{family} {diameter:.0f} chrome'

    # With DEMO customer: NORCO candidates must be filtered
    r_demo = translate(query, cat, customer='DEMO')
    if r_demo.state == PENDING_DISAMBIGUATION:
        for c in r_demo.candidates:
            if c.is_proprietary and c.proprietary_customer:
                # If a proprietary SKU made it through, its attribution
                # must match the customer
                assert c.proprietary_customer.upper().strip() == 'DEMO', (
                    f'Proprietary SKU {c.sku} attributed to '
                    f'{c.proprietary_customer!r} surfaced as candidate for DEMO'
                )


def test_parse_then_translate_consistency():
    """A parsed canonical SKU should translate to itself."""
    cat = _get_catalog()
    sample_skus = ['K5-24SBC', 'BH5-36SBC', 'R5-4C', 'L590-1515SC']
    for sku in sample_skus:
        r = translate(sku, cat)
        assert r.state == RESOLVED, f'{sku} did not resolve: {r.reasoning}'
        assert r.sku == sku, f'{sku} translated to {r.sku}'


# ============================================================================
# Section 11: Pipeline source attribution
# ============================================================================

def test_source_attribution_consistency():
    """Each input class should resolve via the documented source."""
    cat = _get_catalog()
    cases = [
        ('K5-24SBC', 'parser'),                          # canonical
        ('k5-24sbc', 'parser'),                          # case
        ('K524SBC', 'fuzzy'),                            # missing dash
        ('K5-24SBCC', 'fuzzy'),                          # typo
        ('curved 5 inch 24 long chrome SB', 'construct'),  # free-text full
    ]
    for text, expected_source in cases:
        r = translate(text, cat)
        assert r.state == RESOLVED, f'{text!r} did not resolve'
        assert r.source == expected_source, (
            f'{text!r} resolved via {r.source}, expected {expected_source}'
        )


# ============================================================================
# Section 12: Vocabulary expansion (Change 1)
# ============================================================================

def test_vocabulary_coupler_family():
    """'coupler' should pin family CP."""
    try:
        from sku_translator.extractor import extract_spec
    except ImportError:
        from extractor import extract_spec
    spec = extract_spec('5 inch coupler chrome')
    assert spec.family == 'CP', f'Expected CP, got {spec.family}'


def test_vocabulary_couplers_plural():
    """'couplers' (plural) should also pin family CP."""
    try:
        from sku_translator.extractor import extract_spec
    except ImportError:
        from extractor import extract_spec
    spec = extract_spec('5 inch couplers chrome')
    assert spec.family == 'CP', f'Expected CP, got {spec.family}'


def test_vocabulary_v_band_clamp():
    """'v-band clamp' variants should pin family VB."""
    try:
        from sku_translator.extractor import extract_spec
    except ImportError:
        from extractor import extract_spec
    for variant in ('v-band clamp 5', '5 inch v band clamp chrome',
                    'vband 5 chrome', '5 v-band'):
        spec = extract_spec(variant)
        assert spec.family == 'VB', f'{variant!r}: expected VB, got {spec.family}'


def test_vocabulary_turbo_flare_family():
    """'turbo flare' should pin family T."""
    try:
        from sku_translator.extractor import extract_spec
    except ImportError:
        from extractor import extract_spec
    spec = extract_spec('5 inch turbo flare 588')
    assert spec.family == 'T', f'Expected T, got {spec.family}'


def test_vocabulary_type_one_muffler():
    """'type one' / 'type 1' should pin family M."""
    try:
        from sku_translator.extractor import extract_spec
    except ImportError:
        from extractor import extract_spec
    for variant in ('type one muffler 465', 'type 1 muffler 465'):
        spec = extract_spec(variant)
        assert spec.family == 'M', f'{variant!r}: expected M, got {spec.family}'


def test_vocabulary_reducers_plural():
    """'reducers' plural should pin family R."""
    try:
        from sku_translator.extractor import extract_spec
    except ImportError:
        from extractor import extract_spec
    spec = extract_spec('5 to 4 reducers aluminized')
    assert spec.family == 'R', f'Expected R, got {spec.family}'


def test_vocabulary_bare_turbo_not_family():
    """Bare 'turbo' should NOT pin a family on its own (only 'turbo flare'/'turbo pipe')."""
    try:
        from sku_translator.normalizer import normalize_family_word
    except ImportError:
        from normalizer import normalize_family_word
    assert normalize_family_word('turbo') is None, (
        "'turbo' alone must not resolve to a family — only 'turbo flare' or 'turbo pipe' should"
    )


# ============================================================================
# Section 13: Elbow angle-to-prefix mapping (Change 2)
# ============================================================================

def test_elbow_subfamily_prefix_90():
    """A 5-inch 90-degree elbow spec should compute subfamily_prefix='L590'."""
    try:
        from sku_translator.extractor import extract_spec
    except ImportError:
        from extractor import extract_spec
    spec = extract_spec('5 inch 90 degree elbow chrome 12 by 12')
    assert spec.subfamily_prefix == 'L590', (
        f'Expected L590, got {spec.subfamily_prefix}'
    )


def test_elbow_subfamily_prefix_45():
    """A 4-inch 45-degree elbow spec should compute subfamily_prefix='L445'."""
    try:
        from sku_translator.extractor import extract_spec
    except ImportError:
        from extractor import extract_spec
    spec = extract_spec('4 inch 45 degree elbow aluminized')
    assert spec.subfamily_prefix == 'L445', (
        f'Expected L445, got {spec.subfamily_prefix}'
    )


def test_elbow_resolves_to_l590_prefix():
    """translate(5 inch 90 deg elbow chrome 12x12) must return L590-* candidates."""
    cat = _get_catalog()
    r = translate('5 inch 90 degree elbow chrome 12 by 12', cat)
    cands = r.candidates or []
    if r.state == RESOLVED:
        assert r.sku.startswith('L590'), (
            f'RESOLVED to non-L590 SKU: {r.sku}'
        )
    else:
        assert cands, 'Expected candidates from disambiguator'
        for c in cands[:3]:
            assert c.sku.startswith('L590'), (
                f'Expected L590-* candidates, got {c.sku}'
            )


def test_elbow_angle_constant_is_catalog_derived():
    """ELBOW_ANGLE_TO_PREFIX must include only angles that exist in catalog."""
    try:
        from sku_translator.extractor import ELBOW_ANGLE_TO_PREFIX
    except ImportError:
        from extractor import ELBOW_ANGLE_TO_PREFIX
    cat = _get_catalog()
    catalog_angles = {
        int(r.angle) for r in cat.parsed_rows()
        if r.family == 'L' and r.angle is not None
    }
    mapping_angles = set(ELBOW_ANGLE_TO_PREFIX.keys())
    # Every angle in the mapping must appear in the catalog.
    extra = mapping_angles - catalog_angles
    assert not extra, f'Mapping has angles not in catalog: {sorted(extra)}'


# ============================================================================
# Section 14: Sales-dominance auto-resolve (Change 3)
# ============================================================================

def test_sales_dominance_k5_24sbc():
    """K 5 chrome SB 24 must return RESOLVED to K5-24SBC."""
    cat = _get_catalog()
    r = translate('K 5 chrome SB 24', cat)
    assert r.state == RESOLVED, (
        f'Expected RESOLVED, got {r.state}: {r.reasoning}'
    )
    assert r.sku == 'K5-24SBC', f'Expected K5-24SBC, got {r.sku}'


def test_sales_dominance_helper_basic():
    """_is_sales_dominant: 3x and runner==0 thresholds."""
    try:
        from sku_translator.catalog_index import ParsedRow
        from sku_translator.disambiguator import _is_sales_dominant
    except ImportError:
        from catalog_index import ParsedRow
        from disambiguator import _is_sales_dominant

    def cand(sales):
        c = type('C', (), {})()
        c.parsed = ParsedRow(sku='X', sales_count=sales, family='K')
        return c

    # 3x or more → dominant (top >= 100)
    assert _is_sales_dominant(cand(10000), cand(2000)) is True
    assert _is_sales_dominant(cand(10000), cand(3333)) is True
    # Just under 3x → not dominant
    assert _is_sales_dominant(cand(10000), cand(3334)) is False
    # Top below 100 sales → not dominant regardless of ratio
    assert _is_sales_dominant(cand(50), cand(0)) is False
    # Runner-up at 0, top >= 100 → dominant
    assert _is_sales_dominant(cand(100), cand(0)) is True


def test_sales_close_stays_pending():
    """When top and runner-up are within ratio gap, confidence must NOT be high."""
    try:
        from sku_translator.catalog_index import ParsedRow, family_prefix_for
        from sku_translator.disambiguator import disambiguate
        from sku_translator.extractor import PartSpec
    except ImportError:
        from catalog_index import ParsedRow, family_prefix_for
        from disambiguator import disambiguate
        from extractor import PartSpec

    class _Mock:
        def __init__(self, rows):
            self._rows = rows
        def tenant_id(self): return 'mock'
        def is_canonical(self, s): return False
        def lookup(self, s): return None
        def parsed_rows(self): return iter(self._rows)
        def all_skus(self): return [r.sku for r in self._rows]
        def bucket(self, family=None, diameter=None):
            out = self._rows
            if family is not None: out = [r for r in out if r.family == family]
            if diameter is not None: out = [r for r in out if r.diameter == diameter]
            return list(out)
        def family_prefix_bucket(self, p):
            return [r for r in self._rows if family_prefix_for(r.sku) == p.upper()]
        def reload(self): pass
        def size(self): return len(self._rows)

    rows = [
        ParsedRow(sku='K5-24SBC', family='K', diameter=5.0, length=24.0,
                  body='SB', finish='C', sales_count=1000),
        ParsedRow(sku='K5-24SBA', family='K', diameter=5.0, length=24.0,
                  body='SB', finish='A', sales_count=600),  # within 3x
    ]
    cat = _Mock(rows)
    spec = PartSpec(family='K', diameter=5.0, length=24.0, body='SB',
                    raw_input='K 5 24 SB')
    r = disambiguate(spec, cat)
    # Both candidates tie on score; sales gap is 1000/600 = 1.67 < 3x.
    # Confidence must not be 'high' via sales-dominance.
    assert r.confidence != 'high', (
        f'Sales-close (1.67x) should not auto-resolve high; got {r.confidence}'
    )


def test_sales_dominance_low_volume_not_promoted():
    """Top <100 sales must never promote via sales-dominance."""
    try:
        from sku_translator.catalog_index import ParsedRow, family_prefix_for
        from sku_translator.disambiguator import disambiguate
        from sku_translator.extractor import PartSpec
    except ImportError:
        from catalog_index import ParsedRow, family_prefix_for
        from disambiguator import disambiguate
        from extractor import PartSpec

    class _Mock:
        def __init__(self, rows):
            self._rows = rows
        def tenant_id(self): return 'mock'
        def is_canonical(self, s): return False
        def lookup(self, s): return None
        def parsed_rows(self): return iter(self._rows)
        def all_skus(self): return [r.sku for r in self._rows]
        def bucket(self, family=None, diameter=None):
            out = self._rows
            if family is not None: out = [r for r in out if r.family == family]
            if diameter is not None: out = [r for r in out if r.diameter == diameter]
            return list(out)
        def family_prefix_bucket(self, p):
            return [r for r in self._rows if family_prefix_for(r.sku) == p.upper()]
        def reload(self): pass
        def size(self): return len(self._rows)

    rows = [
        ParsedRow(sku='X1', family='K', diameter=5.0, length=24.0,
                  body='SB', finish='C', sales_count=50),  # below 100
        ParsedRow(sku='X2', family='K', diameter=5.0, length=24.0,
                  body='SB', finish='A', sales_count=0),
    ]
    cat = _Mock(rows)
    spec = PartSpec(family='K', diameter=5.0, length=24.0, body='SB',
                    raw_input='K 5 24 SB')
    r = disambiguate(spec, cat)
    # Top has 50 sales (<100) — must not promote even though runner is 0.
    assert r.confidence != 'high' or 'Sales-dominant' not in r.reasoning, (
        f'Low-volume (50<100) must not sales-dominate: {r.confidence} '
        f'{r.reasoning}'
    )


# ============================================================================
# Self-test runner
# ============================================================================

def _run_all():
    """Discover and run every test_* function in this module."""
    test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith('test_') and callable(fn)
    ]
    test_fns.sort(key=lambda x: x[0])

    passed = 0
    failed: list[tuple[str, str]] = []

    for name, fn in test_fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed.append((name, str(e) or '(assertion failed)'))
        except Exception as e:
            failed.append((name, f'{type(e).__name__}: {e}'))

    total = len(test_fns)
    print(f'\n{"=" * 70}')
    print('INTEGRATION TEST RESULTS')
    print(f'{"=" * 70}')
    print(f'  Passed:  {passed:3d} / {total}')
    print(f'  Failed:  {len(failed):3d}')
    if failed:
        print('\nFailures:')
        for name, err in failed:
            err_short = err[:200] + ('...' if len(err) > 200 else '')
            print(f'  ✗ {name}')
            print(f'      {err_short}')
    return len(failed) == 0


if __name__ == '__main__':
    import sys
    sys.exit(0 if _run_all() else 1)
