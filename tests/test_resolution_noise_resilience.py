"""Noise resilience: the engine never invents under realistic bad input and
degrades gracefully (pending/unresolvable) rather than guessing.

Companion to scripts/noise_resilience_audit.py (which runs a larger sample and
writes state/noise_resilience_audit.json); this asserts the GUARANTEES on a
CI-sized sample. The point it makes that the round-trip audit cannot: round-trip
feeds clean canonical SKUs (so 96.96% is "high by construction"); this feeds
typo'd / OCR-mangled / under-specified input and shows the engine stays honest.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from noise_resilience_audit import run_audit

from resolution import ResolutionService, catalog_content_version
from sku_translator import FixtureCatalogIndex, InMemoryStore

CATALOG = Path(__file__).resolve().parent.parent / 'data' / 'catalog.csv'


def _report(n: int = 150, seed: int = 4242) -> dict:
    catalog = FixtureCatalogIndex(str(CATALOG), tenant_id='audit')
    svc = ResolutionService(catalog, InMemoryStore(),
                            catalog_version=catalog_content_version(str(CATALOG)))
    rng = random.Random(seed)
    skus = rng.sample(sorted(catalog.all_skus()), n)
    return run_audit(catalog, svc, skus, rng)


def test_never_invents_under_noise():
    """The core guarantee, held under noise: no resolved SKU and no surfaced
    candidate is outside the catalog — across typos, OCR slips, and partials."""
    r = _report()
    assert r['never_invent_failures'] == 0, r['invent_examples']
    assert r['ok'] is True


def test_partial_specs_degrade_to_pending():
    """Under-specified input (a dropped dimension) should mostly decline to
    guess — pending/unresolvable, not a confident wrong SKU."""
    r = _report()
    assert r['by_class']['partial']['graceful_degradation_rate'] > 0.5


def test_typo_noise_still_resolves_a_meaningful_share():
    """Regression floor: the engine recovers a real share of typo'd SKUs (via
    fuzzy/normalization) while degrading gracefully on the rest — never inventing."""
    r = _report()
    assert r['by_class']['typo']['resolution_rate'] > 0.15
    assert r['by_class']['typo']['graceful_degradation_rate'] > 0.4
