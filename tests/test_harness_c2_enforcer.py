"""C2 — enforcer smoke + the adversarial-write E2E.

The E2E proves zero writes AT THE DESTINATION: an adversarial explorer that
tries to 'verify write access' is refused by the enforcer, and the twin's
own audit log — not the harness's journal — shows no non-GET request ever
arrived.
"""
from __future__ import annotations

import pytest
from harness_fixtures import make_rig

from erp_harness import BudgetExhausted, SafetyEnforcer, WriteRefused
from erp_harness.transport import ManualClock, TransportRequest, TransportResponse


class _CountingBackend:
    def __init__(self, status: int = 200, headers=None):
        self.calls = 0
        self._status, self._headers = status, headers or {}

    def handle(self, req: TransportRequest) -> TransportResponse:
        self.calls += 1
        return TransportResponse(status=self._status, headers=self._headers,
                                 json={'value': []})


# --- smoke ----------------------------------------------------------------------

def test_write_methods_blocked_before_the_wire():
    backend = _CountingBackend()
    enf = SafetyEnforcer(backend, ManualClock(), rate_per_minute=10,
                         total_call_budget=10)
    with pytest.raises(WriteRefused):
        enf._request('PATCH', 'items', {})
    assert backend.calls == 0                       # never reached the backend
    assert enf.journal.events('refused_write')      # but WAS journaled


def test_total_call_budget_halts_cleanly():
    backend = _CountingBackend()
    enf = SafetyEnforcer(backend, ManualClock(), rate_per_minute=1000,
                         total_call_budget=5)
    for _ in range(5):
        enf.get('items')
    with pytest.raises(BudgetExhausted):
        enf.get('items')
    assert backend.calls == 5


def test_rate_ceiling_waits_out_the_window():
    clock = ManualClock()
    backend = _CountingBackend()
    enf = SafetyEnforcer(backend, clock, rate_per_minute=10,
                         total_call_budget=100)
    for _ in range(15):
        enf.get('items')
    assert clock.now() >= 60.0      # the 11th call had to wait out the window


def test_retryable_statuses_backoff_and_honor_retry_after():
    clock = ManualClock()
    backend = _CountingBackend(status=429, headers={'Retry-After': '7'})
    enf = SafetyEnforcer(backend, clock, rate_per_minute=1000,
                         total_call_budget=100)
    with pytest.raises(BudgetExhausted, match='retry'):
        enf.get('items')
    assert clock.now() >= 7 * 5     # slept Retry-After on every attempt
    assert len(enf.journal.events('throttled')) == 6   # initial + 5 retries


def test_retry_after_http_date_form_does_not_crash():
    """R0 #5: Retry-After may be an HTTP-date (RFC 9110), not just seconds.
    The original code threw ValueError on the date form. It must back off
    cleanly using the fallback instead."""
    from erp_harness.enforcer import _parse_retry_after
    # seconds form parsed exactly
    assert _parse_retry_after('12', now=0.0, fallback=2) == 12.0
    # HTTP-date form does not raise; honored as fallback
    d = _parse_retry_after('Wed, 21 Oct 2026 07:28:00 GMT', now=0.0, fallback=4)
    assert d == 4.0
    # garbage and missing both fall back, never raise, never negative
    assert _parse_retry_after('not-a-thing', now=0.0, fallback=3) == 3.0
    assert _parse_retry_after(None, now=0.0, fallback=1) == 1.0
    assert _parse_retry_after('-5', now=0.0, fallback=2) == 0.0

    # end to end: a 429 carrying an HTTP-date Retry-After backs off, no crash
    clock = ManualClock()
    backend = _CountingBackend(status=429,
                               headers={'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'})
    enf = SafetyEnforcer(backend, clock, rate_per_minute=1000, total_call_budget=100)
    with pytest.raises(BudgetExhausted, match='retry'):
        enf.get('items')
    assert clock.now() > 0       # it did back off


def test_every_attempt_is_journaled():
    backend = _CountingBackend()
    enf = SafetyEnforcer(backend, ManualClock(), rate_per_minute=10,
                         total_call_budget=10)
    enf.get('items')
    enf.get('customers')
    sent = enf.journal.events('sent')
    assert [(e.method, e.path) for e in sent] == [('GET', 'items'),
                                                  ('GET', 'customers')]


# --- E2E behavioral: the adversarial write, proven at the destination -------------

class AdversarialWriteExplorer:
    """Tries every escalation path an over-eager agent might reach for."""

    def attack(self, enforcer: SafetyEnforcer) -> list[str]:
        refused = []
        for method, path in (('POST', 'items'), ('PATCH', 'items'),
                             ('DELETE', 'salesOrders'), ('PUT', 'customers')):
            try:
                enforcer._request(method, path, {})
            except WriteRefused:
                refused.append(method)
        return refused


def test_e2e_adversarial_write_never_reaches_the_twin():
    clock, twin, enforcer = make_rig()
    refused = AdversarialWriteExplorer().attack(enforcer)
    assert refused == ['POST', 'PATCH', 'DELETE', 'PUT']    # all refused...
    assert len(enforcer.journal.events('refused_write')) == 4  # ...all journaled
    # The destination's evidence, not the harness's:
    assert twin.write_attempts() == [], \
        'a write reached the twin — the enforcer guarantee is broken'
