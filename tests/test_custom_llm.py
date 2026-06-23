"""The mapper is the seam the brain's proofs all assume but none test: everything
downstream is accurate only if the normalized turns are a faithful image of what
ElevenLabs sends. These tests exercise exactly what the mock-model brain tests
can't — role assignment (incl. self-laundering AT the mapping layer), provenance
round-trip fidelity, and unmappable-payload fail-closed.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from gateway_fixtures import build_gateway

from gateway import Channel
from gateway.provenance import surfaced
from gateway.spoken import voice_render
from runtime.agent_brain import FALLBACK, SERVICE_FALLBACK
from runtime.custom_llm import (
    MappingError,
    handle,
    handle_async,
    map_request,
    map_response,
    parse_tool_content,
)


def _explode(_m):
    raise AssertionError('model must not be invoked on a tool-result turn')


def _gateway_tool_result(text):
    """Build a real /agent/turn result the way app.py emits it, then SERIALIZE it
    the way ElevenLabs would carry a tool message's content."""
    gw, sessions, _, _ = build_gateway('/tmp/cl-' + text[:6].replace(' ', ''))
    tok = sessions.open('S', 'c')
    # /agent/turn runs on TYPED (the agent tool channel); verify so pricing discloses
    gw.turn('S', tok, 'my account number is 1001', channel=Channel.TYPED)
    resp = gw.turn('S', tok, text, channel=Channel.TYPED)
    skus, values = surfaced(resp)
    result = {'say': voice_render(resp.text), 'kind': resp.kind,
              'surfaced_skus': list(skus), 'surfaced_values': values}
    return result, json.dumps(result)            # the dict, and its serialized form


# -- role assignment ---------------------------------------------------------

