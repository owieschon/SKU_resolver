"""P1 — in-situ ambiguity resolution + graceful degradation/escalation.

Covers: explicit handoff, out-of-scope escalation, repeated-failure
escalation, informed disambiguation (named missing field / contrasted
candidates), and that a normal answerable turn never escalates.
"""
from __future__ import annotations

from gateway import Channel, EventType
from gateway_fixtures import build_gateway


def _open(gw, sessions, sid='S'):
    return sessions.open(sid, f'chan-{sid}')


# explicit handoff request -> immediate escalation + journaled
def test_explicit_handoff_escalates(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    r = gw.turn('S', tok, 'can I just talk to a real person?', channel=Channel.TYPED)
    assert r.kind == 'escalate'
    assert r.escalation.reason == 'explicit_request'
    assert r.escalation.action == 'connect_to_agent'
    assert 'connect you' in r.text.lower()
    assert journal.events(EventType.ESCALATED)


# out-of-scope question -> escalation, not a bogus part lookup
def test_out_of_scope_escalates(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    for q in ('where is my order, it was supposed to ship last week',
              'I need to dispute an invoice on my account',
              'what is the weather in cleveland today'):
        r = gw.turn('S', tok, q, channel=Channel.TYPED)
        assert r.kind == 'escalate', q
        assert r.escalation.reason == 'out_of_scope'


# a genuine free-text PART description is NOT mistaken for out-of-scope
def test_free_text_part_not_escalated_as_out_of_scope(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    r = gw.turn('S', tok, '5 inch chrome curved stack 24 long SB',
                channel=Channel.TYPED)
    assert r.kind != 'escalate'    # it resolves (or asks), never hands off


# repeated unresolvable attempts -> escalation after the threshold
def test_repeated_failure_escalates(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    # part-ish text that won't cleanly resolve: first turn is a non-answer
    # (clarify or candidate list), second consecutive non-answer hands off.
    r1 = gw.turn('S', tok, 'the zzqq part sku thing', channel=Channel.TYPED)
    assert r1.kind in ('unknown', 'identify')         # first non-answer
    r2 = gw.turn('S', tok, 'the xxyy part sku item', channel=Channel.TYPED)
    assert r2.kind == 'escalate'                      # second non-answer: hand off
    assert r2.escalation.reason == 'repeated_failure'


# informed disambiguation: a partial spec asks the SPECIFIC missing field
def test_informed_question_names_missing_field(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    # 'K5' family + diameter but no finish/body/length -> open questions
    r = gw.turn('S', tok, 'I need a K5 stack', channel=Channel.TYPED)
    if r.candidates or r.needs_confirmation:
        low = r.text.lower()
        # asks about a concrete attribute, not a bare "which one?"
        assert any(w in low for w in ('finish', 'body', 'length', 'diameter',
                                      'did you mean'))
        assert 'which one?' != low.strip()


# a normal answerable turn never escalates
def test_answerable_turn_does_not_escalate(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    r = gw.turn('S', tok, 'is K5-24SBC in stock?', channel=Channel.TYPED)
    assert r.kind == 'availability' and r.escalation is None


# success resets the failure counter (one miss then a hit doesn't pre-arm)
def test_failure_counter_resets_on_success(tmp_path):
    gw, sessions, journal, _ = build_gateway(tmp_path)
    tok = _open(gw, sessions)
    gw.turn('S', tok, 'the zzqq part sku thing', channel=Channel.TYPED)   # non-answer
    gw.turn('S', tok, 'K5-24SBC', channel=Channel.TYPED)                  # hit -> reset
    r = gw.turn('S', tok, 'the wwvv part sku item', channel=Channel.TYPED)
    # counter reset by the hit, so this is back to a first non-answer, NOT escalation
    assert r.kind != 'escalate'
