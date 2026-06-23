"""C5 — probes report measured values with methods, and track twin
reconfiguration. Probes are journal analyses: the throttle measurement must
come from what the enforcer observed, never from bypassing it.
"""
from __future__ import annotations

from erp_harness.probes import (
    DEFERRED_EXPERIMENTS, probe_posting_queue, probe_throttle, run_all,
)
from harness_fixtures import make_rig


# --- smoke ----------------------------------------------------------------------

def test_all_probes_emit_value_and_method():
    clock, _, enforcer = make_rig()
    findings = run_all(enforcer, clock)
    for name in ('throttle', 'pagination', 'timestamps', 'posting_queue'):
        assert 'value' in findings[name] and 'method' in findings[name], name
        assert findings[name]['method'], f'{name}: empty method'


def test_deferred_writepath_experiments_are_named_not_guessed():
    clock, _, enforcer = make_rig()
    findings = run_all(enforcer, clock)
    assert findings['deferred_experiments'] == list(DEFERRED_EXPERIMENTS)
    assert any('idempotency' in d for d in findings['deferred_experiments'])


def test_posting_queue_drain_measured_from_two_observations():
    clock, _, enforcer = make_rig()
    pq = probe_posting_queue(enforcer, clock=clock)
    assert pq['value']['drain_per_minute'] == 10.0   # seeded drain rate
    assert 'no writes' in pq['method']


# --- E2E behavioral: throttle tracking -------------------------------------------------

def test_e2e_throttle_probe_tracks_twin_reconfiguration():
    # Twin ceiling 30/min: probe must observe a 429 near 30.
    _, _, enforcer30 = make_rig(throttle_per_minute=30, rate_per_minute=500,
                                total_call_budget=400)
    t30 = probe_throttle(enforcer30, burst=80)
    assert t30['value'] is not None and 25 <= t30['value'] <= 35

    # Reconfigured twin at 60/min: the measurement must move with it.
    _, _, enforcer60 = make_rig(throttle_per_minute=60, rate_per_minute=500,
                                total_call_budget=400)
    t60 = probe_throttle(enforcer60, burst=80)
    assert t60['value'] is not None and 55 <= t60['value'] <= 65

    # And the accurate no-measurement outcome when no ceiling exists:
    _, _, unthrottled = make_rig(throttle_per_minute=None)
    tn = probe_throttle(unthrottled, burst=20)
    assert tn['value'] is None and 'no 429' in tn['method']
