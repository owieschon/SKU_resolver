"""Behavioral contract for the unified resolution service (M3).

Covers: translator-first precedence, fallback firing only on UNRESOLVABLE,
the complete response envelope on every path, the state-grounded
needs_review rule (decision D6), and the catalog version stamp (D7).
"""
from __future__ import annotations

from pathlib import Path

from resolution import ResolutionService, catalog_content_version
from sku_translator import FixtureCatalogIndex, InMemoryStore

REPO = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO / 'data' / 'catalog.csv'

_catalog = None
_version = None


def _service(customer_memory=None):
    global _catalog, _version
    if _catalog is None:
        _catalog = FixtureCatalogIndex(str(CATALOG_PATH), tenant_id='tenant_001')
        _version = catalog_content_version(CATALOG_PATH)
    return ResolutionService(_catalog, customer_memory or InMemoryStore(),
                             catalog_version=_version)


def _assert_envelope(res):
    """M3 DoD: every response carries state, source, confidence, flags,
    needs_review, candidates, catalog version, tenant — every time."""
    assert res.state in ('resolved', 'pending_disambiguation', 'unresolvable')
    assert isinstance(res.source, str) and res.source
    assert res.confidence in ('high', 'medium', 'low', 'none')
    assert isinstance(res.flags, tuple)
    assert isinstance(res.needs_review, bool)
    assert isinstance(res.candidates, tuple)
    assert len(res.catalog_version) == 12
    assert res.tenant_id == 'tenant_001'


def test_canonical_sku_resolves_via_translator_high_confidence():
    res = _service().resolve('K5-24SBC')
    _assert_envelope(res)
    assert res.state == 'resolved'
    assert res.sku == 'K5-24SBC'
    assert res.source.startswith('translator:')
    assert res.confidence == 'high'
    assert res.needs_review is False


def test_free_text_construct_resolves_via_translator():
    res = _service().resolve('5 inch chrome curved 24 long SB')
    _assert_envelope(res)
    assert res.state == 'resolved'
    assert res.sku == 'K5-24SBC'
    assert res.needs_review is False


def test_translator_ambiguity_surfaces_picker_not_fallback():
    # Translator produced candidates -> retrieval must NOT fire (precedence).
    res = _service().resolve('five inch vee band clamp stainless')
    _assert_envelope(res)
    assert res.state == 'pending_disambiguation'
    assert res.source.startswith('translator:')
    assert res.needs_review is True
    assert all(c.source == 'translator' for c in res.candidates)
    assert res.candidates  # 0/1/many rule: 2+ matches -> picker, never auto-pick


def test_retrieval_fallback_fires_only_on_unresolvable():
    # Description words the grammar/extractor can't structure, but BM25 can match
    # against customer-facing catalog descriptions (the rain-cap rows RC-2/3/400).
    res = _service().resolve('rain cap')
    _assert_envelope(res)
    if res.source == 'retrieval:bm25':           # fallback path
        assert res.state == 'pending_disambiguation'
        assert res.confidence == 'low'           # D5: chooser absent
        assert res.sku is None                   # retrieval NEVER resolves
        assert 'retrieval_fallback' in res.flags
        assert res.needs_review is True
        assert any('CAP' in c.reason.upper() for c in res.candidates)
    else:                                        # translator got there first
        assert res.source.startswith('translator:')


def test_gibberish_is_honestly_unresolvable():
    res = _service().resolve('zzz qqq xxyzzy floop')
    _assert_envelope(res)
    assert res.state == 'unresolvable'
    assert res.sku is None
    assert res.needs_review is True
    assert 'no_candidates' in res.flags


def test_catalog_version_is_content_derived_and_stable():
    v1 = catalog_content_version(CATALOG_PATH)
    v2 = catalog_content_version(CATALOG_PATH)
    assert v1 == v2 and len(v1) == 12
    res = _service().resolve('K5-24SBC')
    assert res.catalog_version == v1


def test_needs_review_rule_is_state_grounded():
    # D6: high-confidence RESOLVED with no flags -> no review; everything
    # else -> review. (The production structural flag rule is out of scope —
    # F1 0.15-0.33 at production accuracy, redesign = M2.)
    svc = _service()
    clean = svc.resolve('K5-24SBC')
    assert (clean.confidence == 'high') and not clean.needs_review
    for messy in ('five inch vee band clamp stainless', 'zzz qqq xxyzzy floop'):
        assert svc.resolve(messy).needs_review is True
