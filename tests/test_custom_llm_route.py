"""The mounted custom-LLM seam: POST /v1/chat/completions. Transport + auth +
instrumentation over the (separately proven) containment brain. The route is
gateway-independent — substitution reads the tool message already in the request
body — so these drive it on a bare app with an injected model, no gateway needed.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runtime.agent_brain import FALLBACK, GROUNDING_FALLBACK, SERVICE_FALLBACK
from runtime.custom_llm_route import register_custom_llm


def _client(model_fn, *, budget_secs=8.0):
    app = FastAPI()
    register_custom_llm(app, model_fn=model_fn, budget_secs=budget_secs)
    return TestClient(app)


def _tool_msg(say='in stock', skus=('K5-24SBC',), values=None):
    return {'role': 'tool', 'tool_call_id': 't1', 'content': json.dumps(
        {'say': say, 'surfaced_skus': list(skus),
         'surfaced_values': values or {'qty': 58}})}


def _explode(_m):
    raise AssertionError('model must not be invoked on a substitution turn')


# -- happy path: free turn runs the model, shape is OpenAI --------------------

def test_free_turn_returns_openai_completion_shape():
    c = _client(lambda m: 'Sure, how can I help with parts today?')
    r = c.post('/v1/chat/completions',
               json={'model': 'x', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert r.status_code == 200
    body = r.json()
    assert body['object'] == 'chat.completion'
    assert body['id'].startswith('chatcmpl-clm-') and 'created' in body
    assert body['choices'][0]['message']['content'].startswith('Sure')


# -- substitution turn: the model is NOT invoked, the gateway say is spoken ---

def test_tool_result_turn_substitutes_say_without_the_model():
    c = _client(_explode)                          # would fail if model ran
    r = c.post('/v1/chat/completions', json={'messages': [
        {'role': 'user', 'content': 'stock?'}, _tool_msg('Yep, in stock — 58 on hand.')]})
    assert r.status_code == 200
    assert r.json()['choices'][0]['message']['content'] == 'Yep, in stock — 58 on hand.'


# -- containment still bites through the route -------------------------------

def test_free_turn_fabrication_is_blocked_through_the_route():
    c = _client(lambda m: 'Part number HO2503170 is in stock for that.')
    r = c.post('/v1/chat/completions',
               json={'messages': [{'role': 'user', 'content': 'what fits a civic?'}]})
    assert r.json()['choices'][0]['message']['content'] == FALLBACK


def test_inbound_invented_lookup_key_is_grounded_through_the_route():
    c = _client(lambda m: {'tool_call': [{'id': 't', 'type': 'function', 'function': {
        'name': 'resolve_part', 'arguments': json.dumps({'text': 'K5-24SBC'})}}]})
    r = c.post('/v1/chat/completions',
               json={'messages': [{'role': 'user', 'content': 'I need a chrome stack'}]})
    assert r.json()['choices'][0]['message']['content'] == GROUNDING_FALLBACK


# -- streaming: buffer-don't-stream wearing SSE clothes -----------------------

def test_stream_emits_sse_with_done_and_the_full_filtered_content():
    c = _client(lambda m: 'How can I help with parts today?')
    r = c.post('/v1/chat/completions', json={'stream': True, 'messages': [
        {'role': 'user', 'content': 'hi'}]})
    assert r.status_code == 200
    assert 'text/event-stream' in r.headers['content-type']
    text = r.text
    assert 'data: [DONE]' in text
    # reassemble the streamed content deltas
    content = ''
    for line in text.splitlines():
        if line.startswith('data: ') and line[6:].strip() != '[DONE]':
            delta = json.loads(line[6:])['choices'][0]['delta']
            content += delta.get('content', '')
    assert content == 'How can I help with parts today?'


def test_stream_tool_call_finishes_with_tool_calls_reason():
    c = _client(lambda m: {'tool_call': [{'id': 't', 'type': 'function', 'function': {
        'name': 'resolve_part', 'arguments': json.dumps({'text': 'is K5-24SBC in stock'})}}]})
    r = c.post('/v1/chat/completions', json={'stream': True, 'messages': [
        {'role': 'user', 'content': 'is K5-24SBC in stock?'}]})
    text = r.text
    assert 'tool_calls' in text and 'data: [DONE]' in text
    finishes = [json.loads(l[6:])['choices'][0]['finish_reason']
                for l in text.splitlines()
                if l.startswith('data: ') and l[6:].strip() != '[DONE]']
    assert 'tool_calls' in finishes


# -- auth --------------------------------------------------------------------

def test_auth_required_when_configured(monkeypatch):
    monkeypatch.setenv('CUSTOM_LLM_API_KEY', 'sekret')
    c = _client(lambda m: 'hi')
    body = {'messages': [{'role': 'user', 'content': 'hi'}]}
    assert c.post('/v1/chat/completions', json=body).status_code == 403
    assert c.post('/v1/chat/completions', json=body,
                  headers={'Authorization': 'Bearer wrong'}).status_code == 403
    assert c.post('/v1/chat/completions', json=body,
                  headers={'Authorization': 'Bearer sekret'}).status_code == 200


# -- over-budget through the route -> fallback -------------------------------

def test_over_budget_returns_fallback_through_the_route():
    import time as _t

    def slow(_m):
        _t.sleep(0.2)
        return 'too late to matter'
    c = _client(slow, budget_secs=0.03)
    r = c.post('/v1/chat/completions',
               json={'messages': [{'role': 'user', 'content': 'hi'}]})
    # over-budget = no usable model output, topic unknown -> SOFT service fallback,
    # not the part-number line (which would be a non-sequitur on small talk).
    assert r.json()['choices'][0]['message']['content'] == SERVICE_FALLBACK


def test_small_talk_failure_is_tonally_coherent_not_a_part_number_nonsequitur():
    # the CX guard: a free turn that fails (model error here, but same path as the
    # keyless boot / key outage) must NOT answer small talk with "let me get a rep
    # to confirm that part number."
    def boom(_m):
        raise RuntimeError('model unavailable')
    c = _client(boom)
    r = c.post('/v1/chat/completions', json={'messages': [
        {'role': 'user', 'content': "thanks, you've been really helpful"}]})
    spoken = r.json()['choices'][0]['message']['content']
    assert spoken == SERVICE_FALLBACK
    assert 'part number' not in spoken.lower()       # no non-sequitur


# -- instrumentation from the first call: the JSONL ledger -------------------

def test_per_route_latency_ledger_is_written(tmp_path, monkeypatch):
    log = tmp_path / 'clm_trace.jsonl'
    monkeypatch.setenv('CUSTOM_LLM_TRACE_LOG', str(log))
    c = _client(_explode)
    # a substitution turn and a free turn -> two distinct routes recorded
    c.post('/v1/chat/completions', json={'messages': [
        {'role': 'user', 'content': 'stock?'}, _tool_msg()]})
    c2 = _client(lambda m: 'hello there')
    monkeypatch.setenv('CUSTOM_LLM_TRACE_LOG', str(log))
    c2.post('/v1/chat/completions',
            json={'messages': [{'role': 'user', 'content': 'hi'}]})
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    routes = {r['route'] for r in rows}
    assert 'substitute_say' in routes and 'free' in routes
    for r in rows:
        assert 'latency_secs' in r and 'wall_secs' in r   # per-route latency present
    sub = next(r for r in rows if r['route'] == 'substitute_say')
    assert sub['model_invoked'] is False
