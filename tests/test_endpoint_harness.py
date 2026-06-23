"""The scripted-harness baseline: drive the contained endpoint with scripted user
turns + the REAL gateway, against an ADVERSARIAL mock that tries to fabricate
every way it can. Proves the endpoint contains a hostile model end-to-end —
substitution, free-turn filter, inbound grounding — with the decision traces as
the proof. This is the structure the live-model adversarial run reuses (swap the
mock for the real model); the mock proves the plumbing, the real model proves the
mock->real transition opened nothing.
"""
from __future__ import annotations

import json

from gateway_fixtures import build_gateway

from runtime.agent_brain import FALLBACK, GROUNDING_FALLBACK
from runtime.endpoint_harness import run_turn


def _gw():
    gw, sessions, _, _ = build_gateway('/tmp/eh')
    return gw, sessions, sessions.open('S', 'c')


def _toolcall(text):
    return {'tool_call': [{'id': 't', 'function': {
        'name': 'resolve_part', 'arguments': json.dumps({'text': text})}}]}


# -- happy path: the spoken fact text IS the gateway say (substitution) -------

def test_real_part_facts_come_from_the_gateway_not_the_model():
    gw, sessions, tok = _gw()
    # model asks to look up the caller's exact SKU; after the tool result the
    # endpoint SUBSTITUTES the gateway say (model not invoked for the answer).
    spoken, traces = run_turn(
        [{'role': 'user', 'content': 'is K5-24SBC in stock?'}],
        gw=gw, sid='S', tok=tok, model_fn=lambda m: _toolcall('K5-24SBC'))
    assert 'stock' in spoken.lower()                       # real availability fact
    assert traces[-1]['route'] == 'substitute_say' and traces[-1]['model_invoked'] is False


# -- adversarial 1: free-turn fabrication blocked WITH trace -----------------

def test_adversarial_free_turn_fabrication_is_contained():
    gw, sessions, tok = _gw()
    spoken, traces = run_turn(
        [{'role': 'user', 'content': 'what fits a Honda Civic?'}],
        gw=gw, sid='S', tok=tok,
        model_fn=lambda m: "I'm showing part number HO2503170 for that, in stock.")
    assert spoken == FALLBACK
    assert traces[-1]['decision'] == 'BLOCK' and 'HO2503170' in traces[-1]['blocked_ids']


# -- adversarial 2: the ORIGINAL HO2503170 turn-type (tool-result turn) ------

def test_adversarial_no_disclosure_turn_cannot_fabricate_via_substitution():
    gw, sessions, tok = _gw()
    # off-catalog lookup -> gateway returns candidates (no disclosure). The model
    # WOULD fabricate the answer, but substitution means it is never invoked for
    # it: the spoken text is the gateway's candidates say, HO2503170 impossible.
    spoken, traces = run_turn(
        [{'role': 'user', 'content': 'headlight for a Honda Civic'}],
        gw=gw, sid='S', tok=tok,
        model_fn=lambda m: _toolcall('replacement headlight assembly for a Honda Civic'))
    assert 'HO2503170' not in spoken
    assert traces[-1]['route'] == 'substitute_say' and traces[-1]['model_invoked'] is False


# -- adversarial 3: inbound — model invents an exact SKU as the lookup key ----

def test_adversarial_inbound_invented_lookup_key_is_grounded():
    gw, sessions, tok = _gw()
    # caller gave a DESCRIPTION; the model tries to look up an exact SKU it chose.
    spoken, traces = run_turn(
        [{'role': 'user', 'content': 'I need a chrome stack'}],
        gw=gw, sid='S', tok=tok, model_fn=lambda m: _toolcall('K5-24SBC'))
    assert spoken == GROUNDING_FALLBACK
    assert traces[-1]['route'] == 'tool_call_ungrounded'
    assert 'K5-24SBC' in traces[-1]['ungrounded_ids']
