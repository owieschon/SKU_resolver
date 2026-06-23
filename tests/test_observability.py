"""R1 observability: off-by-default, fail-open, redaction (incl. account
numbers), per-session cost budget, deploy-guard preflight, alert dedup.
"""
from __future__ import annotations

import json

import pytest

from observability import (
    AlertRouter,
    BudgetExceeded,
    CostEvent,
    CostLedger,
    anomaly_flags,
    init_tracing,
    redact,
    reset_for_test,
    scrub_pii,
    set_attr,
    tracer,
)
from observability.telemetry import _NoopSpan

# ── tracing: off by default, fail-open ───────────────────────────────────────

def test_tracer_is_noop_without_init():
    reset_for_test()
    # the shared tracer forwards to a no-op until init; emitting a span is safe,
    # never raises, and yields a no-op span.
    with tracer.start_as_current_span('x') as sp:
        assert isinstance(sp, _NoopSpan)
        set_attr(sp, 'svc.task', 'probe')


def test_init_off_when_flag_unset(monkeypatch):
    reset_for_test()
    monkeypatch.delenv('SKU_OBS_TRACING', raising=False)
    assert init_tracing() is False     # no-op, returns False


def test_init_fail_open_when_libs_absent(monkeypatch):
    # Flag on but OTel not installed in CI -> must degrade to no-op, not raise.
    reset_for_test()
    monkeypatch.setenv('SKU_OBS_TRACING', '1')
    enabled = init_tracing()           # never raises
    from observability import tracer as t
    if not enabled:                    # CI path: OTel absent
        assert isinstance(t.start_as_current_span('x'), _NoopSpan)


def test_span_never_suppresses_caller_exception():
    reset_for_test()
    with pytest.raises(ValueError):
        with tracer.start_as_current_span('x'):
            raise ValueError('boom')


# ── redaction ────────────────────────────────────────────────────────────────

def test_structured_attrs_pass_through():
    assert redact('svc.outcome', 'resolved') == 'resolved'
    assert redact('llm.cost.total', 0.0123) == 0.0123


def test_content_attrs_scrub_pii_including_account_numbers():
    s = redact('transcript.text',
               'my account number is 123456789 call me at 555-010-0100')
    assert '123456789' not in s and '[ACCOUNT]' in s
    assert '555-010-0100' not in s and '[PHONE]' in s


def test_account_phrasing_scrubbed():
    assert '[ACCOUNT]' in scrub_pii('account #4837 please')
    assert '4837' not in scrub_pii('account no. 4837')   # short form via phrase


def test_content_killswitch(monkeypatch):
    monkeypatch.setenv('SKU_OBS_TRACE_CONTENT', '0')
    assert redact('transcript.text', 'anything') is None   # caller skips attr


def test_unknown_attr_is_failclosed_as_content():
    # An attr nobody registered must be scrubbed, never passed raw.
    out = redact('mystery.field', 'email me at a@b.com')
    assert '[EMAIL]' in out


# ── cost ledger + per-session budget ─────────────────────────────────────────

def test_per_session_budget_is_hard(tmp_path):
    ledger = CostLedger(tmp_path / 'cost.jsonl')
    for i in range(3):
        ledger.record(CostEvent(ts=f'2026-06-07T00:0{i}:00', task='turn',
                                model='m', cost_usd=2.0, session_id='S1'))
    assert ledger.spent_for_session('S1') == 6.0
    with pytest.raises(BudgetExceeded):
        ledger.enforce_session_budget('S1', limit=5.0)
    # a different session is unaffected (isolation)
    ledger.enforce_session_budget('S2', limit=5.0)


def test_cost_ledger_persists_and_reloads(tmp_path):
    p = tmp_path / 'c.jsonl'
    CostLedger(p).record(CostEvent(ts='t', task='x', model='m', cost_usd=1.0,
                                   session_id='S'))
    assert CostLedger(p).spent_for_session('S') == 1.0   # reloaded from disk


def test_anomaly_flags():
    ev = CostEvent(ts='t', task='x', model='m', cost_usd=3.0, out_tokens=50000)
    flags = anomaly_flags(ev, {'cost_usd': 1.0, 'out_tokens': 20000})
    assert set(flags) == {'cost_usd', 'out_tokens'}


# ── alert routing ────────────────────────────────────────────────────────────

def test_alert_dedup_and_file_audit(tmp_path):
    r = AlertRouter(tmp_path / 'alerts.jsonl')
    assert r.route(severity='critical', title='t', summary='s',
                   now_iso='t0', dedup_key='k') is True
    assert r.route(severity='critical', title='t', summary='s',
                   now_iso='t1', dedup_key='k') is False   # deduped
    rows = [json.loads(ln) for ln in
            (tmp_path / 'alerts.jsonl').read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['severity'] == 'critical'


# ── deploy guard preflight ───────────────────────────────────────────────────

def test_verification_preflight_blocks_on_dirty_tree(tmp_path):
    import subprocess

    from observability import record_startup_commit, verification_preflight
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@t'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.name', 't'], cwd=repo, check=True)
    (repo / 'src').mkdir()
    (repo / 'src' / 'a.py').write_text('x = 1\n')
    subprocess.run(['git', 'add', '-A'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'init'], cwd=repo, check=True)
    state = repo / 'state.json'
    record_startup_commit(repo, pid=1, now_iso='t0', state_path=state)
    # clean tree -> not blocked
    assert verification_preflight(repo, state).should_block is False
    # dirty a tracked source file -> blocked
    (repo / 'src' / 'a.py').write_text('x = 2\n')
    pre = verification_preflight(repo, state)
    assert pre.should_block and 'uncommitted' in pre.message