def test_role_assignment_is_typed_and_drops_system():
    body = {'messages': [
        {'role': 'system', 'content': 'you are the parts line'},
        {'role': 'user', 'content': 'is K5-24SBC in stock?'},
        {'role': 'assistant', 'content': None,
         'tool_calls': [{'id': 't1', 'type': 'function',
                         'function': {'name': 'resolve_part', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 't1',
         'content': json.dumps({'say': 'in stock', 'surfaced_skus': ['K5-24SBC'],
                                'surfaced_values': {'qty': 58}})},
    ]}
    norm = map_request(body)
    assert [m['role'] for m in norm] == ['user', 'assistant', 'tool']   # system dropped
    assert norm[-1]['result']['surfaced_skus'] == ['K5-24SBC']


def test_self_laundering_at_the_mapping_layer():
    # A fabricated SKU in an ASSISTANT message must be mapped to role 'assistant'
    # so the brain's role-typing ignores it. This proves the mapper assigns the
    # role the brain's safety depends on.
    body = {'messages': [
        {'role': 'user', 'content': 'got any stacks?'},
        {'role': 'assistant', 'content': 'part number FAKE-9999 is one option'},
        {'role': 'user', 'content': 'tell me about that one'},
    ]}
    norm = map_request(body)
    assert norm[1]['role'] == 'assistant'           # NOT tool, NOT user
    # end-to-end: the agent cannot now quote the laundered number
    resp, trace = handle(body, model_fn=lambda m: 'Sure, the FAKE-9999 is in stock.')
    assert resp['choices'][0]['message']['content'] == FALLBACK
    assert 'FAKE-9999' in trace['blocked_ids']


# -- provenance round-trip fidelity ------------------------------------------

def test_surfaced_values_survive_the_round_trip():
    result, serialized = _gateway_tool_result('how much is K5-24SBC?')
    assert result['surfaced_values'].get('unit_price')          # gateway emitted a price
    parsed = parse_tool_content(serialized)                     # as the brain receives it
    assert parsed['surfaced_values'] == result['surfaced_values']   # byte-faithful
    # and through the full mapper:
    norm = map_request({'messages': [
        {'role': 'user', 'content': 'price?'},
        {'role': 'tool', 'content': serialized}]})
    assert norm[-1]['result']['surfaced_values'] == result['surfaced_values']


def test_tool_turn_substitutes_say_through_the_mapper():
    _, serialized = _gateway_tool_result('is K5-24SBC in stock?')
    say = json.loads(serialized)['say']
    body = {'messages': [{'role': 'user', 'content': 'stock?'},
                         {'role': 'tool', 'content': serialized}]}
    resp, trace = handle(body, model_fn=_explode)               # model must NOT run
    assert resp['choices'][0]['message']['content'] == say
    assert trace['route'] == 'substitute_say' and trace['model_invoked'] is False


# -- mapping failure = fail-closed -------------------------------------------

@pytest.mark.parametrize('body', [
    {'messages': [{'role': 'martian', 'content': 'hi'}]},          # unknown role
    {'messages': [{'role': 'tool', 'content': 'not json at all'}]},  # unparseable tool content
    {'messages': [{'role': 'tool', 'content': json.dumps({'say': 'x'})}]},  # missing provenance keys
    {'messages': []},                                              # empty
    {'no_messages': True},                                         # malformed
])
def test_unmappable_payload_fails_closed_to_fallback(body):
    resp, trace = handle(body, model_fn=lambda m: 'should never run')
    # no model output -> service fallback (topic unknown), not the part-number line
    assert resp['choices'][0]['message']['content'] == SERVICE_FALLBACK
    assert trace['route'] == 'mapping_error' and trace['fallback_used']


def test_partial_provenance_raises_not_best_effort():
    with pytest.raises(MappingError):
        parse_tool_content(json.dumps({'say': 'in stock', 'surfaced_skus': ['K5-24SBC']}))  # no surfaced_values


# -- over-budget -------------------------------------------------------------

def test_over_budget_converts_to_fallback_and_is_traced():
    def slow(_m):
        time.sleep(0.03)
        return 'a fine free-turn reply'
    body = {'messages': [{'role': 'user', 'content': 'hello there'}]}
    resp, trace = handle(body, model_fn=slow, budget_secs=0.005)
    assert resp['choices'][0]['message']['content'] == SERVICE_FALLBACK
    assert trace.get('over_budget') is True and trace['fallback_used']


# -- response shaping --------------------------------------------------------

def test_map_response_text_and_tool_call_shapes():
    assert map_response('hi')['choices'][0]['message']['content'] == 'hi'
    tc = map_response({'tool_call': [{'id': 'x'}]})
    assert tc['choices'][0]['message']['tool_calls'] == [{'id': 'x'}]


# -- the free-turn model must run WITH its system prompt ---------------------

def test_free_turn_model_receives_system_prompt():
    seen = {}

    def spy(messages):
        seen['msgs'] = messages
        return 'hi there, how can I help?'
    body = {'messages': [{'role': 'system', 'content': 'you are Sam, the parts line'},
                         {'role': 'user', 'content': 'hello'}]}
    handle(body, model_fn=spy)
    assert 'system' in [m['role'] for m in seen['msgs']]   # not run blind


# -- the async deadline is REAL: fires while the model is still hung ---------

def test_async_real_deadline_returns_fallback_before_a_hung_model_finishes():
    def hung(_messages):
        time.sleep(0.30)                                   # model hangs past B
        return 'too late to matter'
    body = {'messages': [{'role': 'user', 'content': 'hi'}]}
    resp, trace = asyncio.run(handle_async(body, model_fn=hung, budget_secs=0.03))
    assert resp['choices'][0]['message']['content'] == SERVICE_FALLBACK
    assert trace['over_budget'] is True and trace['route'] == 'over_budget'
    assert trace['latency_secs'] < 0.20                    # returned at ~B, NOT after 0.30


def test_async_model_fn_is_really_aborted_at_the_deadline():
    # The live integrity property: an ASYNC model_fn awaited under the deadline is
    # genuinely CANCELLED when B fires — not left running in the background while
    # we return fallback (which is what a sync fn in a thread does). We prove the
    # coroutine received CancelledError and never reached its completion line.
    state = {'cancelled': False, 'completed': False}

    async def slow(_messages):
        try:
            await asyncio.sleep(0.5)               # past B
        except asyncio.CancelledError:
            state['cancelled'] = True              # the abort reached the request
            raise
        state['completed'] = True                  # must NEVER run
        return 'too late to matter'

    body = {'messages': [{'role': 'user', 'content': 'hi'}]}
    resp, trace = asyncio.run(handle_async(body, model_fn=slow, budget_secs=0.03))
    assert resp['choices'][0]['message']['content'] == SERVICE_FALLBACK
    assert trace['over_budget'] is True and trace['route'] == 'over_budget'
    assert state['cancelled'] is True              # REAL abort, not a leaked task
    assert state['completed'] is False             # it did not keep running


def test_async_substitution_never_touches_the_model():
    _, serialized = _gateway_tool_result('is K5-24SBC in stock?')
    body = {'messages': [{'role': 'user', 'content': 'x'},
                         {'role': 'tool', 'content': serialized}]}
    resp, trace = asyncio.run(handle_async(body, model_fn=_explode))
    assert trace['route'] == 'substitute_say' and trace['model_invoked'] is False


# -- every route is timed (not just over-budget-on-model-turns) -------------

def test_every_route_carries_latency_tagged():
    _, serialized = _gateway_tool_result('is K5-24SBC in stock?')
    _, t_sub = handle({'messages': [{'role': 'user', 'content': 'x'},
                                    {'role': 'tool', 'content': serialized}]},
                      model_fn=_explode)
    assert 'latency_secs' in t_sub and t_sub['route'] == 'substitute_say'
    _, t_map = handle({'messages': [{'role': 'martian'}]}, model_fn=lambda m: 'x')
    assert 'latency_secs' in t_map and t_map['route'] == 'mapping_error'
