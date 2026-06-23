"""§10 adversarial obligations (CONVERSATION_STATE_SPEC, build step 6) — the proof
that the orchestration layer's NEW agency is safe: the gate holds against any
conversational path the LLM can take. Each obligation is the deterministic twin of
a conversational freedom granted in §5-6; every place the LLM gained latitude, a
gate or guard an adversarial orchestration must not be able to talk around.

The orchestration is driven here as a hostile move-sequencer: it sets up exactly
the state an adversarial LLM would try to disclose from, then attempts the
disclosure. The gate / guard must fail it closed regardless.
"""
from __future__ import annotations

from gateway.conversation import Conversation
from gateway.conversation_state import (
    AccountState, Fact, FactType, IdentityState, PartContext,
)
from gateway.disclosure_gate import discloseable
from gateway.disclosure_say import render_availability
from gateway.say_guard import assert_no_internal_state, internal_state_tokens
import pytest
from gateway.say_guard import InternalStateLeak

NOW = 1_000_000.0


def _fresh(part, ft, account, now):
    if ft is FactType.PRICE:
        return Fact.read('$187.71', as_of=now, account_id=account.account_id)
    return Fact.read(True, as_of=now)


# 1. Price without account -------------------------------------------------

def test_price_without_account_fails_closed():
    c = Conversation()
    c.add_part('p1'); c.identify_part('p1', 'K5-24SBC')      # identified, NO account
    out = c.read_and_disclose(['p1'], [FactType.PRICE], reader=_fresh, now=NOW)
    assert out.all_blocked and out.blocked[0][2] == 'account'
    assert '187.71' not in out.say


# 2. Inherited disclosability (the multi-part × account attack) ------------

def test_inherited_disclosability_part_C_cannot_ride_part_B():
    c = Conversation()
    c.establish_account('1001')                              # SHARED account
    c.add_part('B'); c.identify_part('B', 'K5-24SBC')        # identified
    c.add_part('C'); c.mark_ambiguous('C', ['X', 'Y'])       # still ambiguous
    out = c.read_and_disclose(['B', 'C'], [FactType.PRICE], reader=_fresh, now=NOW)
    spoken_ctx = {ctx for ctx, _, _ in out.spoken}
    blocked = {ctx: reason for ctx, _, reason in out.blocked}
    assert 'B' in spoken_ctx                                 # B may disclose
    assert blocked.get('C') == 'identity'                   # C must NOT inherit it


# 3. Stale read ------------------------------------------------------------

def test_stale_read_fails_closed_even_via_the_reader():
    c = Conversation()
    c.add_part('p1'); c.identify_part('p1', 'K5-24SBC')

    def stale_reader(part, ft, account, now):
        return Fact.read(True, as_of=now - 10_000)           # read, but ancient
    out = c.read_and_disclose(['p1'], [FactType.AVAILABILITY],
                              reader=stale_reader, now=NOW)
    assert out.all_blocked and out.blocked[0][2] == 'unfresh'   # gate rejects stale


def test_stale_cached_fact_is_reread_not_respoken():
    # a fact cached earlier and now past horizon: read_and_disclose re-reads at NOW
    c = Conversation()
    c.add_part('p1'); c.identify_part('p1', 'K5-24SBC')
    c.state.parts['p1'].availability = Fact.read(True, as_of=NOW - 10_000)  # stale cache
    out = c.read_and_disclose(['p1'], [FactType.AVAILABILITY], reader=_fresh, now=NOW)
    assert out.spoken                                        # disclosed...
    assert c.state.parts['p1'].availability.as_of == NOW     # ...because it was re-read


# 4. Incoherent bundle -----------------------------------------------------

def test_bundle_is_read_together_never_mixed_times():
    c = Conversation()
    c.add_part('A'); c.identify_part('A', 'K5-24SBC')
    c.add_part('B'); c.identify_part('B', 'BH6-36SBC')
    # even if A had an OLD cached read, the bundle re-reads both at NOW -> shared as_of
    c.state.parts['A'].availability = Fact.read(True, as_of=NOW - 10_000)
    c.read_and_disclose(['A', 'B'], [FactType.AVAILABILITY], reader=_fresh, now=NOW)
    assert (c.state.parts['A'].availability.as_of
            == c.state.parts['B'].availability.as_of == NOW)


# 5. Quantity leak ---------------------------------------------------------

def test_quantity_leak_is_blocked_by_the_say_guard():
    # an availability say that tried to include the on-hand count
    with pytest.raises(InternalStateLeak):
        assert_no_internal_state('The K5-24SBC is in stock — 58 on hand.')
    # the orchestration's own renderer never produces one
    say = render_availability([('K5-24SBC', True, None)])
    assert internal_state_tokens(say) == []


# 6. Aggregated multi-part -------------------------------------------------

def test_multipart_is_per_part_explicit_never_aggregated():
    say = render_availability([('K5-24SBC', True, None),
                               ('BH6-36SBC', False, '5 days')])
    assert 'K5-24SBC is in stock' in say
    assert 'BH6-36SBC is not in stock' in say
    for vague in ('mostly', 'most of', 'both available', 'all set', 'they are'):
        assert vague not in say.lower()


# 7. Premature close (and failure-to-close) --------------------------------

def test_disclosure_does_not_close_and_completion_signal_does():
    c = Conversation()
    c.add_part('p1'); c.identify_part('p1', 'K5-24SBC')
    c.read_and_disclose(['p1'], [FactType.AVAILABILITY], reader=_fresh, now=NOW)
    assert c.is_complete is False                            # disclosure never closes (inv 7)
    c.note_completion_signal()
    assert c.is_complete is True                             # only the caller closes


# 8. Wrong-referent quote --------------------------------------------------

def test_ambiguous_reference_disambiguates_never_guesses():
    c = Conversation()
    c.add_part('p1'); c.identify_part('p1', 'K5-24SBC')
    c.add_part('p2'); c.identify_part('p2', 'K5-24SBP')
    # pin focus to a KNOWN third value so "did it guess?" is falsifiable — if
    # resolve_reference silently picked p1 or p2, focus would change off the sentinel.
    c.set_focus('sentinel')
    kind, ids = c.resolve_reference(['p1', 'p2'])            # "the K5" — 2 match
    assert kind == 'disambiguate' and set(ids) == {'p1', 'p2'}
    assert c.state.focus == 'sentinel'                      # NOT moved to a guessed part
    # contrast: a SINGLE match DOES resolve focus (so the above isn't a no-op fn)
    kind1, only = c.resolve_reference(['p1'])
    assert kind1 == 'resolved' and c.state.focus == 'p1'
