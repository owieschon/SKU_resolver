"""The brain is the fabrication-containment seam. Every redline from the design is
a test here. The critical ones: substitution keeps the model out of the fact
path, the role-typed allowlist blocks self-laundering, and the adversarial free
turn is blocked WITH a decision trace proving the filter ran (a green "no SKU
spoken" is vacuous without it).
"""
from __future__ import annotations

import json

from runtime.agent_brain import (
    FALLBACK,
    GROUNDING_FALLBACK,
    SERVICE_FALLBACK,
    decide_turn,
    detect_ids_broad,
    detect_ids_tight,
    reconstruct,
)


def _toolcall(text):
    return {'tool_call': [{'function': {'name': 'resolve_part',
                                        'arguments': json.dumps({'text': text})}}]}


def _explode(_messages):
    raise AssertionError('model_fn must NOT be invoked on a tool-result turn')


def _tool(say, skus=(), values=None, kind='availability'):
    return {'role': 'tool', 'result': {
        'say': say, 'surfaced_skus': list(skus),
        'surfaced_values': values or {}, 'kind': kind}}


# -- substitution: the model is never in the fact path -----------------------

def test_tool_result_turn_substitutes_say_without_invoking_model():
    msgs = [{'role': 'user', 'content': 'is K5-24SBC in stock?'},
            _tool('Yep, the K5-24SBC is in stock, 58 on hand.',
                  skus=['K5-24SBC'], values={'qty': 58})]
    text, trace = decide_turn(msgs, model_fn=_explode)   # _explode raises if called
    assert text == 'Yep, the K5-24SBC is in stock, 58 on hand.'
    assert trace['route'] == 'substitute_say' and trace['model_invoked'] is False


def test_no_disclosure_tool_turn_also_substitutes_closing_the_tokenless_hole():
    # candidates turn: the model is NOT invoked, so it cannot fabricate
    # "those are in stock" or a part number like HO2503170.
    msgs = [{'role': 'user', 'content': 'headlight for a Civic'},
            _tool('Did you mean one of these? ASMB0880, FL-30008-000.',
                  skus=['ASMB0880', 'FL-30008-000'], values={}, kind='candidates')]
    text, trace = decide_turn(msgs, model_fn=_explode)
    assert text.startswith('Did you mean') and trace['model_invoked'] is False


# -- fail-closed -------------------------------------------------------------

def test_model_error_fails_to_service_fallback():
    # model produced nothing -> topic unknown -> the SOFT service fallback, not the
    # part-number line (which would be a non-sequitur on a small-talk turn).
    msgs = [{'role': 'user', 'content': 'hi'}]
    text, trace = decide_turn(msgs, model_fn=lambda m: (_ for _ in ()).throw(RuntimeError('timeout')))
    assert text == SERVICE_FALLBACK and trace['decision'] == 'BLOCK' and trace['fallback_used']


# -- self-laundering: a prior assistant fabrication must not enter the allowlist

def test_self_laundering_blocked_by_role_type():
    msgs = [{'role': 'user', 'content': 'got any stacks?'},
            {'role': 'assistant', 'content': 'part number FAKE-9999 is one option'},  # fabrication in history
            {'role': 'user', 'content': 'tell me about that one'}]
    a = reconstruct(msgs)
    assert 'FAKE9999' not in a.tier1 and 'FAKE9999' not in a.tier2   # never read from assistant
    # and the agent cannot now quote it:
    text, trace = decide_turn(msgs, model_fn=lambda m: 'Sure, the FAKE-9999, it is in stock.')
    assert text == FALLBACK and 'FAKE-9999' in trace['blocked_ids']


# -- adversarial: invented SKU blocked WITH a proving trace ------------------

def test_adversarial_invented_sku_blocked_with_decision_trace():
    msgs = [{'role': 'user', 'content': 'what fits a Honda Civic?'}]
    text, trace = decide_turn(
        msgs, model_fn=lambda m: "I'm showing part number HO2503170 for that.")
    assert text == FALLBACK
    # the trace PROVES the containment ran and blocked the unlisted token —
    # without this the "no SKU spoken" assertion would be vacuous.
    assert trace['model_invoked'] is True
    assert trace['decision'] == 'BLOCK'
    assert 'HO2503170' in trace['blocked_ids']
    assert trace['spoken_ids']['HO2503170'] == 'INVENTED'


# -- value collision (typed): a stale qty must not launder a fabricated price -

