"""Dependency fault-injection harness — the durability bar for someone else's
callers. Every dependency faulted at every seam of the turn must fail CLOSED and
COHERENT: never a hang, never a crash/500, never an incoherent or empty utterance,
never a fabricated fact. Map: docs/FAULT_INJECTION_PLAN.md. Same adversarial
discipline as containment, applied to exogenous faults with deterministic correct
behavior — and fault-injection check where a real fix is involved (show the
unwrapped path fails BEFORE asserting the wrapped path holds).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway_fixtures import build_gateway

from gateway import Channel
from gateway.say_guard import internal_state_tokens
from runtime.agent_brain import SERVICE_FALLBACK
from runtime.custom_llm import handle_async
from runtime.custom_llm_route import register_custom_llm

# ============ custom-LLM endpoint (handle_async flow) =======================

def _client(model_fn, *, budget_secs=8.0):
    app = FastAPI()
    register_custom_llm(app, model_fn=model_fn, budget_secs=budget_secs)
    return TestClient(app)


def _content(r):
    return r.json()['choices'][0]['message']['content']


def _never(_m):
    raise AssertionError('model must not be invoked on this turn')


# -- S1 transport-in ---------------------------------------------------------

def test_s1_non_json_body_is_400_not_a_crash():
    c = _client(lambda m: 'hi')
    r = c.post('/v1/chat/completions', content=b'{not json',
               headers={'Content-Type': 'application/json'})
    assert r.status_code == 400


def test_s1_missing_messages_fails_to_service_fallback():
    c = _client(lambda m: 'hi')
    r = c.post('/v1/chat/completions', json={'model': 'x'})
    assert _content(r) == SERVICE_FALLBACK


def test_s1_reordered_and_oversized_history_never_crashes():
    # tool message FIRST (no preceding assistant), then a long run of turns
    tool = {'role': 'tool', 'tool_call_id': 't', 'content': json.dumps(
        {'say': 'in stock', 'surfaced_skus': ['K5-24SBC'], 'surfaced_values': {}})}
    msgs = [tool] + [{'role': 'user', 'content': f'turn {i}'} for i in range(200)]
    c = _client(lambda m: 'how can I help?')
    r = c.post('/v1/chat/completions', json={'messages': msgs})
    assert r.status_code == 200 and _content(r)            # coherent, no crash


# -- S2 tool-message (the gateway result as ElevenLabs relays it) ------------

def test_s2_non_json_tool_content_fails_to_service_fallback():
    c = _client(_never)
    r = c.post('/v1/chat/completions', json={'messages': [
        {'role': 'user', 'content': 'stock?'},
        {'role': 'tool', 'tool_call_id': 't', 'content': 'GATEWAY 500 Bad Gateway'}]})
    assert _content(r) == SERVICE_FALLBACK


def test_s2_missing_provenance_keys_fails_to_service_fallback():
    c = _client(_never)
    r = c.post('/v1/chat/completions', json={'messages': [
        {'role': 'user', 'content': 'stock?'},
        {'role': 'tool', 'tool_call_id': 't',
         'content': json.dumps({'say': 'in stock'})}]})    # no surfaced_* keys
    assert _content(r) == SERVICE_FALLBACK


# -- S3 degraded-but-parseable tool result: empty say ------------------------

def test_s3_empty_say_never_speaks_silence():
    c = _client(_never)
    r = c.post('/v1/chat/completions', json={'messages': [
        {'role': 'user', 'content': 'stock?'},
        {'role': 'tool', 'tool_call_id': 't', 'content': json.dumps(
            {'say': '   ', 'surfaced_skus': [], 'surfaced_values': {}})}]})
    assert _content(r) == SERVICE_FALLBACK                 # not '' (dead air)


# -- S4 model faults ---------------------------------------------------------

def test_s4_model_error_fails_to_service_fallback():
    def boom(_m):
        raise RuntimeError('upstream 500')
    assert _content(_client(boom).post(
        '/v1/chat/completions', json={'messages': [{'role': 'user', 'content': 'hi'}]})
    ) == SERVICE_FALLBACK


def test_s4_rate_limit_fails_to_service_fallback():
    class RateLimitError(Exception):
        pass

    def limited(_m):
        raise RateLimitError('429 Too Many Requests')
    assert _content(_client(limited).post(
        '/v1/chat/completions', json={'messages': [{'role': 'user', 'content': 'hi'}]})
    ) == SERVICE_FALLBACK


def test_s4_malformed_tool_call_bad_json_args_fails_closed():
    bad = {'tool_call': [{'id': 't', 'type': 'function', 'function': {
        'name': 'resolve_part', 'arguments': '{not valid json'}}]}
    assert _content(_client(lambda m: bad).post(
        '/v1/chat/completions', json={'messages': [{'role': 'user', 'content': 'stock?'}]})
    ) == SERVICE_FALLBACK


def test_s4_tool_call_without_text_fails_closed():
    notext = {'tool_call': [{'id': 't', 'type': 'function', 'function': {
        'name': 'resolve_part', 'arguments': json.dumps({'caller_id': 'x'})}}]}
    assert _content(_client(lambda m: notext).post(
        '/v1/chat/completions', json={'messages': [{'role': 'user', 'content': 'stock?'}]})
    ) == SERVICE_FALLBACK


# -- S4 the slow tail, Top-level: realistic-shaped budget, real abort -------

def test_s4_slow_tail_over_budget_is_really_aborted_at_a_seconds_scale_budget():
    # the case the deadline EXISTS for and the small latency sample keeps brushing:
    # a model slower than B (here seconds-scale, not the 0.03s mechanism test).
    state = {'cancelled': False, 'completed': False}

    async def slow_tail(_m):
        try:
            await asyncio.sleep(2.0)                       # past B
        except asyncio.CancelledError:
            state['cancelled'] = True
            raise
        state['completed'] = True
        return 'too late'
    t0 = time.monotonic()
    resp, trace = asyncio.run(handle_async(
        {'messages': [{'role': 'user', 'content': 'is K5-24SBC in stock?'}]},
        model_fn=slow_tail, budget_secs=1.0))
    dt = time.monotonic() - t0
    assert resp['choices'][0]['message']['content'] == SERVICE_FALLBACK
    assert trace['route'] == 'over_budget' and trace['over_budget'] is True
    assert state['cancelled'] is True and state['completed'] is False   # real abort
    assert dt < 1.6                                        # returned at ~B, not 2.0


def test_s4_slow_but_under_budget_returns_normally():
    async def under(_m):
        await asyncio.sleep(0.2)
        return 'how can I help with parts today?'
    resp, trace = asyncio.run(handle_async(
        {'messages': [{'role': 'user', 'content': 'hi'}]},
        model_fn=under, budget_secs=1.0))
    assert resp['choices'][0]['message']['content'].startswith('how can I help')
    assert trace.get('over_budget') is not True


# -- S6 transport-out: empty content streams coherently ----------------------

def test_s6_empty_content_streams_with_done_and_no_crash():
    c = _client(lambda m: '')
    r = c.post('/v1/chat/completions', json={'stream': True, 'messages': [
        {'role': 'user', 'content': 'hi'}]})
    assert r.status_code == 200 and 'data: [DONE]' in r.text


# -- correlated load: model slow AND a substitution turn (the one realistic pair)

def test_correlated_load_substitution_route_stays_bounded_without_the_model():
    # Production faults correlate: the model is rate-limited/slow at the SAME time
    # general load slows the gateway. The substitution route is EXEMPT from B — so
    # confirm that exemption is safe under correlated slowness. It is, for a
    # STRONGER reason than "the gateway is fast": the substitution route does ZERO
    # I/O. The gateway result is already in the request body (its latency was spent
    # on the prior /agent/turn hop, bounded by ElevenLabs' tool timeout), so a
    # hanging model cannot make a substitution turn hang.
    state = {'model_called': False}

    async def hanging_model(_m):
        state['model_called'] = True
        await asyncio.sleep(30)                            # would hang far past any B
        return 'never'

    tool = {'role': 'tool', 'tool_call_id': 't', 'content': json.dumps(
        {'say': 'Yep, in stock — 58 on hand.', 'surfaced_skus': ['K5-24SBC'],
         'surfaced_values': {'qty': 58}})}
    t0 = time.monotonic()
    resp, trace = asyncio.run(handle_async(
        {'messages': [{'role': 'user', 'content': 'stock?'}, tool]},
        model_fn=hanging_model, budget_secs=8.0))
    dt = time.monotonic() - t0
    assert trace['route'] == 'substitute_say' and trace['model_invoked'] is False
    assert state['model_called'] is False                 # the model was never reached
    assert dt < 0.5                                        # bounded despite a 30s-hang-capable model
    assert resp['choices'][0]['message']['content'] == 'Yep, in stock — 58 on hand.'


# ============ gateway endpoint (/agent/turn internal dependencies) ==========

def _gw():
    gw, sessions, journal, _ = build_gateway('/tmp/fault-gw')
    return gw, sessions, journal, sessions.open('S', 'c')


def _coherent_escalation(resp):
    assert resp.kind == 'escalate' and resp.refused == 'internal_error'
    assert resp.text and internal_state_tokens(resp.text) == []   # guard-clean


def test_gateway_resolution_fault_demonstrate_the_catch(monkeypatch):
    # Prove the check catches the fault: the unwrapped dispatch RAISES on a real backend fault;
    # turn() converts it to a coherent escalation instead of a 500 into ElevenLabs.
    gw, sessions, _, tok = _gw()

    def boom(*a, **k):
        raise RuntimeError('resolution backend down')
    monkeypatch.setattr('gateway.orchestrator.identify', boom)
    state = sessions.state_of('S', tok)
    with pytest.raises(RuntimeError):                      # the fault is real
        gw._dispatch('S', tok, 'is K5-24SBC in stock?', Channel.TYPED, state)
    resp = gw.turn('S', tok, 'is K5-24SBC in stock?', channel=Channel.TYPED)
    _coherent_escalation(resp)                             # and it is caught


def test_gateway_inventory_fault_fails_closed(monkeypatch):
    gw, sessions, _, tok = _gw()

    def boom(*a, **k):
        raise RuntimeError('inventory service unavailable')
    monkeypatch.setattr('gateway.orchestrator.availability', boom)
    _coherent_escalation(
        gw.turn('S', tok, 'is K5-24SBC in stock?', channel=Channel.TYPED))


def test_gateway_pricing_fault_fails_closed(monkeypatch):
    gw, sessions, _, tok = _gw()

    def boom(*a, **k):
        raise RuntimeError('pricebook down')
    monkeypatch.setattr('gateway.orchestrator.pricing', boom)
    _coherent_escalation(
        gw.turn('S', tok, 'how much is K5-24SBC?', channel=Channel.TYPED))


def test_gateway_customer_db_fault_fails_closed(monkeypatch):
    gw, sessions, _, tok = _gw()

    def boom(*a, **k):
        raise RuntimeError('customer DB timeout')
    monkeypatch.setattr(gw.sessions, 'verify', boom)
    _coherent_escalation(
        gw.turn('S', tok, 'my account number is 1001', channel=Channel.TYPED))


def test_gateway_journal_failure_does_not_fail_the_turn(monkeypatch):
    # G6: the audit journal is a dependency; a write failure must not drop a turn.
    gw, sessions, journal, tok = _gw()

    def boom():
        raise RuntimeError('disk full')
    monkeypatch.setattr(journal, 'now_fn', boom)           # every record() now throws
    resp = gw.turn('S', tok, 'is K5-24SBC in stock?', channel=Channel.TYPED)
    # the turn still produced a real answer (not an internal-fault escalation)
    assert resp.refused != 'internal_error' and resp.text
