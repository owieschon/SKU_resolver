"""Catalog index — interface between the translator pipeline and a tenant's
catalog state (typically backed by their ERP).

Architecture
------------
the platform is multi-tenant. Each customer's catalog state lives in their own
ERP environment (Microsoft Dynamics NAV for the example tenant; Acumatica or
NetSuite for future tenants). The translator pipeline shouldn't know or
care which ERP backs the catalog — it just needs a stable interface that
exposes membership, sales context, and proprietary-flag enforcement.

This module defines that interface as a Protocol (CatalogIndex). Two
implementations exist:

  - FixtureCatalogIndex (fixture_catalog.py): loads a CSV export of
    the catalog. Used for development, testing, and the regression suite.
    Production code paths do not reach this.

  - ERPCatalogIndex (erp_catalog.py): production-shape implementation,
    stubbed for now. Will be backed by a Supabase materialized view that's
    synced from the tenant's ERP. The OMA/MIA/PA agents wire to this in
    production.

Tenancy
-------
Each CatalogIndex instance represents one tenant's catalog state. A
deployment running multiple tenants holds multiple CatalogIndex
instances (one per tenant) and routes requests by tenant_id. Memory
(the rep-choice replay layer) and proprietary-flag enforcement are
already customer-scoped; the catalog index completes the per-tenant
isolation.

Data shape
----------
ParsedRow is the canonical shape for one catalog entry: the SKU string
itself, the parser's decoded fields, and the tenant-specific metadata
(sales counts, recency, proprietary flag). All CatalogIndex
implementations produce ParsedRows of the same shape so downstream
consumers (fuzzy matcher, disambiguator, OMA) don't care which ERP
the data came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, runtime_checkable

# ============================================================================
# Data shape
# ============================================================================

@dataclass
class ParsedRow:
    """One canonical catalog entry, parsed and enriched with tenant metadata.

    All CatalogIndex implementations produce rows of this exact shape. The
    parser fields come from ``part_number_parser.parse(sku)``; the tenant
    fields come from the ERP.

    Two SKUs are equal iff their ``sku`` strings are equal. The other
    fields are reference data, not identity.
    """
    # ----- Identity -----
    sku: str
    """The canonical SKU string. Always non-empty after construction."""

    # ----- Parser-derived fields -----
    pattern: str | None = None
    """Pattern name from the parser (e.g. 'parametric', 'elbow', 's_reducer').
    None if the parser couldn't classify the SKU."""

    family: str | None = None
    family_meaning: str | None = None
    diameter: float | None = None
    length: float | None = None
    angle: int | None = None
    leg1: float | None = None
    leg2: float | None = None
    body: str | None = None
    finish: str | None = None
    inlet_diameter: float | None = None
    outlet_diameter: float | None = None
    is_reducer: bool = False
    oem: str | None = None
    oem_meaning: str | None = None

    # ----- Tenant metadata -----
    description: str = ''
    """Free-text description from the ERP (item card)."""

    is_proprietary: bool = False
    """True iff this SKU is flagged as single-customer/restricted."""

    proprietary_customer: str | None = None
    """If known, the customer this proprietary SKU belongs to. Frequently None
    even when is_proprietary=True — NAV's PGC field marks restriction but
    customer attribution is often only inferrable from description or
    SKU prefix conventions."""

    sales_count: int = 0
    """Lifetime sales quantity (from the ERP). Zero if unknown or never sold."""

    sales_qty_year: int = 0
    """Sales quantity in the trailing year. Zero if unknown."""

    quantity_on_hand: int = 0
    """Current inventory level (from the ERP). Zero if unknown or out of stock."""

    is_obsolete: bool = False
    """True iff this SKU is marked obsolete (should not be sold)."""

    unit_price: float = 0.0
    """List unit price from the ERP (0.0 if unknown/unpriced)."""

    is_customer_facing: bool = False
    """True iff this SKU is in the customer-facing catalog scope: not obsolete,
    not proprietary, a real product group (not custom/component/raw/etc.), priced,
    and not description-flagged dead. ~3,000 of the ~10,700 ERP rows. The agent
    resolves/suggests within this scope; non-customer-facing rows stay in the
    index (for exact lookup + proprietary enforcement) but are never suggested."""

    raw_parser_result: dict[str, Any] = field(default_factory=dict)
    """The complete parser result dict, kept for downstream consumers
    that need fields not promoted to top-level attributes."""

    raw_erp_row: dict[str, Any] = field(default_factory=dict)
    """The complete raw ERP row, kept for audit and for fields not yet
    promoted to top-level attributes."""

    def matches_field(self, field_name: str, value: Any, *, tolerance: float = 0.001) -> bool:
        """Compare a candidate value against this row's field.

        Used by the disambiguator to score candidates. Returns True if
        ``value`` matches the row's ``field_name``, False otherwise.
        Numeric comparisons use ``tolerance``; string comparisons are
        case-insensitive.
        """
        own = getattr(self, field_name, None)
        if own is None or value is None:
            return False
        if isinstance(own, (int, float)) and isinstance(value, (int, float)):
            return abs(float(own) - float(value)) < tolerance
        return str(own).upper() == str(value).upper()


