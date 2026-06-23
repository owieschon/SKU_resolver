"""R2 architecture gaps: incremental sync + atomic refresh (#7),
transport timeouts (#8), token-expiry vs never-granted (#9)."""
from __future__ import annotations

import pytest
from harness_fixtures import BC, make_rig

from erp_harness import (
    AtomicCatalogRef,
    AuthExpiredError,
    BudgetExhausted,
    HeuristicExplorer,
    ReviewGate,
    SafetyEnforcer,
    TransportTimeout,
    run_onboarding,
    sync_items,
    sync_items_incremental,
)
from erp_harness.transport import ManualClock, TransportRequest, TransportResponse


def _approved(item_limit=120):
    clock, twin, enforcer = make_rig(item_limit=item_limit, total_call_budget=900)
    result = run_onboarding(BC, enforcer, HeuristicExplorer(), clock)
    profile = ReviewGate.approve(result.profile, reviewer='sme', reason='ok')
    return clock, twin, enforcer, profile


# ── #7 atomic refresh ────────────────────────────────────────────────────────

def test_atomic_ref_inflight_reader_sees_consistent_snapshot():
    _, twin, enforcer, profile = _approved()
    idx_v1 = sync_items(profile, enforcer, tenant_id='t')
    ref = AtomicCatalogRef(idx_v1)

    held = ref.current()                 # an in-flight turn grabs the snapshot
    sku = held.all_skus()[0]
    assert held.is_canonical(sku)

    idx_v2 = sync_items(profile, enforcer, tenant_id='t')   # a refresh builds new
    ref.swap(idx_v2)

    # The held snapshot is unchanged and still fully consistent...
    assert held is idx_v1
    assert held.is_canonical(sku)
    # ...and the ref now serves the new index.
    assert ref.current() is idx_v2


# ── #7 incremental sync ──────────────────────────────────────────────────────

def test_incremental_sync_merges_only_changed_rows():
    _, twin, enforcer, profile = _approved()
    full = sync_items(profile, enforcer, tenant_id='t')
    target = full.all_skus()[0]
    before_qty = full.lookup(target).quantity_on_hand

    # Mutate one row on the twin with a newer lastModifiedDateTime.
    for r in twin._entities['items'].rows:
        if r['number'] == target:
            r['inventoryQty'] = before_qty + 999
            r['lastModifiedDateTime'] = '2026-06-05T00:00:00Z'
            break

    delta = sync_items_incremental(profile, enforcer, tenant_id='t',
                                   since='2026-06-02T00:00:00Z', prior=full)
    assert delta.size() == full.size()                       # no rows lost
    assert delta.lookup(target).quantity_on_hand == before_qty + 999  # updated
    # An unchanged row is carried over from the prior snapshot.
    other = full.all_skus()[1]
    assert delta.lookup(other).quantity_on_hand == \
        full.lookup(other).quantity_on_hand


def test_incremental_sync_only_fetches_changed_rows():
    _, twin, enforcer, profile = _approved()
    full = sync_items(profile, enforcer, tenant_id='t')
    for r in twin._entities['items'].rows[:1]:
        r['lastModifiedDateTime'] = '2026-06-05T00:00:00Z'
    calls_before = 900 - enforcer.calls_remaining
    sync_items_incremental(profile, enforcer, tenant_id='t',
                           since='2026-06-04T00:00:00Z', prior=full)
    # The $filter means only the 1 changed row comes back -> a single page,
    # far fewer calls than a full re-pull of ~120 rows.
    calls_after = 900 - enforcer.calls_remaining
    assert calls_after - calls_before <= 2


# ── #8 transport timeout ─────────────────────────────────────────────────────

class _TimeoutBackend:
    """Times out `fail_times`, then succeeds."""
    def __init__(self, fail_times: int):
        self.fail_times, self.calls = fail_times, 0
    def handle(self, req: TransportRequest) -> TransportResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TransportTimeout('stalled')
        return TransportResponse(status=200, json={'value': []})


def test_transient_timeout_is_retried_then_succeeds():
    clock = ManualClock()
    enf = SafetyEnforcer(_TimeoutBackend(fail_times=2), clock,
                         rate_per_minute=1000, total_call_budget=100)
    resp = enf.get('items')
    assert resp.status == 200
    assert len(enf.journal.events('timeout')) == 2     # journaled, then recovered


def test_persistent_timeout_halts_cleanly_not_hangs():
    clock = ManualClock()
    enf = SafetyEnforcer(_TimeoutBackend(fail_times=99), clock,
                         rate_per_minute=1000, total_call_budget=100)
    with pytest.raises(BudgetExhausted, match='retry'):
        enf.get('items')


# ── #9 token expiry vs never-granted ─────────────────────────────────────────

class _AuthBackend:
    def __init__(self, status_sequence):
        self.seq, self.calls = list(status_sequence), 0
    def handle(self, req: TransportRequest) -> TransportResponse:
        s = self.seq[min(self.calls, len(self.seq) - 1)]
        self.calls += 1
        return TransportResponse(status=s,
                                 json={'value': []} if s == 200 else {'error': s})


def test_401_without_refresh_raises_auth_expired_not_missing_grant():
    enf = SafetyEnforcer(_AuthBackend([401]), ManualClock(),
                         rate_per_minute=100, total_call_budget=100)
    with pytest.raises(AuthExpiredError):
        enf.get('items')


def test_401_with_successful_refresh_retries_once():
    state = {'refreshed': False}
    def refresh():
        state['refreshed'] = True
        return True
    enf = SafetyEnforcer(_AuthBackend([401, 200]), ManualClock(),
                         rate_per_minute=100, total_call_budget=100,
                         auth_refresh=refresh)
    resp = enf.get('items')
    assert resp.status == 200 and state['refreshed']
    assert enf.journal.events('auth_refresh')


def test_401_with_failing_refresh_gives_up():
    enf = SafetyEnforcer(_AuthBackend([401, 401]), ManualClock(),
                         rate_per_minute=100, total_call_budget=100,
                         auth_refresh=lambda: False)
    with pytest.raises(AuthExpiredError, match='failed'):
        enf.get('items')


def test_403_is_still_a_grant_gap_not_auth_expiry():
    from erp_harness import MissingGrantError
    from erp_harness.discovery import fetch_all_rows
    enf = SafetyEnforcer(_AuthBackend([403]), ManualClock(),
                         rate_per_minute=100, total_call_budget=100)
    with pytest.raises(MissingGrantError):
        fetch_all_rows(enf, 'items')      # 403 -> grant gap, distinct from 401