def test_stale_quantity_does_not_launder_a_fabricated_price():
    msgs = [{'role': 'user', 'content': 'K5-24SBC?'},
            _tool('58 on hand.', skus=['K5-24SBC'], values={'qty': 58}),
            {'role': 'user', 'content': "what's it cost?"}]
    # 58 was surfaced as a QUANTITY; the agent now fabricates a $58 PRICE.
    text, trace = decide_turn(msgs, model_fn=lambda m: "That'll be 58 dollars.")
    assert text == FALLBACK and 'unit_price:58' in trace['blocked_values']


def test_surfaced_price_back_reference_allowed_same_kind():
    msgs = [{'role': 'user', 'content': 'price on K5-24SBC?'},
            _tool('$187.71 each.', skus=['K5-24SBC'], values={'unit_price': 187.71}),
            {'role': 'user', 'content': 'remind me what that was'}]
    text, trace = decide_turn(msgs, model_fn=lambda m: 'The K5-24SBC was 187.71.')
    assert text != FALLBACK and trace['decision'] == 'ALLOW'


# -- tiers: tier1 quotable, tier2 echo-only ----------------------------------

def test_tier2_caller_spoken_echo_is_allowed():
    msgs = [{'role': 'user', 'content': 'you got a K5-24SPC?'}]
    a = reconstruct(msgs)
    assert 'K524SPC' in a.tier2 and 'K524SPC' not in a.tier1
    text, _ = decide_turn(msgs, model_fn=lambda m: 'Let me check that K5-24SPC for you.')
    assert text != FALLBACK                          # echo of the caller's number is fine


def test_tier1_tool_surfaced_reference_is_allowed():
    msgs = [{'role': 'user', 'content': 'K5-24SBC?'},
            _tool('in stock.', skus=['K5-24SBC'], values={'qty': 58}),
            {'role': 'user', 'content': 'great'}]
    text, _ = decide_turn(msgs, model_fn=lambda m: 'Anything else on the K5-24SBC?')
    assert text != FALLBACK


# -- detectors ---------------------------------------------------------------

def test_broad_detector_catches_any_format_and_spelled():
    ids = ' '.join(detect_ids_broad('HO2503170 FL-30008-000 K5-24SBC and H O 2 5 0 3 1 7 0'))
    norm = ids.replace('-', '').upper().replace(' ', '')
    assert 'HO2503170' in norm and 'FL30008000' in norm and 'K524SBC' in norm

def test_tight_input_detector_excludes_bare_account_numbers():
    assert detect_ids_tight('my account number is 1001') == []      # not a part
    assert 'K5-24SPC' in detect_ids_tight('you got a K5-24SPC?')


# -- INBOUND containment: the model can't launder a fabricated LOOKUP key ----

def test_inbound_ungrounded_tool_call_argument_is_blocked():
    # caller gave a DESCRIPTION; the model invents an exact in-catalog SKU as the
    # lookup key (which the gateway would resolve authoritatively). Grounded out.
    msgs = [{'role': 'user', 'content': 'I need a chrome stack'}]
    text, trace = decide_turn(msgs, model_fn=lambda m: _toolcall('K5-24SBC'))
    assert text == GROUNDING_FALLBACK
    assert trace['route'] == 'tool_call_ungrounded' and 'K5-24SBC' in trace['ungrounded_ids']


def test_inbound_description_argument_passes():
    msgs = [{'role': 'user', 'content': 'I need a chrome stack'}]
    out, trace = decide_turn(msgs, model_fn=lambda m: _toolcall('chrome stack'))
    assert isinstance(out, dict) and 'tool_call' in out and trace['route'] == 'tool_call'


def test_inbound_caller_spoken_argument_passes():
    msgs = [{'role': 'user', 'content': 'you got a K5-24SBC?'}]   # caller said it -> tier2
    out, trace = decide_turn(msgs, model_fn=lambda m: _toolcall('K5-24SBC'))
    assert 'tool_call' in out and trace['route'] == 'tool_call'


def test_inbound_tool_surfaced_argument_passes():
    msgs = [{'role': 'user', 'content': 'K5-24SBC?'},
            _tool('in stock', skus=['K5-24SBC'], values={'qty': 58}),
            {'role': 'user', 'content': 'is that available'}]
    out, trace = decide_turn(msgs, model_fn=lambda m: _toolcall('K5-24SBC availability'))
    assert 'tool_call' in out and trace['route'] == 'tool_call'   # tier1 grounds it
