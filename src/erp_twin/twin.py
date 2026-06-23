"""BC-shaped synthetic twin — the harness's validation target (decision D8).

An in-process Backend implementing the *documented* Business Central
behaviors the harness must survive (sources: erp-replica-research-spec
findings, 2026-06-03):

  - OData-style JSON entity endpoints with $skiptoken pagination
  - $metadata XML carrying tenant tableextension custom fields
    (field numbers >= 50000) — the ONLY surface where custom fields appear
  - 429 throttling with Retry-After once a per-minute ceiling is crossed
  - per-entity grant enforcement (403 on non-granted surfaces)
  - an append-only AUDIT LOG of every request *received* — including write
    attempts — so tests prove zero-writes AT THE DESTINATION, not from the
    harness's own journal
  - observable posting-queue lag (clock-driven) for the consistency probe

This is deliberately a test double with high behavioral fidelity, not a
Business Central emulator. The real container twin (erp-replica spec) is the
later integration target; this twin exists so the fault-injection check
matrix runs deterministically in CI. Fault injection lives in faults.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from erp_harness.transport import Clock, TransportRequest, TransportResponse

PAGE_SIZE = 100


@dataclass(frozen=True)
class TwinField:
    name: str
    edm_type: str
    nullable: bool
    field_number: int   # >= 50000 => tenant tableextension custom field


@dataclass
class TwinEntity:
    name: str
    fields: list[TwinField]
    rows: list[dict[str, Any]]
    nav_properties: list[str] = field(default_factory=list)
    hidden: bool = False   # exists in the ERP, not exposed on the API surface


@dataclass(frozen=True)
class AuditEntry:
    ts: float
    method: str
    path: str
    granted: bool
    status: int


class BCShapedTwin:
    """Backend implementation. Construct via erp_twin.seed.seeded_twin()."""

    def __init__(self, entities: list[TwinEntity], *, clock: Clock,
                 granted: set[str], throttle_per_minute: int | None = None,
                 posting_queue_depth: int = 0,
                 posting_drain_per_minute: int = 10) -> None:
        self._entities = {e.name: e for e in entities}
        self._clock = clock
        self._granted = set(granted)
        self._throttle = throttle_per_minute
        self._win_start = clock.now()
        self._win_count = 0
        self._posting_seeded = posting_queue_depth
        self._posting_drain = posting_drain_per_minute
        self._t0 = clock.now()
        self.audit_log: list[AuditEntry] = []

    # -- grant administration (the "IT admin" surface, used by tests) ----------

    def grant(self, object_name: str) -> None:
        self._granted.add(object_name)

    def revoke(self, object_name: str) -> None:
        self._granted.discard(object_name)

    # -- Backend ----------------------------------------------------------------

    def handle(self, req: TransportRequest) -> TransportResponse:
        status, payload = self._route(req)
        self.audit_log.append(AuditEntry(
            ts=self._clock.now(), method=req.method, path=req.path,
            granted=self._object_of(req.path) in self._granted, status=status))
        return payload if isinstance(payload, TransportResponse) else \
            TransportResponse(status=status, json=payload)

    def write_attempts(self) -> list[AuditEntry]:
        return [a for a in self.audit_log if a.method != 'GET']

    # -- routing ----------------------------------------------------------------

    def _route(self, req: TransportRequest) -> tuple[int, Any]:
        if self._throttled():
            return 429, TransportResponse(
                status=429, headers={'Retry-After': '2'},
                json={'error': 'rate limit exceeded'})
        obj = self._object_of(req.path)
        if obj not in self._granted:
            return 403, {'error': f'access to {obj!r} not granted'}
        if req.method != 'GET':
            # A real tenant would 4xx writes from a read-only principal; the
            # twin records the attempt either way — that record is the point.
            return 405, {'error': 'write received by twin (should never happen)'}
        if req.path == '$metadata':
            return 200, TransportResponse(status=200, text=self._metadata_xml())
        if obj == 'status':
            return 200, {'postingQueue': self._posting_pending()}
        ent = self._entities.get(obj)
        if ent is None or ent.hidden:
            return 404, {'error': f'no such entity {obj!r}'}
        return 200, self._page(ent, req)

    @staticmethod
    def _object_of(path: str) -> str:
        return 'metadata' if path == '$metadata' else path.split('?')[0].split('/')[0]

    def _throttled(self) -> bool:
        if self._throttle is None:
            return False
        now = self._clock.now()
        if now - self._win_start >= 60.0:
            self._win_start, self._win_count = now, 0
        self._win_count += 1
        return self._win_count > self._throttle

    # -- entity pages -------------------------------------------------------------

    def _page(self, ent: TwinEntity, req: TransportRequest) -> dict[str, Any]:
        source = self._apply_filter(ent.rows, req.params.get('$filter'))
        skip = int(req.params.get('$skiptoken', '0'))
        top = min(int(req.params.get('$top', str(PAGE_SIZE))), PAGE_SIZE)
        rows = source[skip:skip + top]
        out: dict[str, Any] = {'value': rows}
        if skip + top < len(source):
            out['@odata.nextLink'] = f'{ent.name}?$skiptoken={skip + top}'
        return out

    @staticmethod
    def _apply_filter(rows: list[dict], flt: str | None) -> list[dict]:
        """Minimal OData $filter support for the incremental-sync test:
        'lastModifiedDateTime gt <iso>'. ISO-8601 sorts lexicographically, so
        a string compare is correct here."""
        if not flt:
            return rows
        parts = flt.split(None, 2)
        if len(parts) == 3 and parts[1] == 'gt':
            field, _, value = parts
            return [r for r in rows if str(r.get(field, '')) > value]
        return rows   # unsupported filter form -> no-op (twin is a test double)

    # -- $metadata ------------------------------------------------------------------

    def _metadata_xml(self) -> str:
        parts = ['<?xml version="1.0" encoding="utf-8"?>',
                 '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">',
                 '<edmx:DataServices><Schema '
                 'xmlns="http://docs.oasis-open.org/odata/ns/edm">']
        for ent in self._entities.values():
            if ent.hidden or ent.name not in self._granted:
                continue
            parts.append(f'<EntityType Name="{ent.name}">')
            for f in ent.fields:
                parts.append(
                    f'<Property Name="{f.name}" Type="{f.edm_type}" '
                    f'Nullable="{str(f.nullable).lower()}" '
                    f'FieldNumber="{f.field_number}"/>'
                )
            for nav in ent.nav_properties:
                parts.append(f'<NavigationProperty Name="{nav}" Type="{nav}"/>')
            parts.append('</EntityType>')
        parts.append('</Schema></edmx:DataServices></edmx:Edmx>')
        return ''.join(parts)

    # -- consistency simulation -------------------------------------------------------

    def _posting_pending(self) -> int:
        elapsed_min = (self._clock.now() - self._t0) / 60.0
        drained = int(elapsed_min * self._posting_drain)
        return max(0, self._posting_seeded - drained)
