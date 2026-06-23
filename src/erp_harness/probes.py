"""C5 — Behavior Probes. Measured values with named methods; never bypasses C2.

Design point worth a sentence: the throttle probe does NOT hammer the tenant
and read raw 429s — the enforcer owns retries and would mask them. Instead,
probes are JOURNAL ANALYSES: drive a measured burst through the enforcer,
then read what the enforcer observed. Probes measure by introspecting the
safety layer, structurally unable to circumvent it.

Write-path experiments (idempotency, draft-then-commit — erp-replica E1) are
DEFERRED to the sync phase and reported as named deferrals, never as gaps or
guesses.
"""
from __future__ import annotations

from typing import Any

from erp_harness.enforcer import BudgetExhausted, SafetyEnforcer

DEFERRED_EXPERIMENTS = (
    'idempotency_on_504 (write-path; erp-replica E1 — sync phase only)',
    'draft_then_commit_semantics (write-path — sync phase only)',
)


def probe_throttle(enforcer: SafetyEnforcer, *, burst: int = 80) -> dict[str, Any]:
    """Drive a burst of cheap reads; measure the 429 threshold from the
    journal AFTER the burst — regardless of whether the burst ended by
    detection, exhausted retries, or completion. (The original draft read
    the journal only on the clean path and reported None when the enforcer's
    retry loop hit a hard 429 wall — the planted-fault E2E caught it.)
    Accurate outcome when no 429 occurs: ceiling-not-reached."""
    start_idx = len(enforcer.journal.entries)
    try:
        for _ in range(burst):
            enforcer.get('items', {'$top': '1'})
            if any(e.event == 'throttled'
                   for e in enforcer.journal.entries[start_idx:]):
                break
    except BudgetExhausted:
        pass   # journal still carries the evidence; read it below
    window = enforcer.journal.entries[start_idx:]
    first_throttle = next((i for i, e in enumerate(window)
                           if e.event == 'throttled'), None)
    if first_throttle is None:
        return {'value': None, 'method': f'burst of {burst} reads, no 429 '
                'observed within politeness ceiling — threshold >= burst'}
    accepted = sum(1 for e in window[:first_throttle]
                   if e.event == 'sent' and e.status == 200)
    return {'value': accepted,
            'method': f'journal analysis: {accepted} requests accepted '
                      f'in-window before the first 429'}


def _denied(entity: str) -> dict[str, Any]:
    """A probe that loses its grant degrades LOUDLY: a named error finding,
    never a crash and never a silent absence (C1 minimality depends on it)."""
    return {'value': None,
            'method': f'read of {entity!r} denied',
            'error': f'permission denied: grant for {entity!r} missing or revoked'}


def probe_pagination(enforcer: SafetyEnforcer, entity: str = 'items'
                     ) -> dict[str, Any]:
    resp = enforcer.get(entity)
    if resp.status == 403:
        return _denied(entity)
    page = len(resp.json['value'])
    has_next = '@odata.nextLink' in resp.json
    return {'value': {'page_size': page, 'next_link': has_next},
            'method': 'first-page fetch; counted rows and nextLink presence'}


def probe_timestamps(enforcer: SafetyEnforcer, entity: str = 'items',
                     field: str = 'lastModifiedDateTime') -> dict[str, Any]:
    resp = enforcer.get(entity, {'$top': '5'})
    if resp.status == 403:
        return _denied(entity)
    samples = [r.get(field) for r in resp.json['value'] if r.get(field)]
    utc = all(str(s).endswith('Z') or '+' in str(s) for s in samples)
    return {'value': {'offset_explicit': utc, 'samples': samples[:2]},
            'method': f'inspected {len(samples)} {field} values for explicit '
                      f'UTC offset'}


def probe_posting_queue(enforcer: SafetyEnforcer, *,
                        interval_seconds: float = 120.0,
                        clock) -> dict[str, Any]:
    """Observable eventual-consistency: sample the posting queue twice and
    report the drain rate. Observation only — nothing is posted."""
    first_resp = enforcer.get('status')
    if first_resp.status == 403:
        return _denied('status')
    first = first_resp.json['postingQueue']
    clock.sleep(interval_seconds)
    second = enforcer.get('status').json['postingQueue']
    drain_per_min = (first - second) / (interval_seconds / 60.0)
    return {'value': {'pending_t0': first, 'pending_t1': second,
                      'drain_per_minute': round(drain_per_min, 1)},
            'method': f'two reads {interval_seconds}s apart; observation '
                      f'only, no writes'}


def run_all(enforcer: SafetyEnforcer, clock) -> dict[str, Any]:
    return {
        'throttle': probe_throttle(enforcer),
        'pagination': probe_pagination(enforcer),
        'timestamps': probe_timestamps(enforcer),
        'posting_queue': probe_posting_queue(enforcer, clock=clock),
        'deferred_experiments': list(DEFERRED_EXPERIMENTS),
    }
