"""C8 — Drift Guard.

Re-fingerprint the granted surface; diff against the profile's baseline.
Unacknowledged drift HALTS sync with the exact change named; acknowledgment
is an explicit artifact that bumps the profile version through the review
gate. No auto-resume path exists — the absence is the feature.
"""
from __future__ import annotations

from dataclasses import replace

from erp_harness.discovery import crawl_metadata, surface_fingerprint
from erp_harness.enforcer import SafetyEnforcer
from erp_harness.models import DriftReport, EntitySchema, TenantERPProfile


class SyncHalted(Exception):
    def __init__(self, report: DriftReport):
        self.report = report
        super().__init__(
            'sync halted: schema drift vs approved baseline — '
            + '; '.join(report.changes))


def _schema_map(entities: tuple[EntitySchema, ...]) -> dict[tuple[str, str], tuple]:
    return {(e.name, f.name): (f.edm_type, f.nullable, f.field_number)
            for e in entities for f in e.fields}


def diff_surfaces(baseline: tuple[EntitySchema, ...],
                  current: tuple[EntitySchema, ...]) -> list[str]:
    b, c = _schema_map(baseline), _schema_map(current)
    changes = []
    for key in sorted(b.keys() - c.keys()):
        changes.append(f'removed field {key[0]}.{key[1]}')
    for key in sorted(c.keys() - b.keys()):
        changes.append(f'added field {key[0]}.{key[1]}'
                       + (' (tenant custom range)' if (c[key][2] or 0) >= 50000
                          else ''))
    for key in sorted(b.keys() & c.keys()):
        if b[key] != c[key]:
            changes.append(f'changed {key[0]}.{key[1]}: '
                           f'{b[key][0]} -> {c[key][0]}')
    b_entities = {e.name for e in baseline}
    c_entities = {e.name for e in current}
    for name in sorted(b_entities - c_entities):
        changes.append(f'entity {name!r} no longer on surface')
    return changes


class DriftGuard:
    def __init__(self, baseline_entities: tuple[EntitySchema, ...],
                 baseline_fingerprint: str) -> None:
        self._baseline = baseline_entities
        self._fingerprint = baseline_fingerprint

    def check(self, enforcer: SafetyEnforcer) -> DriftReport:
        current = crawl_metadata(enforcer)
        fp = surface_fingerprint(current)
        if fp == self._fingerprint:
            return DriftReport(False, self._fingerprint, fp, ())
        return DriftReport(True, self._fingerprint, fp,
                           tuple(diff_surfaces(self._baseline, current)))

    def check_or_halt(self, enforcer: SafetyEnforcer) -> None:
        report = self.check(enforcer)
        if report.drifted:
            raise SyncHalted(report)


def acknowledge_drift(profile: TenantERPProfile, report: DriftReport, *,
                      reviewer: str, reason: str) -> TenantERPProfile:
    """Explicit human acknowledgment: profile version bumps, the new
    fingerprint becomes the baseline, and approval RESETS — the bumped
    profile must pass the review gate again before sync resumes."""
    if not report.drifted:
        raise ValueError('nothing to acknowledge: no drift in report')
    if not reviewer or not reason:
        raise ValueError('acknowledgment requires a named reviewer and reason')
    return replace(profile,
                   profile_version=profile.profile_version + 1,
                   fingerprint=report.current_fingerprint,
                   approval=None)
