"""Onboarding orchestrator — composes the ERP harness + the catalog decoder
under one workflow, and the standalone (no-ERP) catalog path.

Proves the contract between the two harnesses: the ERP harness names the item
master via VERIFIED mappings, and the decoder runs on those discovered fields
(not hardcoded ones).
"""
from __future__ import annotations

from harness_fixtures import BC, make_rig

from erp_harness import (
    CatalogGrammarReport,
    HeuristicExplorer,
    ItemMasterRef,
    LineCatalogSource,
    decode_catalog_source,
    identify_item_master,
    run_full_onboarding,
)


def test_full_onboarding_decodes_via_discovered_item_master():
    clock, twin, enforcer = make_rig(item_limit=3000)
    report = run_full_onboarding(BC, enforcer, HeuristicExplorer(), clock,
                                 item_limit=3000)
    # The decoder field names came from the harness's verified mapping, not
    # hardcoded — proving the two harnesses compose through a real contract.
    assert report.item_master == ItemMasterRef(
        entity='items', sku_field='number', description_field='displayName')
    assert isinstance(report.grammar, CatalogGrammarReport)
    assert report.grammar.total_items > 0
    # One combined review queue spanning both layers.
    assert report.review_queue


def test_identify_item_master_none_without_verified_sku():
    # A profile whose 'sku' mapping never verified can't drive the decoder.
    clock, twin, enforcer = make_rig(item_limit=200)
    report = run_full_onboarding(BC, enforcer, HeuristicExplorer(), clock,
                                 item_limit=200)
    # (Heuristic explorer verifies sku on the twin, so this one IS identified;
    # the None path is exercised directly below with an empty profile.)
    assert report.item_master is not None

    class _EmptyProfile:
        def verified_mappings(self):
            return ()
    assert identify_item_master(_EmptyProfile()) is None


def test_standalone_catalog_source_decodes_without_erp():
    # The pre-sales path: a catalog file straight into the decoder, no ERP.
    lines = [
        'WA902-01-1002  142689  Accessory Drive Gear  Fits CUMMINS® N14',
        'WA902-01-1004  190397  Sleeve Wear           Fits CUMMINS® NT855',
        'WA902-02-1400  144714  Air Compressor Valve  Fits CUMMINS® NTC',
        'WA901-17-6601  4W5739  Connecting Rod Bearing Fits CATERPILLAR® 3300',
        'WA903-01-1021  8929310 Accessory Drive Gear  Fits DETROIT® 60 Series',
    ]
    report = decode_catalog_source(LineCatalogSource(lines))
    wa = next(f for f in report.families if f.family_code == 'WA')
    assert wa.shape_mask == 'AN-N-N' and wa.member_count == 5
