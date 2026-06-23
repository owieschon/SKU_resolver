"""C6 — Gap Detector + the canonical contract.

Totality is the invariant: every contract field ends as exactly one of a
verified mapping or a NamedGap — mapped ∪ gapped = contract, checked
mechanically in the orchestrator and asserted again in tests. Silence is
structurally impossible.

The knowledge table (per ERP class) classifies *expected* absences — e.g.
BC's standard v2.0 surface has no cost-ledger or item-vendor API
(vendor-confirmed; erp-replica spec) — distinct from permission gaps, which
are detected empirically (the entity is in the manifest but the surface
403'd or omitted it).
"""
from __future__ import annotations

from erp_harness.models import (
    ContractField, ERPClass, GapClass, NamedGap, SurfaceProfile,
)

# The canonical contract: what the downstream layers (CatalogIndex shape,
# inventory, orders) require. Kinds drive C7's verification checks.
CANONICAL_CONTRACT: tuple[ContractField, ...] = (
    ContractField('sku',               'item',        'identifier', True),
    ContractField('description',       'item',        'text',       True),
    ContractField('quantity_on_hand',  'item',        'number',     True),
    ContractField('is_blocked',        'item',        'flag',       False),
    ContractField('row_modified_at',   'item',        'date',       False),
    ContractField('customer_id',       'customer',    'identifier', True),
    ContractField('customer_name',     'customer',    'text',       True),
    ContractField('order_id',          'sales_order', 'identifier', True),
    ContractField('order_date',        'sales_order', 'date',       True),
    # Known-absent on the BC standard surface (the documented gap class):
    ContractField('unit_cost_ledger',  'item',        'number',     False),
    ContractField('vendor_lead_time_days', 'item',    'number',     False),
)

# Entity role -> the API object that carries it (per ERP class).
ROLE_OBJECTS = {
    'item': 'items',
    'customer': 'customers',
    'sales_order': 'salesOrders',
}

# Knowledge table of expected absences per ERP class.
_KNOWN_ABSENT: dict[ERPClass, dict[str, tuple[GapClass, str]]] = {
    ERPClass.BC_SAAS: {
        'unit_cost_ledger': (
            GapClass.CUSTOM_API_PAGE_REQUIRED,
            'Value Entry (cost ledger) has no standard v2.0 API; requires '
            'a custom AL API page per tenant (vendor-confirmed).'),
        'vendor_lead_time_days': (
            GapClass.CUSTOM_API_PAGE_REQUIRED,
            'Item Vendor (lead time) has no standard v2.0 API; requires '
            'a custom AL API page per tenant (vendor-confirmed).'),
    },
    ERPClass.NAV_ONPREM: {},   # raw SQL exposes the tables; mappings expected
}


def classify_unmapped(contract_field: ContractField, *, erp_class: ERPClass,
                      surface: SurfaceProfile,
                      granted_objects: set[str]) -> NamedGap:
    """Why did this contract field end up without a verified mapping?"""
    known = _KNOWN_ABSENT.get(erp_class, {}).get(contract_field.name)
    if known is not None:
        return NamedGap(contract_field.name, known[0], known[1])

    obj = ROLE_OBJECTS.get(contract_field.entity_role, '')
    on_surface = surface.entity(obj) is not None

    # Before declaring a permission/availability gap, check whether the data
    # plausibly lives on a DIFFERENT discovered entity than the contract's
    # expected role object — the ALTERNATIVE_ENTITY case (e.g. a tenant that
    # carries item descriptions on an 'itemMaster' entity instead of 'items').
    alt = _find_alternative_entity(contract_field, surface, exclude=obj)
    if alt is not None:
        return NamedGap(
            contract_field.name, GapClass.ALTERNATIVE_ENTITY,
            f'{contract_field.name!r} was not verified on the expected entity '
            f'{obj!r}, but a candidate field {alt[1]!r} exists on {alt[0]!r} — '
            f'remap to the alternative entity and re-verify.')

    if not on_surface and obj not in granted_objects:
        return NamedGap(
            contract_field.name, GapClass.PERMISSION_GAP,
            f'entity {obj!r} is in the manifest but absent from the granted '
            f'surface — grant missing or revoked.')
    if not on_surface:
        return NamedGap(
            contract_field.name, GapClass.UNAVAILABLE,
            f'entity {obj!r} not exposed by this tenant\'s surface.')
    return NamedGap(
        contract_field.name, GapClass.UNAVAILABLE,
        f'no field on {obj!r} survived verification for '
        f'{contract_field.name!r} — candidates were rejected or absent.')


# Core token per contract field used for the alternative-entity scan. Kept
# local and minimal (not the explorer's full synonym table) — this answers
# only "does this data plausibly exist somewhere else?", a hint for the
# human reviewer, not an auto-remap.
_ALT_TOKENS = {
    'sku': ('number', 'itemno', 'itemcode', 'sku'),
    'description': ('description', 'displayname', 'itemname'),
    'quantity_on_hand': ('inventory', 'qtyonhand', 'quantityonhand'),
    'customer_id': ('customerno', 'custno', 'accountno'),
    'order_id': ('orderno', 'ordernumber'),
}


def _find_alternative_entity(contract_field: ContractField, surface,
                             *, exclude: str) -> tuple[str, str] | None:
    tokens = _ALT_TOKENS.get(contract_field.name)
    if not tokens:
        return None
    for ent in surface.entities:
        if ent.name == exclude:
            continue
        for f in ent.fields:
            norm = f.name.lower().replace('_', '')
            if any(tok in norm for tok in tokens):
                return (ent.name, f.name)
    return None
