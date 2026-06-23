"""Structural gate: internal resolution state never reaches the spoken say.

The analogue of test_provenance's completeness invariant. Its FIRST TWO tests are
the two leaks the live adversarial run surfaced — a BM25 score and a taxonomy-code
option list — now caught structurally instead of by review. The rest prove the
fix at the source (informed_question no longer authors either) and prove the guard
is COLLISION-SAFE with the legitimate spelled-SKU readback (a real part number
read back for confirmation must pass).
"""
from __future__ import annotations

import pytest
from gateway_fixtures import build_gateway

from gateway import Channel
from gateway.escalation import informed_question
from gateway.say_guard import (
    InternalStateLeak,
    assert_no_internal_state,
    internal_state_tokens,
    safe_voice_say,
)
from gateway.spoken import voice_render
from resolution.service import Candidate, OpenQuestion

# -- the two leaks from the live adversarial run, verbatim -------------------

def test_guard_catches_bm25_score_leak():
    # exactly what reached the caller: "...R D 690 S (B M 25 score 9.359)..."
    leaked = ('Did you mean one of these? R D 690 S (B M 25 score 9.359); '
              'P F, 35 S S E X (B M 25 score 6.48)')
    with pytest.raises(InternalStateLeak):
        assert_no_internal_state(leaked)
    assert internal_state_tokens(leaked)        # score + B M 25 both flagged


def test_guard_catches_taxonomy_code_option_list():
    leaked = 'I can narrow it down — what body style (SB, EX, XB)?'
    with pytest.raises(InternalStateLeak):
        assert_no_internal_state(leaked)
    assert '(SB, EX, XB)' in internal_state_tokens(leaked)


# -- the fix at the source: informed_question authors neither ----------------

def test_candidate_readback_uses_description_not_score():
    # a bm25-sourced candidate carries an internal `reason` with the score AND a
    # caller-safe `description`. The readback must speak the description, never
    # the reason.
    cand = Candidate(sku='RD690SBC',
                     reason='bm25 score 9.359: chrome 6.90 curved stack',
                     source='retrieval:bm25',
                     description='chrome 6.90 curved stack')
    say = voice_render(informed_question([], [cand]))
    assert_no_internal_state(say)                       # structurally clean
    assert 'score' not in say.lower() and '9.359' not in say
    assert 'curved stack' in say                        # the useful part survived


def test_missing_field_question_names_attribute_not_codes():
    oq = OpenQuestion(field='body_unspecified',
                      reason='Family K requires a body code (SB=OD-fit, ...)',
                      options=('SB', 'EX', 'XB'))
    say = voice_render(informed_question([oq], []))
    assert_no_internal_state(say)
    assert 'body style' in say
    for code in ('SB', 'EX', 'XB'):
        assert code not in say                          # no internal codes spoken


def test_family_conflict_field_does_not_speak_codes_or_internal_label():
    oq = OpenQuestion(field='family_conflict',
                      reason='ambiguous family', options=('A', 'SS'))
    say = voice_render(informed_question([oq], []))
    assert_no_internal_state(say)
    assert '(A, SS)' not in say
    assert 'conflict' not in say                        # not "family conflict"


# -- collision-safety: legitimate says MUST pass -----------------------------

@pytest.mark.parametrize('clean', [
    'Yep, the K5-24SBC is in stock. It ships by tomorrow afternoon.',   # boolean
    "That's 6.90 each. Ships by the ninth.",                            # price
    'The lead time is 5 days.',                                         # lead-time #
    'Did you mean one of these? K5-24SBC (the 5 by 24 inch chrome stack)',
    'I can narrow it down — what finish?',
    'I can narrow it down — what body style — an OD-fit, an ID-fit, or a variant?',
])
def test_real_spelled_sku_and_prices_pass(clean):
    # voice_render spells the SKU ("K 5, 24 S B C") — the guard must NOT trip on it,
    # nor on ship-times / lead-time numbers / dimensions / prices (the trailing word
    # isn't an on-hand word)
    assert_no_internal_state(voice_render(clean))


# -- invariant 5: on-hand QUANTITY is internal state, never spoken -----------

@pytest.mark.parametrize('leak', [
    'Yep, the K5-24SBC is in stock — 58 on hand.',     # the real legacy-say shape
    'We have 58 in stock.',
    'There are 12 left.',
    'qty: 58',
    'we have 200 units',
])
def test_quantity_disclosures_are_caught(leak):
    with pytest.raises(InternalStateLeak):
        assert_no_internal_state(voice_render(leak))


# -- the runtime boundary fails SAFE (no crash, no leak) ---------------------

def test_safe_voice_say_passes_clean_boolean_availability():
    out = safe_voice_say('Yep, the K5-24SBC is in stock. It ships by tomorrow.')
    assert 'K 5, 24 S B C' in out and 'rep' not in out.lower()


def test_safe_voice_say_suppresses_a_quantity_leak_to_handoff():
    out = safe_voice_say('Yep, the K5-24SBC is in stock — 58 on hand.')
    assert 'on hand' not in out and 'rep' in out.lower()     # quantity suppressed


def test_safe_voice_say_suppresses_a_leak_to_handoff():
    out = safe_voice_say('what body style (SB, EX, XB)?')
    assert 'SB' not in out and 'rep' in out.lower()     # degraded to safe hand-off


# -- the real say path, end to end through the gateway -----------------------

def test_bm25_disambiguation_say_is_clean_through_the_gateway():
    gw, sessions, _, _ = build_gateway('/tmp/sayguard-e2e')
    tok = sessions.open('S', 'c')
    # a vague description that drops to the bm25 candidate-readback path
    resp = gw.turn('S', tok, 'I need a chrome stack, the shiny five inch one',
                   channel=Channel.TYPED)
    say = safe_voice_say(resp.text)
    assert_no_internal_state(say)                       # real gateway say, clean
    assert 'score' not in say.lower()
