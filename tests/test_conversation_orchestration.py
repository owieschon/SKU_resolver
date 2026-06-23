"""Orchestration layer behaviors (CONVERSATION_STATE_SPEC §5-6, build step 4):
state updates, focus/anaphora reference resolution, the gate-enforced
read_and_disclose, per-part-explicit say, the pricing-unreadable handoff, and the
closure loop. The §10 adversarial proofs live in test_section10_adversarial.py;
these are the constructive behaviors.
"""
from __future__ import annotations

from gateway.conversation import Conversation
from gateway.conversation_state import Fact, FactType
from gateway.say_guard import internal_state_tokens


def make_reader(*, avail=True, price='$187.71', lead='5 days',
                price_unreadable=False):
    def reader(part, ft, account, now):
        if ft is FactType.AVAILABILITY:
            return Fact.read(avail, as_of=now)
        if ft is FactType.LEAD_TIME:
            return Fact.read(lead, as_of=now)
        if ft is FactType.PRICE:
            if price_unreadable:
                return Fact.unreadable()
            return Fact.read(price, as_of=now, account_id=account.account_id)
    return reader


NOW = 1_000_000.0


def test_add_part_takes_focus_and_identify():
    c = Conversation()
    c.add_part('p1', 'the big chrome stack')
    assert c.state.focus == 'p1'
    c.identify_part('p1', 'K5-24SBC')
    assert c.state.parts['p1'].identity.is_identified


def test_resolve_reference_zero_one_many():
    c = Conversation()
    c.add_part('p1'); c.add_part('p2')
    assert c.resolve_reference([]) == ('none', None)
    assert c.resolve_reference(['p1']) == ('resolved', 'p1')
    kind, ids = c.resolve_reference(['p1', 'p2'])
    assert kind == 'disambiguate' and set(ids) == {'p1', 'p2'}


def test_availability_discloses_boolean_no_quantity_leak():
    c = Conversation()
    c.add_part('p1', 'the chrome stack'); c.identify_part('p1', 'K5-24SBC')
    out = c.read_and_disclose(['p1'], [FactType.AVAILABILITY],
                              reader=make_reader(avail=True), now=NOW)
    assert out.spoken and not out.blocked
    assert 'in stock' in out.say.lower()
    assert internal_state_tokens(out.say) == []          # invariant 5: no quantity


def test_multipart_availability_is_per_part_explicit():
    c = Conversation()
    c.add_part('A'); c.identify_part('A', 'K5-24SBC')
    c.add_part('B'); c.identify_part('B', 'BH6-36SBC')

    def mixed(part, ft, account, now):
        return Fact.read(part.ctx_id == 'A', as_of=now)   # A in stock, B not
    out = c.read_and_disclose(['A', 'B'], [FactType.AVAILABILITY],
                              reader=mixed, now=NOW)
    assert 'K5-24SBC is in stock' in out.say
    assert 'BH6-36SBC is not in stock' in out.say         # each explicit, not aggregated


def test_price_after_account_and_identity_discloses():
    c = Conversation()
    c.add_part('p1'); c.identify_part('p1', 'K5-24SBC')
    c.establish_account('1001')
    out = c.read_and_disclose(['p1'], [FactType.PRICE],
                              reader=make_reader(), now=NOW)
    assert out.spoken and '187.71' in out.say


def test_unreadable_price_is_blocked_for_handoff():
    # §8 interim: no pricing source -> precondition met but the fact is unreadable
    c = Conversation()
    c.add_part('p1'); c.identify_part('p1', 'K5-24SBC')
    c.establish_account('1001')
    out = c.read_and_disclose(['p1'], [FactType.PRICE],
                              reader=make_reader(price_unreadable=True), now=NOW)
    assert out.all_blocked and out.blocked[0][2] == 'unreadable'
    assert out.say == ''                                   # nothing spoken -> can't-quote handoff


def test_bundle_reads_share_an_as_of_across_parts():
    # §4 coherence: in-scope facts read together at one `now` share an as_of
    c = Conversation()
    c.add_part('A'); c.identify_part('A', 'K5-24SBC')
    c.add_part('B'); c.identify_part('B', 'BH6-36SBC')
    c.read_and_disclose(['A', 'B'], [FactType.AVAILABILITY],
                        reader=make_reader(avail=True), now=NOW)
    assert (c.state.parts['A'].availability.as_of
            == c.state.parts['B'].availability.as_of == NOW)


def test_disclosure_never_closes_only_the_caller_does():
    # invariant 7: a successful disclosure is NOT an end state
    c = Conversation()
    c.add_part('p1'); c.identify_part('p1', 'K5-24SBC')
    c.read_and_disclose(['p1'], [FactType.AVAILABILITY],
                        reader=make_reader(avail=True), now=NOW)
    assert c.is_complete is False                          # disclosure did not close
    c.note_completion_signal()                             # only the caller closes
    assert c.is_complete is True

