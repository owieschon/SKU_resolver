"""C4 — grammar-readiness smoke + the planted-vocabulary E2E.

The planted fault: seed the twin's items with a synthetic family the
vocabulary has never seen ('zorbtube' -> ZQ-prefix). The module must FIND
the candidate from co-occurrence — proving it discovers vocabulary rather
than echoing known aliases.
"""
from __future__ import annotations

from harness_fixtures import make_rig

from erp_harness import analyze_items
from erp_harness.discovery import fetch_all_rows


def _rows(enforcer, limit=200):
    return fetch_all_rows(enforcer, 'items', limit=limit)


# --- smoke ----------------------------------------------------------------------

def test_report_reproduces_known_family_structure():
    _, _, enforcer = make_rig(item_limit=500)
    report = analyze_items(_rows(enforcer, 500), sku_field='number',
                           description_field='displayName')
    assert report.total_items == 500
    assert report.decoded / report.total_items > 0.9
    assert report.family_histogram         # real families present
    # Undecoded SKUs produce SME questions ordered by volume resolved.
    volumes = [q.skus_resolved for q in report.sme_questions]
    assert volumes == sorted(volumes, reverse=True)
    for q in report.sme_questions:
        assert q.example_skus and 'resolves' in q.question


# --- E2E behavioral: planted vocabulary ----------------------------------------------

def test_e2e_planted_family_word_surfaces_as_candidate():
    _, twin, enforcer = make_rig(item_limit=100)
    items = twin._entities['items']
    # Plant a synthetic family: S-tube SKUs whose descriptions carry a
    # made-up family word. 'S' decodes under the real grammar, so the
    # co-occurrence engine has a family to attach the word to.
    for i in range(6):
        items.rows.append({
            'number': f'S{i + 2}-99EXA',
            'displayName': f'ZORBTUBE straight {i + 2}" raw stock',
            'inventoryQty': 5, 'blocked': False,
            'lastModifiedDateTime': '2026-06-01T12:00:00Z'})
    report = analyze_items(_rows(enforcer, 200), sku_field='number',
                           description_field='displayName')
    planted = [c for c in report.vocabulary_candidates
               if c.phrase == 'zorbtube']
    assert planted, 'planted family word was not discovered'
    assert planted[0].family_code == 'S'
    assert planted[0].support >= 3
    assert planted[0].distinctiveness >= 0.8
