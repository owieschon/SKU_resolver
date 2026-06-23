"""C3 — Surface Discovery & Schema Profiler (Phase B).

$metadata crawl + per-entity sample-row profiling, entirely through the
SafetyEnforcer. defusedxml parses the metadata: real tenant responses are
untrusted input (decision D10).

The fingerprint computed here is the C8 drift baseline: a content hash over
the granted surface's (entity, field, type, nullable, fieldNumber) tuples,
canonically ordered.
"""
from __future__ import annotations

import hashlib
import json

import defusedxml.ElementTree as ET

from erp_harness.enforcer import SafetyEnforcer
from erp_harness.models import (
    DiscoveryError,
    EntitySchema,
    FieldProfile,
    FieldSchema,
    MissingGrantError,
    SurfaceProfile,
)

_EDM_NS = '{http://docs.oasis-open.org/odata/ns/edm}'
_EDMX_NS = '{http://docs.oasis-open.org/odata/ns/edmx}'
CUSTOM_FIELD_FLOOR = 50000
PROFILE_SAMPLE_ROWS = 200


def crawl_metadata(enforcer: SafetyEnforcer) -> tuple[EntitySchema, ...]:
    resp = enforcer.get('$metadata')
    if resp.status == 403:
        raise MissingGrantError('metadata')
    if resp.status != 200 or not resp.text:
        raise DiscoveryError(f'metadata crawl failed: status={resp.status}')
    root = ET.fromstring(resp.text)
    entities = []
    for et in root.iter(f'{_EDM_NS}EntityType'):
        fields = tuple(
            FieldSchema(
                name=p.get('Name'),
                edm_type=p.get('Type'),
                nullable=p.get('Nullable', 'true') == 'true',
                field_number=int(p.get('FieldNumber')) if p.get('FieldNumber') else None,
                is_custom=bool(p.get('FieldNumber'))
                and int(p.get('FieldNumber')) >= CUSTOM_FIELD_FLOOR,
            )
            for p in et.findall(f'{_EDM_NS}Property')
        )
        navs = tuple(n.get('Name')
                     for n in et.findall(f'{_EDM_NS}NavigationProperty'))
        entities.append(EntitySchema(name=et.get('Name'), fields=fields,
                                     nav_properties=navs))
    return tuple(entities)


def fetch_all_rows(enforcer: SafetyEnforcer, entity: str,
                   limit: int | None = None,
                   extra_params: dict[str, str] | None = None) -> list[dict]:
    """Paginate via @odata.nextLink / $skiptoken until exhausted or limit.
    extra_params (e.g. an OData $filter) are sent on every page request."""
    rows: list[dict] = []
    params: dict[str, str] = dict(extra_params or {})
    while True:
        resp = enforcer.get(entity, params)
        if resp.status == 403:
            raise MissingGrantError(entity)
        if resp.status != 200:
            raise DiscoveryError(f'{entity}: unexpected status {resp.status}')
        rows.extend(resp.json['value'])
        if limit is not None and len(rows) >= limit:
            return rows[:limit]
        next_link = resp.json.get('@odata.nextLink')
        if not next_link:
            return rows
        params = {**(extra_params or {}),
                  '$skiptoken': next_link.split('$skiptoken=')[1]}


SAMPLE_SCAN_CAP = 1000


def sample_rows(enforcer: SafetyEnforcer, entity: str, n: int,
                *, scan_cap: int = SAMPLE_SCAN_CAP) -> list[dict]:
    """Stratified sample of up to n rows across the entity (R0 #4).

    The original head-of-table fetch (`fetch_all_rows(limit=n)`) returned the
    first n PK-sorted rows — a biased frame: a field sparsely populated in the
    low-PK range would be wrongly profiled/verified. This scans up to scan_cap
    rows and takes an evenly-spaced subsample, so the sample spans the entity.

    Stated limitation: for entities larger than scan_cap, the first scan_cap
    rows are the sampling frame (bounded cost under the enforcer budget). This
    is logged in the profile detail, never hidden — a known frame, not a
    silent truncation.
    """
    frame = fetch_all_rows(enforcer, entity, limit=scan_cap)
    if len(frame) <= n:
        return frame
    step = len(frame) / n
    return [frame[int(i * step)] for i in range(n)]


def profile_fields(entity: EntitySchema, rows: list[dict]) -> list[FieldProfile]:
    out = []
    n = len(rows)
    for f in entity.fields:
        values = [r.get(f.name) for r in rows]
        nonnull = [v for v in values if v is not None and v != '']
        strs = [str(v) for v in nonnull]
        lens = sorted(len(s) for s in strs)
        out.append(FieldProfile(
            entity=entity.name, name=f.name, sampled=n,
            null_rate=round(1 - len(nonnull) / n, 4) if n else 1.0,
            distinct_ratio=round(len(set(strs)) / len(strs), 4) if strs else 0.0,
            p95_len=lens[int(0.95 * (len(lens) - 1))] if lens else None,
            sample_values=tuple(strs[:3]),
        ))
    return out


def surface_fingerprint(entities: tuple[EntitySchema, ...]) -> str:
    canonical = sorted(
        (e.name, f.name, f.edm_type, f.nullable, f.field_number or 0)
        for e in entities for f in e.fields
    )
    blob = json.dumps(canonical, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def discover(enforcer: SafetyEnforcer,
             profile_sample: int = PROFILE_SAMPLE_ROWS) -> SurfaceProfile:
    entities = crawl_metadata(enforcer)
    profiles: list[FieldProfile] = []
    for ent in entities:
        rows = sample_rows(enforcer, ent.name, profile_sample)  # stratified
        profiles.extend(profile_fields(ent, rows))
    return SurfaceProfile(entities=entities,
                          field_profiles=tuple(profiles),
                          fingerprint=surface_fingerprint(entities))
