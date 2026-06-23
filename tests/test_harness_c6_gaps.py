"""C6 — totality (mapped ∪ gapped = contract, mechanically) + classification
behavior under revocation: a permission gap is NOT the same finding as a
known standard-surface absence, and revoking a grant must change the
classification, not the count.
"""
from __future__ import annotations

from erp_harness import CANONICAL_CONTRACT, GapClass, MappingState
from harness_fixtures import onboard


# --- smoke ----------------------------------------------------------------------

def test_totality_mapped_union_gapped_equals_contract():
    _, _, _, result = onboard()
    verified = {m.mapping.contract_field
                for m in result.profile.verified_mappings()}
    gapped = {g.contract_field for g in result.profile.gaps}
    assert verified | gapped == {c.name for c in CANONICAL_CONTRACT}
    assert not (verified & gapped), 'a field is both mapped and gapped'


def test_alternative_entity_gap_when_data_lives_on_unexpected_entity():
    """R0 #6: ALTERNATIVE_ENTITY is wired, not a dead enum. When the expected
    role object yields no verified mapping but a candidate field exists on a
    different discovered entity, the gap names the alternative for remap."""
    from erp_harness.gaps import CANONICAL_CONTRACT, classify_unmapped
    from erp_harness.models import (
        EntitySchema, FieldSchema, GapClass, SurfaceProfile, ERPClass,
    )
    # 'items' exists but carries no sku-like field; 'itemMaster' carries it.
    items = EntitySchema('items', (FieldSchema('blob', 'Edm.String', True, 1, False),))
    item_master = EntitySchema('itemMaster',
                               (FieldSchema('itemNo', 'Edm.String', False, 1, False),))
    surface = SurfaceProfile(entities=(items, item_master),
                             field_profiles=(), fingerprint='f' * 16)
    sku_field = next(c for c in CANONICAL_CONTRACT if c.name == 'sku')
    gap = classify_unmapped(sku_field, erp_class=ERPClass.BC_SAAS,
                            surface=surface, granted_objects={'items', 'itemMaster'})
    assert gap.gap_class is GapClass.ALTERNATIVE_ENTITY
    assert 'itemMaster' in gap.detail and 'itemNo' in gap.detail


def test_documented_bc_absences_classified_custom_api_page():
    _, _, _, result = onboard()
    by_name = {g.contract_field: g for g in result.profile.gaps}
    for fld in ('unit_cost_ledger', 'vendor_lead_time_days'):
        assert by_name[fld].gap_class is GapClass.CUSTOM_API_PAGE_REQUIRED
        assert 'custom AL API page' in by_name[fld].detail


# --- E2E behavioral: revocation reclassifies ----------------------------------------

def test_e2e_revoked_grant_reclassifies_to_permission_gap():
    full_grants = {'metadata', 'items', 'customers', 'salesOrders', 'status'}
    _, _, _, baseline = onboard(granted=set(full_grants))
    base_gaps = {g.contract_field: g.gap_class for g in baseline.profile.gaps}

    _, _, _, revoked = onboard(granted=full_grants - {'salesOrders'})
    rev_gaps = {g.contract_field: g.gap_class for g in revoked.profile.gaps}

    # order fields flip to PERMISSION_GAP...
    for fld in ('order_id', 'order_date'):
        assert fld not in base_gaps                 # verified at baseline
        assert rev_gaps[fld] is GapClass.PERMISSION_GAP
    # ...while the documented absences keep their classification.
    for fld in ('unit_cost_ledger', 'vendor_lead_time_days'):
        assert rev_gaps[fld] is GapClass.CUSTOM_API_PAGE_REQUIRED
    # and item/customer mappings are untouched (no cross-contamination).
    verified = {m.mapping.contract_field
                for m in revoked.profile.verified_mappings()}
    assert {'sku', 'description', 'quantity_on_hand'} <= verified