# ============================================================================
# Interface
# ============================================================================

@runtime_checkable
class CatalogIndex(Protocol):
    """Protocol that all catalog implementations must satisfy.

    Methods are grouped by concern. All implementations are read-only from
    the translator's perspective; mutation happens only via ``reload()``,
    which refreshes the index from the underlying ERP.

    Tenancy
    -------
    Each instance represents one tenant. Calls don't take a tenant_id
    parameter; the binding is established at construction.

    Thread safety
    -------------
    Implementations should be safe for concurrent reads after construction.
    ``reload()`` is the only mutating operation; callers must coordinate
    if reload can race with reads (typically by holding a lock or by
    swapping references atomically).
    """

    # ----- Tenant identity -----
    def tenant_id(self) -> str:
        """Stable identifier for this tenant (e.g. 'tenant_001')."""
        ...

    # ----- Membership -----
    def is_canonical(self, sku: str) -> bool:
        """True iff ``sku`` is an active SKU in this tenant's catalog.

        Case-insensitive. Excludes obsolete and battery SKUs.
        """
        ...

    def lookup(self, sku: str) -> ParsedRow | None:
        """Return the ParsedRow for ``sku``, or None if not in catalog.

        Case-insensitive lookup. Returns None for obsolete/battery SKUs
        even if they exist in the ERP.
        """
        ...

    # ----- Bulk access -----
    def parsed_rows(self) -> Iterator[ParsedRow]:
        """Iterate over all active ParsedRows in this catalog.

        Order is not specified. Used by index-builders (fuzzy matcher,
        disambiguator) at startup.
        """
        ...

    def all_skus(self) -> list[str]:
        """Snapshot list of all active SKU strings.

        Convenience for callers that need just the strings (e.g. fuzzy
        matcher index construction).
        """
        ...

    # ----- Bucketed access (for disambiguation) -----
    def bucket(
        self,
        family: str | None = None,
        diameter: float | None = None,
    ) -> list[ParsedRow]:
        """Return all rows matching the given family and/or diameter.

        Both args optional. If both are None, returns all rows. If only
        family, returns all rows in that family (any diameter). If both,
        returns the narrowest bucket.
        """
        ...

    def family_prefix_bucket(self, prefix: str) -> list[ParsedRow]:
        """Return all rows whose SKU begins with ``prefix`` (uppercase letters).

        Used by the fuzzy matcher. Returns rows in arbitrary order.
        """
        ...

    # ----- Lifecycle -----
    def reload(self) -> None:
        """Refresh the index from the underlying ERP.

        After return, all subsequent calls reflect the latest ERP state.
        Implementations should not partially expose a half-loaded index;
        either the reload completes fully or the previous state is
        preserved.
        """
        ...

    # ----- Diagnostics -----
    def size(self) -> int:
        """Number of active ParsedRows currently indexed."""
        ...


# ============================================================================
# Helpers — shared by implementations
# ============================================================================

