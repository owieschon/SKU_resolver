"""Fixture CatalogIndex — CSV-backed implementation for development and tests.

This is a DEVELOPMENT FIXTURE. Production code paths use ERPCatalogIndex,
which is backed by a materialized view synced from the tenant's ERP. The
fixture exists so:

  - Integration tests can run against a known catalog snapshot without
    needing a live ERP connection.
  - Local development and demos can produce real translations.
  - The architecture stays accurate: any code that accepts a CatalogIndex
    accepts both the fixture and the production version equivalently.

Loading the fixture
-------------------
The CSV is expected to have at minimum these columns (matched by header
name — column order doesn't matter):

  - 'No.' : the SKU string
  - 'Description' : free text
  - 'Product Group Code' : a category label (proprietary / battery / etc.)
  - 'Inventory Posting Group' : 'OBSOLETE', 'BATTERY', or a category
  - 'Sales (Qty.)' : lifetime sales count (optional; defaults to 0)
  - 'Sales (Qty.) - Year' : trailing-year sales count (optional; default 0)
  - 'Quantity on Hand' : current inventory (optional; default 0)

Other columns are preserved on raw_erp_row but not promoted.

Tenant identifier
-----------------
The fixture takes a tenant_id at construction. Defaults to 'tenant_fixture'
so it can never be confused with a real tenant id in logs.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

try:
    from sku_translator.catalog_index import (
        ParsedRow,
        family_prefix_for,
        is_customer_facing,
        is_excluded_ipg,
        is_excluded_pgc,
        is_proprietary_marker,
    )
    from sku_translator.part_number_parser import parse
except ImportError:
    from catalog_index import (
        ParsedRow,
        family_prefix_for,
        is_customer_facing,
        is_excluded_ipg,
        is_excluded_pgc,
        is_proprietary_marker,
    )
    from part_number_parser import parse


# Column-header recognition. We look up by header name (case-insensitive)
# rather than position so the loader survives column reordering in the export.
_REQUIRED_COLUMNS = {'No.', 'Description'}
_OPTIONAL_COLUMNS = {
    'Product Group Code',
    'Inventory Posting Group',
    'Sales (Qty.)',
    'Sales (Qty.) - Year',
    'Quantity on Hand',
}


class FixtureCatalogIndex:
    """CSV-backed CatalogIndex implementation for development and tests.

    This is NOT for production use. Any code that depends on this class
    by name (rather than by the CatalogIndex protocol) is making a
    fixture-vs-production confusion.
    """

    def __init__(
        self,
        catalog_path: str | Path,
        *,
        tenant_id: str = 'tenant_fixture',
        eager: bool = True,
    ) -> None:
        """Build an index from a catalog CSV file.

        Parameters
        ----------
        catalog_path : path to the catalog CSV
        tenant_id : stable identifier for this tenant
        eager : if True (default), load and parse the catalog now; if False,
            defer until first query (rarely needed)
        """
        self._catalog_path = Path(catalog_path)
        self._tenant_id = tenant_id

        # Internal state — populated by reload()
        self._rows_by_sku: dict[str, ParsedRow] = {}
        # Case-insensitive SKU lookup (uppercase key -> canonical SKU)
        self._sku_case_map: dict[str, str] = {}
        # (family, diameter) -> rows
        self._by_family_diameter: dict[tuple[str | None, float | None], list[ParsedRow]] = \
            defaultdict(list)
        # family -> rows
        self._by_family: dict[str | None, list[ParsedRow]] = defaultdict(list)
        # family-letter prefix -> rows (for fuzzy bucket scoping)
        self._by_family_prefix: dict[str, list[ParsedRow]] = defaultdict(list)
        # All active rows
        self._all_rows: list[ParsedRow] = []

        if eager:
            self.reload()

    # ----- Lifecycle ----------------------------------------------------

    def reload(self) -> None:
        """Re-read the CSV and rebuild all indexes.

        Clears prior state. If the load fails, the index is left empty
        rather than half-loaded.
        """
        try:
            new_rows = self._load_rows()
        except Exception:
            # On error, leave the existing state untouched — caller can decide
            # whether to retry or fall back. We DON'T silently empty the index.
            raise

        # All-or-nothing swap of internal state
        self._rows_by_sku = {}
        self._sku_case_map = {}
        self._by_family_diameter = defaultdict(list)
        self._by_family = defaultdict(list)
        self._by_family_prefix = defaultdict(list)
        self._all_rows = []

        for row in new_rows:
            self._add_row(row)

    def _load_rows(self) -> list[ParsedRow]:
        """Read the catalog CSV, parse every row, return the active ParsedRows."""
        with open(self._catalog_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            fieldnames = {h.strip() for h in (reader.fieldnames or [])}

            # Verify required columns present
            for required in _REQUIRED_COLUMNS:
                if required not in fieldnames:
                    raise ValueError(
                        f'Required column {required!r} not found in CSV header. '
                        f'Found columns: {sorted(fieldnames)}'
                    )

            rows: list[ParsedRow] = []
            for row_data in reader:
                sku_raw = row_data.get('No.')
                if not sku_raw:
                    continue
                sku = str(sku_raw).strip()
                if not sku:
                    continue

                pgc = row_data.get('Product Group Code')
                ipg = row_data.get('Inventory Posting Group')

                # Filter excluded SKUs (obsolete, battery)
                if is_excluded_ipg(ipg) or is_excluded_pgc(pgc):
                    continue

                desc = str(row_data.get('Description') or '').strip()

                # Sales / inventory metadata
                sales_count = self._coerce_int(row_data.get('Sales (Qty.)'))
                sales_qty_year = self._coerce_int(row_data.get('Sales (Qty.) - Year'))
                quantity_on_hand = self._coerce_int(row_data.get('Quantity on Hand'))

                # Proprietary classification
                is_proprietary = is_proprietary_marker(pgc)

                # Customer-facing catalog scope: real, priced products only. Row
                # stays in the index either way; the flag scopes what the agent
                # resolves/suggests.
                unit_price = self._coerce_float(row_data.get('Unit Price')) or 0.0
                customer_facing = is_customer_facing(
                    pgc=pgc, unit_price=unit_price, description=desc,
                    is_proprietary=is_proprietary, is_obsolete=False)

                # Parse the SKU
                parsed = parse(sku)

                # Build the ParsedRow
                row = ParsedRow(
                    sku=sku,
                    pattern=parsed.get('pattern'),
                    family=parsed.get('family'),
                    family_meaning=parsed.get('family_meaning'),
                    diameter=self._coerce_float(parsed.get('diameter')),
                    length=self._coerce_float(parsed.get('length')),
                    angle=self._coerce_int_or_none(parsed.get('angle')),
                    leg1=self._coerce_float(parsed.get('leg1')),
                    leg2=self._coerce_float(parsed.get('leg2')),
                    body=parsed.get('body'),
                    finish=parsed.get('finish'),
                    inlet_diameter=self._coerce_float(parsed.get('inlet_diameter')),
                    outlet_diameter=self._coerce_float(parsed.get('outlet_diameter')),
                    is_reducer=bool(parsed.get('is_reducer')),
                    oem=parsed.get('oem'),
                    oem_meaning=parsed.get('oem_meaning'),
                    description=desc,
                    is_proprietary=is_proprietary,
                    proprietary_customer=parsed.get('proprietary_customer'),
                    sales_count=sales_count,
                    sales_qty_year=sales_qty_year,
                    quantity_on_hand=quantity_on_hand,
                    is_obsolete=False,  # Already filtered above
                    unit_price=unit_price,
                    is_customer_facing=customer_facing,
                    raw_parser_result=dict(parsed),
                    raw_erp_row={
                        'pgc': pgc, 'ipg': ipg,
                        'description': desc,
                        'sales_count': sales_count,
                        'sales_qty_year': sales_qty_year,
                        'quantity_on_hand': quantity_on_hand,
                    },
                )
                rows.append(row)

        return rows

    @staticmethod
    def _coerce_int(value: Any) -> int:
        if value is None:
            return 0
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _coerce_int_or_none(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def _add_row(self, row: ParsedRow) -> None:
        """Insert a row into all internal indexes."""
        self._rows_by_sku[row.sku] = row
        self._sku_case_map[row.sku.upper()] = row.sku
        self._by_family[row.family].append(row)
        self._by_family_diameter[(row.family, row.diameter)].append(row)
        prefix = family_prefix_for(row.sku)
        self._by_family_prefix[prefix].append(row)
        self._all_rows.append(row)

    # ----- CatalogIndex protocol ---------------------------------------

    def tenant_id(self) -> str:
        return self._tenant_id

    def is_canonical(self, sku: str) -> bool:
        if not sku:
            return False
        return sku.strip().upper() in self._sku_case_map

    def lookup(self, sku: str) -> ParsedRow | None:
        if not sku:
            return None
        canonical = self._sku_case_map.get(sku.strip().upper())
        if canonical is None:
            return None
        return self._rows_by_sku[canonical]

    def parsed_rows(self) -> Iterator[ParsedRow]:
        return iter(self._all_rows)

    def all_skus(self) -> list[str]:
        return [r.sku for r in self._all_rows]

    def bucket(
        self,
        family: str | None = None,
        diameter: float | None = None,
    ) -> list[ParsedRow]:
        if family is not None and diameter is not None:
            return list(self._by_family_diameter.get((family, diameter), []))
        if family is not None:
            return list(self._by_family.get(family, []))
        if diameter is not None:
            # Less common — scan all rows by diameter
            return [r for r in self._all_rows if r.diameter == diameter]
        return list(self._all_rows)

    def family_prefix_bucket(self, prefix: str) -> list[ParsedRow]:
        return list(self._by_family_prefix.get(prefix.upper(), []))

    def size(self) -> int:
        return len(self._all_rows)

    # ----- Diagnostics -------------------------------------------------

    def __repr__(self) -> str:
        return (
            f'<FixtureCatalogIndex tenant={self._tenant_id!r} '
            f'path={self._catalog_path.name!r} size={self.size()}>'
        )
