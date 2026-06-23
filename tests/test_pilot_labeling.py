"""The pilot labeling boundary, proven against REAL decision points the wired
orchestration emits (not hand-made dicts) — the on-live-path leg of the trust gate.
The contamination guards are the demonstrate-red: a mis-routed label RAISES, so a
QUALITY judgment structurally cannot reach the eval and a RESOLUTION thumbs-up
cannot become a live alias.
"""
from __future__ import annotations

import pytest

from gateway_fixtures import build_gateway
from pilot.decision_point import DecisionPoint
from pilot.labeling import (
    Label, LabelType, Provenance, Question, label_questions, route,
)
from pilot.stores import (
    CorrectionCandidateQueue, EvalCandidatePool, LabelStores, QualityQuarantine,
    WrongLabelType,
)


def _gw(tmp='/tmp/pilot'):
    gw, sessions, _, _ = build_gateway(tmp)
    c = 'c1'
    return gw, c, sessions.open(c, c)


# -- ON LIVE PATH: the decision point comes from a REAL converse turn --------

def test_decision_point_is_built_from_a_real_converse_emission():
    gw, c, tok = _gw()
    resp = gw.converse(c, tok, 'is K5-24SBC in stock?')
    dp = DecisionPoint.from_turn('is K5-24SBC in stock?', resp)
    assert dp.move == 'availability' and dp.resolved_sku == 'K5-24SBC'
    assert dp.disclosed is True and dp.resolution == 'identified'
    assert dp.exercised() == {'resolution', 'availability', 'quality'}


def test_decision_point_captures_a_gated_pricing_turn():
    gw, c, tok = _gw('/tmp/pilot2')
    gw.converse(c, tok, 'is K5-24SBC in stock?')
    resp = gw.converse(c, tok, "what's the price?")        # no account -> gated
    dp = DecisionPoint.from_turn("what's the price?", resp)
    assert dp.move == 'price' and dp.disclosed is False
    assert dp.refused == 'pricing_unauthorized' and dp.account_established is False
    assert 'pricing_gate' in dp.exercised()


# -- NOT-EXERCISED: ask only about decisions that actually occurred ----------

def test_not_exercised_decisions_generate_no_question():
    gw, c, tok = _gw('/tmp/pilot3')
    resp = gw.converse(c, tok, 'is K5-24SBC in stock?')     # availability, NO pricing
    keys = {q.key for q in label_questions(DecisionPoint.from_turn('x', resp))}
    assert 'availability' in keys and 'resolution' in keys
    assert 'pricing_gate' not in keys                      # never asked — not exercised


def test_exercised_pricing_decision_does_generate_its_question():
    gw, c, tok = _gw('/tmp/pilot4')
    gw.converse(c, tok, 'is K5-24SBC in stock?')
    resp = gw.converse(c, tok, "what's the price?")
    keys = {q.key for q in label_questions(DecisionPoint.from_turn('x', resp))}
    assert 'pricing_gate' in keys                          # exercised -> asked


def test_questions_are_typed_for_routing():
    gw, c, tok = _gw('/tmp/pilot5')
    resp = gw.converse(c, tok, 'is K5-24SBC in stock?')
    by_key = {q.key: q.label_type for q in
              label_questions(DecisionPoint.from_turn('x', resp))}
    assert by_key['resolution'] is LabelType.RESOLUTION
    assert by_key['availability'] is LabelType.BEHAVIORAL
    assert by_key['quality'] is LabelType.QUALITY


# -- ROUTING + the contamination guards (demonstrate-red) --------------------

def _label(t, prov=Provenance.CORRECTION):
    return Label(key='k', label_type=t, value=True, provenance=prov)


def test_each_label_type_routes_to_its_own_store():
    s = LabelStores()
    route(_label(LabelType.RESOLUTION), s)
    route(_label(LabelType.BEHAVIORAL), s)
    route(_label(LabelType.QUALITY), s)
    assert len(s.corrections.candidates) == 1
    assert len(s.eval_pool.dev) == 1
    assert len(s.quality.items) == 1


def test_quality_label_cannot_reach_the_eval():
    # the headline contamination guard: a "tone was off" judgment must NOT enter the
    # behavioral eval. Routed there directly, it RAISES.
    with pytest.raises(WrongLabelType):
        EvalCandidatePool().add_candidate(_label(LabelType.QUALITY))


def test_behavioral_label_cannot_become_a_correction_alias():
    with pytest.raises(WrongLabelType):
        CorrectionCandidateQueue().add_candidate(_label(LabelType.BEHAVIORAL))


def test_resolution_label_cannot_land_in_quality_quarantine():
    with pytest.raises(WrongLabelType):
        QualityQuarantine().add(_label(LabelType.RESOLUTION))


# -- STRUCTURAL isolation: the dev pool has no path to the frozen sets -------

def test_eval_pool_offers_no_frozen_or_holdout_writer():
    # a labeled (human-seen) call must not auto-populate the gate; promotion is a
    # separate curated act, so the pool exposes NO method to write frozen/holdout.
    pool = EvalCandidatePool()
    names = dir(pool)
    assert not any('frozen' in n or 'holdout' in n or 'promote' in n for n in names)


def test_no_cross_store_transfer_method_exists():
    s = LabelStores()
    names = dir(s)
    assert not any('transfer' in n or 'promote' in n or 'feed' in n for n in names)


# -- CONFIDENCE weighting: the labeler is a noisy instrument -----------------

def test_label_weight_reflects_provenance():
    acq = _label(LabelType.RESOLUTION, Provenance.ACQUIESCENCE).weight
    cor = _label(LabelType.RESOLUTION, Provenance.CORRECTION).weight
    gold = _label(LabelType.RESOLUTION, Provenance.GOLD).weight
    assert acq < cor < gold                                # thumbs-up << correction << reality