def family_prefix_for(sku: str) -> str:
    """Compute the family-letter prefix used by the fuzzy matcher.

    Strategy: take leading uppercase letters until first digit. Numeric-
    leading SKUs go to the 'NUM' bucket. Empty input goes to 'OTHER'.

    Examples
    --------
    >>> family_prefix_for('K5-24SBC')
    'K'
    >>> family_prefix_for('SBR6-108EXC')
    'SBR'
    >>> family_prefix_for('15238171A')
    'NUM'
    """
    if not sku:
        return 'OTHER'
    upper = sku.strip().upper()
    if not upper:
        return 'OTHER'
    if upper[0].isdigit():
        return 'NUM'
    prefix_chars = []
    for c in upper:
        if c.isalpha():
            prefix_chars.append(c)
        else:
            break
    return ''.join(prefix_chars) if prefix_chars else 'OTHER'


# Canonical proprietary-flag spellings observed in NAV's PGC field.
# Loading code normalizes any of these to is_proprietary=True.
PROPRIETARY_SPELLINGS = frozenset({
    'PROPRIETARY', 'PROPRIETAR', 'PROPRITARY', 'PROPIETARY',
    'PROPRIATAR', 'PROPRIEETA', 'PROPRIERAR',
})


def is_proprietary_marker(value: Any) -> bool:
    """Return True iff ``value`` is one of NAV's proprietary-flag spellings.

    Tolerant to whitespace, casing, and the six known typo variants.
    """
    if not value:
        return False
    cleaned = str(value).strip().upper()
    if not cleaned:
        return False
    # Direct match against known spellings
    if cleaned in PROPRIETARY_SPELLINGS:
        return True
    # Also catch the substring (e.g. "PROPRIETARY - HP") as a forgiveness mode,
    # but only if the leading 5 chars are 'PROPR' to avoid false positives.
    if cleaned.startswith('PROPR') and 'PROP' in cleaned:
        return True
    return False


# Inventory-posting-group values that mark a SKU as obsolete or out-of-scope.
# Translator should not return these as resolved candidates.
EXCLUDED_IPG_VALUES = frozenset({
    'OBSOLETE', 'BATTERY',
})


def is_excluded_ipg(value: Any) -> bool:
    """True iff this Inventory Posting Group value should be excluded."""
    if not value:
        return False
    cleaned = str(value).strip().upper()
    return any(marker in cleaned for marker in EXCLUDED_IPG_VALUES)


# Product-group-code values that mark a SKU as out-of-scope (separate from IPG).
EXCLUDED_PGC_VALUES = frozenset({
    'BATTERY',  # Batteries aren't part of the exhaust catalog
})


def is_excluded_pgc(value: Any) -> bool:
    """True iff this Product Group Code value should be excluded."""
    if not value:
        return False
    cleaned = str(value).strip().upper()
    return any(marker in cleaned for marker in EXCLUDED_PGC_VALUES)


# Product-group-code markers that are NOT customer-facing catalog scope: one-off
# custom work, internal components, raw material, and non-exhaust merch. (Matched
# as substrings so the ERP's many typo variants — COMPONANT, RAW MATE1 — are
# caught.) Everything else with a present PGC is treated as a real product line.
NON_CUSTOMER_FACING_PGC = (
    'CUSTOM', 'COMPON', 'RAW', 'BATTERY', 'APPAREL', 'LITERATURE',
    'CONTAINER', 'PROMO', 'MOTOR', 'MERCH', 'CATALOG', 'SAMPLE',
)

# Description language that marks a row dead even when the ERP flags missed it
# (e.g. SB2-6745GM "INACTIVE, NOT SELLING AT THIS TIME").
_DEAD_DESC_MARKERS = (
    'DO NOT SELL', 'DO NOT USE', 'DO NOT ORDER', 'NOT SELLING', 'INACTIVE',
)


def is_customer_facing(*, pgc: Any, unit_price: float, description: str,
                       is_proprietary: bool, is_obsolete: bool) -> bool:
    """The customer-facing catalog scope (tenant-001, ~3,000 SKUs): a real,
    priced product a caller can actually buy — not obsolete, not proprietary, a
    genuine product group, with a price, and not description-flagged dead."""
    if is_obsolete or is_proprietary:
        return False
    if (unit_price or 0) <= 0:
        return False
    p = str(pgc or '').strip().upper()
    if not p or any(m in p for m in NON_CUSTOMER_FACING_PGC):
        return False
    d = str(description or '').upper()
    if any(m in d for m in _DEAD_DESC_MARKERS):
        return False
    return True
