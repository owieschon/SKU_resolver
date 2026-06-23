"""Production CatalogIndex implementation — stub.

This is the production-shape implementation that wires the platform to a tenant's
ERP environment. It's stubbed in this session because the production wiring
(NAV → Supabase materialized view → this class) doesn't exist yet.

When this gets implemented in the production codebase
------------------------------------------
The class signature below is the contract. Concretely:

  - The constructor takes a tenant_id and a Supabase client (or DuckDB
    handle, depending on which storage tier we settle on).

  - reload() issues a SELECT against the materialized view that mirrors
    the tenant's NAV ITEM_LEDGER, ITEM, and SALES_INVOICE_LINE tables.
    The view includes the parser fields pre-computed at sync time so
    reload() doesn't need to call parse() on every row at query time.

  - is_canonical / lookup / bucket are simple in-memory lookups against
    the loaded snapshot. The same shape as FixtureCatalogIndex, just
    with a different data source.

  - The index does NOT call out to the ERP per-query. The latency budget
    for translate() is 50ms p99; an ERP round-trip blows that. All query
    methods are O(1) or O(B) against in-memory state.

Sync strategy (decided when we get to it)
-----------------------------------------
Three options on the table:

  1. NAV → Azure Service Bus → Supabase webhook → the platform materialized view
     (real-time, complex, requires the example tenant's IT to enable change data
     capture)

  2. NAV → nightly bulk export → Supabase table replace → the platform view
     (simple, predictable, 24-hour data lag)

  3. NAV → polling agent (every N minutes) → the platform view
     (intermediate latency, intermediate complexity)

For the first production deployment with the example tenant we'll likely start
at option 2 and migrate to option 1 if 24h lag becomes a problem. None
of that affects the CatalogIndex contract — it just changes how often
reload() gets called.

Why this is a stub now
----------------------
- No Supabase materialized view exists yet for the example tenant
- No NAV connection is established
- The fixture is the only source of catalog data we can use locally

Calling any method on ERPCatalogIndex raises NotImplementedError so that
accidental wiring during testing produces a loud, immediate failure rather
than a silently-empty catalog.
"""
from __future__ import annotations

from typing import Iterator

try:
    from sku_translator.catalog_index import ParsedRow
except ImportError:
    from catalog_index import ParsedRow


class ERPCatalogIndex:
    """Production-shape CatalogIndex backed by ERP/Supabase. NOT YET IMPLEMENTED.

    The class signature here is the contract that the production
    implementation must satisfy. Calling any method raises
    NotImplementedError so that production code paths can't accidentally
    fall through to silent emptiness during the migration period.

    Attributes
    ----------
    tenant_id : the tenant this index represents (e.g. 'tenant_001')
    supabase_url : the Supabase project URL backing this tenant
    materialized_view : the name of the SQL view backing this tenant's catalog

    Implementation plan (when this gets built)
    ------------------------------------------
    1. __init__ stores tenant_id and Supabase connection params; calls
       reload() if eager=True.
    2. reload() issues a single SELECT against the materialized view,
       loads all active rows into memory as ParsedRow objects, and
       atomically swaps internal state.
    3. All other methods read from the in-memory snapshot. No per-query
       network calls.
    4. The materialized view is responsible for excluding obsolete and
       battery rows (via WHERE clauses in its definition), so this class
       doesn't need to filter at load time.
    5. The materialized view is responsible for pre-computing parser
       fields (family, diameter, length, etc.) so reload() doesn't need
       to call parse() per row. This is a meaningful latency win at
       scale (10k rows × parse() = ~500ms; pre-computed = <50ms).
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        supabase_url: str | None = None,
        supabase_key: str | None = None,
        materialized_view: str = 'catalog_active',
        eager: bool = True,
    ) -> None:
        self._tenant_id = tenant_id
        self._supabase_url = supabase_url
        self._supabase_key = supabase_key
        self._materialized_view = materialized_view
        if eager:
            self.reload()

    def reload(self) -> None:
        raise NotImplementedError(
            'ERPCatalogIndex is not yet implemented. '
            'Use FixtureCatalogIndex for development and tests. '
            'Production implementation pending the NAV → Supabase sync.'
        )

    def tenant_id(self) -> str:
        return self._tenant_id

    def is_canonical(self, sku: str) -> bool:
        raise NotImplementedError('ERPCatalogIndex pending production wiring')

    def lookup(self, sku: str) -> ParsedRow | None:
        raise NotImplementedError('ERPCatalogIndex pending production wiring')

    def parsed_rows(self) -> Iterator[ParsedRow]:
        raise NotImplementedError('ERPCatalogIndex pending production wiring')

    def all_skus(self) -> list[str]:
        raise NotImplementedError('ERPCatalogIndex pending production wiring')

    def bucket(
        self,
        family: str | None = None,
        diameter: float | None = None,
    ) -> list[ParsedRow]:
        raise NotImplementedError('ERPCatalogIndex pending production wiring')

    def family_prefix_bucket(self, prefix: str) -> list[ParsedRow]:
        raise NotImplementedError('ERPCatalogIndex pending production wiring')

    def size(self) -> int:
        raise NotImplementedError('ERPCatalogIndex pending production wiring')
