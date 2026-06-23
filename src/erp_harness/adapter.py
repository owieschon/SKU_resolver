"""The deterministic adapter — the ONLY consumer of an approved profile.

Syncs the items entity through verified mappings into a CatalogIndex-shaped
store the SKU translator can resolve against. Refuses unapproved profiles
(no exceptions, no flags to bypass). The golden path's acceptance criterion
runs here: after sync, the translator's identity guarantee must hold against
the synced catalog.
"""
from __future__ import annotations

from collections import defaultdict

from sku_translator.catalog_index import ParsedRow, family_prefix_for
from sku_translator.part_number_parser import parse as parse_sku

from erp_harness.drift import DriftGuard
from erp_harness.enforcer import SafetyEnforcer
from erp_harness.discovery import fetch_all_rows
from erp_harness.models import TenantERPProfile


class UnapprovedProfileError(Exception):
    pass


class SyncedCatalogIndex:
    """CatalogIndex-protocol implementation over adapter-synced rows.
    Mirrors FixtureCatalogIndex's row construction (parser-enriched
    ParsedRow) so the translator behaves identically against synced data."""

    def __init__(self, tenant_id: str, rows: list[ParsedRow]) -> None:
        self._tenant = tenant_id
        self._rows = {r.sku.upper(): r for r in rows}
        self._buckets: dict[str, list[ParsedRow]] = defaultdict(list)
        for r in rows:
            self._buckets[family_prefix_for(r.sku)].append(r)

    def tenant_id(self) -> str:
        return self._tenant

    def is_canonical(self, sku: str) -> bool:
        return sku.upper() in self._rows

    def lookup(self, sku: str) -> ParsedRow | None:
        return self._rows.get(sku.upper())

    def parsed_rows(self):
        return iter(self._rows.values())

    def all_skus(self) -> list[str]:
        return [r.sku for r in self._rows.values()]

    def bucket(self, **kwargs) -> list[ParsedRow]:
        out = list(self._rows.values())
        for fld, val in kwargs.items():
            out = [r for r in out if r.matches_field(fld, val)]
        return out

    def family_prefix_bucket(self, prefix: str) -> list[ParsedRow]:
        return self._buckets.get(prefix.upper(), [])

    def reload(self) -> None:
        pass

    def size(self) -> int:
        return len(self._rows)


class AtomicCatalogRef:
    """A swappable holder for the live catalog index (R2 #7, atomic refresh).

    A resolution in flight grabs current() and holds that snapshot for the
    whole turn; a refresh builds a complete new index and swap()s the
    reference in one rebind. Readers never observe a half-built index — they
    see either the old snapshot in full or the new one in full. (Python name
    rebinding is atomic w.r.t. the GIL; the discipline is build-then-swap,
    never mutate-in-place.)"""

    def __init__(self, index: 'SyncedCatalogIndex') -> None:
        self._index = index

    def current(self) -> 'SyncedCatalogIndex':
        return self._index

    def swap(self, new_index: 'SyncedCatalogIndex') -> 'SyncedCatalogIndex':
        old, self._index = self._index, new_index
        return old


def _row_from_erp(r: dict, field_for: dict) -> ParsedRow:
    sku = str(r[field_for['sku'].source_field])
    parsed = parse_sku(sku)
    return ParsedRow(
        sku=sku, pattern=parsed.get('pattern'), family=parsed.get('family'),
        family_meaning=parsed.get('family_meaning'),
        diameter=parsed.get('diameter'), length=parsed.get('length'),
        body=parsed.get('body'), finish=parsed.get('finish'),
        description=str(r.get(field_for['description'].source_field) or ''),
        quantity_on_hand=int(float(
            r.get(field_for['quantity_on_hand'].source_field) or 0)),
        is_obsolete=bool(r.get(field_for['is_blocked'].source_field))
        if 'is_blocked' in field_for else False,
        raw_parser_result=parsed,
    )


def _item_field_map(profile: TenantERPProfile) -> dict:
    if profile.approval is None or not profile.approval.approved:
        raise UnapprovedProfileError(
            'sync refused: profile has no recorded approval. The review '
            'gate is not optional.')
    field_for = {r.mapping.contract_field: r.mapping
                 for r in profile.verified_mappings()
                 if r.mapping.entity == 'items'}
    missing = [f for f in ('sku', 'description', 'quantity_on_hand')
               if f not in field_for]
    if missing:
        raise UnapprovedProfileError(
            f'sync refused: required item mappings unverified: {missing}')
    return field_for


def sync_items(profile: TenantERPProfile, enforcer: SafetyEnforcer, *,
               tenant_id: str,
               drift_guard: DriftGuard | None = None) -> SyncedCatalogIndex:
    """Full sync: build a complete fresh index from all item rows."""
    field_for = _item_field_map(profile)
    if drift_guard is not None:
        drift_guard.check_or_halt(enforcer)   # C8: drift halts BEFORE any read
    rows = [_row_from_erp(r, field_for) for r in fetch_all_rows(enforcer, 'items')]
    return SyncedCatalogIndex(tenant_id, rows)


def sync_items_incremental(profile: TenantERPProfile, enforcer: SafetyEnforcer,
                           *, tenant_id: str, since: str,
                           prior: SyncedCatalogIndex,
                           drift_guard: DriftGuard | None = None
                           ) -> SyncedCatalogIndex:
    """Delta sync (R2 #7): fetch only rows modified after `since` (server-side
    $filter on lastModifiedDateTime) and merge them onto the prior snapshot,
    returning a NEW index for atomic swap. Cheaper than a full re-pull on a
    large catalog.

    Limitation, stated not hidden: this merges adds/updates by SKU; it does
    NOT detect deletions (a removed SKU lingers until the next full sync). A
    deletion-aware delta needs a tombstone feed the standard surface doesn't
    provide — flagged for the sync-phase, not silently assumed away.
    """
    field_for = _item_field_map(profile)
    if drift_guard is not None:
        drift_guard.check_or_halt(enforcer)
    changed = fetch_all_rows(enforcer, 'items',
                             extra_params={'$filter':
                                           f'lastModifiedDateTime gt {since}'})
    merged = {r.sku.upper(): r for r in prior.parsed_rows()}
    for raw in changed:
        row = _row_from_erp(raw, field_for)
        merged[row.sku.upper()] = row
    return SyncedCatalogIndex(tenant_id, list(merged.values()))
