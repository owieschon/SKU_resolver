"""Shadow replay: re-grounding + per-stream divergence + autonomous-vs-conditional
resolution tagging, driven through the LIVE agent (gw.converse). Two real-shaped
calls exercise the detector; the trust gate is green + on-live-path (the agent's
moves come from real converse) + demonstrate-red (the conditional tag flips if the
behavioral divergence is removed; the account is established from RAW while the
persisted transcript is scrubbed).
"""
from __future__ import annotations

from gateway_fixtures import build_gateway
from pilot.capture import CallTurn, HumanMove, RawCall
from pilot.shadow import (
    DivergenceMarker, ingest, replay_with_regrounding, tag_decisions,
)


def _gw(tmp='/tmp/shadow'):
    gw, *_ = build_gateway(tmp)
    return gw


# Call A: resolution stays aligned; behavioral stream DIVERGES at T2 (the agent
# would transfer to a person, the rep de-escalates and keeps helping). T3's price
# resolution is correct but reached AFTER the behavioral split -> CONDITIONAL.
CALL_A = RawCall('callA', (
    CallTurn('I need a K5-24SBC, is it in stock?', 'It is, ships tomorrow.',
             HumanMove(resolved_sku='K5-24SBC', branch='disclose_availability')),
    CallTurn('my account number is 1001', "You're verified.",
             HumanMove(branch='establish_account', established_account='1001')),
    CallTurn('can you just transfer me to a person', 'I can help with that here —',
             HumanMove(resolved_sku='K5-24SBC', branch='disclose_availability')),
    CallTurn("ok so what's the price", 'For your account it is $187.71.',
             HumanMove(resolved_sku='K5-24SBC', branch='disclose_price')),
))

# Call B: the rep knew the part was K5-24SBC; the agent can only get candidates from
# "a chrome stack" -> RESOLUTION divergence at T0 (catalog-checkable: human right).
CALL_B = RawCall('callB', (
    CallTurn("I'm looking for a chrome stack", 'The K5-24SBC, that one is in stock.',
             HumanMove(resolved_sku='K5-24SBC', branch='disclose_availability')),
))


def test_behavioral_divergence_lands_where_a_human_would():
    gw = _gw('/tmp/shadowA')
    comps, marker = replay_with_regrounding(CALL_A, gw, caller_id='A')
    # T0 availability aligned, T1 account aligned, T2 the agent escalates vs continue
    assert comps[2].agent_branch == 'escalate' and comps[2].human_branch == 'disclose_availability'
    assert marker.behavioral == 2                          # the split is at T2
    assert marker.resolution is None                       # resolution never diverged


def test_post_behavioral_divergence_resolution_is_CONDITIONAL_not_autonomous():
    # the load-bearing refinement: T3 resolves the price to the right part, but
    # AFTER the behavioral divergence -> conditional (capability), not autonomous.
    gw = _gw('/tmp/shadowA2')
    comps, marker = replay_with_regrounding(CALL_A, gw, caller_id='A2')
    tagged = {t.index: t for t in tag_decisions(comps, marker)}
    assert tagged[0].resolution_class == 'autonomous'      # pre-divergence
    assert tagged[3].resolution_class == 'conditional'     # post-behavioral-divergence
    assert 'resolution:conditional' in tagged[3].tags
    # demonstrate-red: if a rep adjudicates AWAY the behavioral divergence, T3's
    # resolution re-derives to autonomous — proving the tag keys on the marker.
    retag = {t.index: t for t in tag_decisions(comps, marker.adjudicate(behavioral=None))}
    assert retag[3].resolution_class == 'autonomous'


def test_behavioral_labels_after_divergence_carry_the_self_comparison_bias():
    gw = _gw('/tmp/shadowA3')
    comps, marker = replay_with_regrounding(CALL_A, gw, caller_id='A3')
    tagged = {t.index: t for t in tag_decisions(comps, marker)}
    assert 'rep_self_comparison' not in tagged[0].tags      # pre-divergence: clean
    assert 'rep_self_comparison' in tagged[2].tags          # at divergence: biased
    assert 'rep_self_comparison' in tagged[3].tags          # post: biased


def test_resolution_divergence_is_detected_and_catalog_checkable():
    gw = _gw('/tmp/shadowB')
    comps, marker = replay_with_regrounding(CALL_B, gw, caller_id='B')
    assert marker.resolution == 0                          # agent couldn't resolve what the rep did
    assert comps[0].human_resolved == 'K5-24SBC'           # the exogenous ground truth
    assert comps[0].agent_resolved != 'K5-24SBC'           # agent diverged (ambiguous/other)


def test_replay_runs_on_RAW_but_only_SCRUBBED_persists():
    # filter-on-raw-store-scrubbed: the account number 1001 must drive the re-ground
    # (so T3 can price), yet must NOT survive in the persisted transcript.
    gw = _gw('/tmp/shadowScrub')
    art = ingest(CALL_A, gw, caller_id='S')
    # the re-ground used the RAW account number: T3 priced -> behavioral aligned there
    t3 = next(c for c in [d.comparison for d in art.decisions] if c.index == 3)
    assert t3.agent_branch == 'disclose_price' and t3.human_branch == 'disclose_price'
    # but the persisted transcript has the account number scrubbed
    joined = ' '.join(c + ' ' + r for c, r in art.scrubbed.turns)
    assert '1001' not in joined and '[ACCOUNT]' in joined


def test_rep_can_adjudicate_the_divergence_LOCATION():
    # the detector PROPOSES the location; a misfire mistags everything downstream,
    # so the rep can move it and the tags re-derive.
    gw = _gw('/tmp/shadowAdj')
    comps, marker = replay_with_regrounding(CALL_A, gw, caller_id='Adj')
    moved = marker.adjudicate(behavioral=3)                # rep says the split is really at T3
    tagged = {t.index: t for t in tag_decisions(comps, moved)}
    assert tagged[2].behavioral_class == 'pre_divergence'  # T2 now pre-divergence
    assert tagged[3].behavioral_class == 'at_divergence'
