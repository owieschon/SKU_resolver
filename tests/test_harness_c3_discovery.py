"""C3 — discovery smoke + the fault-injection check mutation matrix.

Five fault classes planted on the twin; the harness must name ALL of them.
A detector that has never caught a planted fault is unproven (spec §2).
"""
from __future__ import annotations

from erp_harness import surface_fingerprint
from erp_harness.discovery import crawl_metadata, discover
from erp_twin import faults
from harness_fixtures import make_rig


# --- smoke ----------------------------------------------------------------------

def test_discovery_finds_all_granted_entities_and_validates():
    _, twin, enforcer = make_rig(item_limit=150)
    surface = discover(enforcer)
    assert {e.name for e in surface.entities} == {'items', 'customers',
                                                  'salesOrders'}
    items = surface.entity('items')
    assert items.field('number').edm_type == 'Edm.String'
    sku_profile = surface.profile_for('items', 'number')
    assert sku_profile.distinct_ratio == 1.0       # SKUs are unique
    assert len(surface.fingerprint) == 16


def test_discovery_is_reproducible():
    _, _, e1 = make_rig(item_limit=100)
    _, _, e2 = make_rig(item_limit=100)
    s1, s2 = discover(e1), discover(e2)
    assert s1.fingerprint == s2.fingerprint
    assert s1.entities == s2.entities


def test_hidden_entities_never_appear_on_the_surface():
    _, twin, enforcer = make_rig(item_limit=50)
    surface = discover(enforcer)
    assert surface.entity('valueEntries') is None   # hidden = invisible


# --- E2E behavioral: the mutation matrix --------------------------------------------

def test_e2e_mutation_matrix_every_planted_fault_is_named():
    _, twin, enforcer = make_rig(item_limit=100)
    baseline = crawl_metadata(enforcer)
    baseline_fp = surface_fingerprint(baseline)

    planted = [
        faults.rename_field(twin, 'items', 'displayName', 'itemDescription'),
        faults.add_custom_field(twin, 'items', 'GRX_LegacyCode',
                                field_number=50001),
        faults.change_type(twin, 'items', 'inventoryQty', 'Edm.String'),
        faults.drop_nav_property(twin, 'items', 'itemCategory'),
        faults.hide_entity(twin, 'salesOrders'),
    ]
    assert len(planted) == 5

    current = crawl_metadata(enforcer)
    assert surface_fingerprint(current) != baseline_fp   # drift is visible

    from erp_harness.drift import diff_surfaces
    changes = '\n'.join(diff_surfaces(baseline, current))

    assert 'removed field items.displayName' in changes          # rename: old side
    assert 'added field items.itemDescription' in changes        # rename: new side
    assert 'added field items.GRX_LegacyCode' in changes
    assert '(tenant custom range)' in changes                    # 50000+ flagged
    assert 'changed items.inventoryQty: Edm.Decimal -> Edm.String' in changes
    assert "entity 'salesOrders' no longer on surface" in changes


def test_e2e_custom_field_discovery_flags_the_50000_range():
    _, twin, enforcer = make_rig(item_limit=50)
    faults.add_custom_field(twin, 'items', 'GRX_RouteCode', field_number=50007)
    surface = discover(enforcer)
    custom = [f for f in surface.entity('items').fields if f.is_custom]
    assert [f.name for f in custom] == ['GRX_RouteCode']
    assert custom[0].field_number == 50007
