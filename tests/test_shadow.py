"""Shadow / listen-in onboarding mode — observe real calls, map capability,
close the HITL correction loop, capture anonymized service-improvement data.

All tests use the GATED pipeline: propose_correction → label → battery →
human release → ACTIVE. No test recreates the deleted instant-add path.
"""
from __future__ import annotations

import pytest
from gateway_fixtures import _shared_catalog

from gateway import (
    CapabilityMap,
    CorrectionStore,
    ShadowCampaign,
    ShadowObserver,
    looks_part_like,
)
from gateway.alias_store import ACTIVE, PROPOSED
from learning.eval_battery import Verdict
from observability import ImprovementLog, anon_key
from resolution import ResolutionService
from sku_translator import InMemoryStore

REAL_SKU = 'K5-24SBC'
FAIL = 'do you stock the qq9zz adapter'   # part-like token, not in catalog
CHITCHAT = 'hi thanks for calling, how are you today'


def _gate_and_release(corr, phrase, sku, *, source='rep_label', now=1000.0):
    """Full gated promotion: propose → bump confidence → battery → release.
    Used by test fixtures to get an alias to ACTIVE through the one true path."""
    from gateway.alias_store import on_confirm
    a = corr.propose_correction(phrase, sku, source=source, now=now)
    # Bump confidence to >= auto_resolve (0.70). c0=0.30, rep_label=+0.25 already
    # applied in propose_correction, so confidence is 0.55. Need one more confirm.
    on_confirm(a, 'order_not_returned', now=now)  # +0.40 -> 0.95
    assert corr.clear_for_release(phrase, verdict=Verdict.injected_pass())
    corr.release(phrase)
    assert a.state == ACTIVE
    return a


def _svc():
    cat, ver = _shared_catalog()
    return ResolutionService(cat, InMemoryStore(), catalog_version=ver), cat


# --- observe-only attempts ------------------------------------------------------

def test_part_like_heuristic():
    assert looks_part_like(FAIL) and looks_part_like('5 inch chrome stack')
    assert not looks_part_like(CHITCHAT) and not looks_part_like('yes please')


def test_observe_call_classifies_attempts():
    svc, _ = _svc()
    obs = ShadowObserver(svc)
    atts = obs.observe_call([
        ('rep', 'parts desk'), ('customer', CHITCHAT),
        ('customer', f'i need {REAL_SKU}'), ('customer', FAIL)])
    by = {a.utterance: a for a in atts}
    assert by[CHITCHAT].outcome == 'not_a_part'        # chit-chat not a failure
    assert by[f'i need {REAL_SKU}'].outcome == 'success'
    assert by[f'i need {REAL_SKU}'].sku == REAL_SKU
    assert by[FAIL].outcome in ('no_match', 'ambiguous')   # a real failure point


# --- capability map -------------------------------------------------------------

def test_capability_map_ranks_failures():
    svc, _ = _svc()
    atts = ShadowObserver(svc).observe_call([
        ('customer', f'need {REAL_SKU}'), ('customer', FAIL), ('customer', CHITCHAT)])
    cap = CapabilityMap.from_attempts(atts)
    assert cap.attempted == 2 and cap.succeeded == 1     # chit-chat excluded
    assert cap.success_rate == 0.5
    assert cap.failure_points and cap.failure_points[0].count >= 1


# --- multiple calls over a (configurable / continuous) window -------------------

def test_campaign_aggregates_many_calls_and_window_config():
    svc, _ = _svc()
    camp = ShadowCampaign(ShadowObserver(svc), window_days=7)
    for _ in range(3):                                   # three observed calls
        camp.observe_call([('customer', f'need {REAL_SKU}'), ('customer', FAIL)])
    assert camp.calls == 3
    cap = camp.capability_map()
    # the recurring failure shows up once per call -> aggregated count == 3
    assert cap.attempted == 6 and cap.succeeded == 3
    assert cap.failure_points[0].count == 3            # recurs across all 3 calls
    # window config
    assert camp.continuous is False
    assert camp.window_open(3) and not camp.window_open(10)
    assert ShadowCampaign(ShadowObserver(svc)).continuous is True   # default = continuous


# --- HITL correction loop (the point of the exercise) ---------------------------

def test_correction_closes_the_loop_and_is_never_invent():
    svc, cat = _svc()
    pre = ShadowObserver(svc).observe('customer', FAIL)
    assert pre.outcome != 'success'                      # fails before correction

    corr = CorrectionStore(cat)
    _gate_and_release(corr, 'qq9zz adapter', REAL_SKU)   # full gated pipeline
    post = ShadowObserver(svc, corrections=corr).observe('customer', FAIL)
    assert post.outcome == 'success' and post.sku == REAL_SKU
    assert post.source == 'learned_correction'           # loop closed

    # never-invent: a correction may only target a real catalog SKU
    with pytest.raises(ValueError):
        corr.propose_correction('whatever', 'TOTALLY-BOGUS-SKU-XYZ')

    # graceful-degradation correction is recorded for its failure category
    corr.set_degradation('no_match', 'offer to transfer to a parts specialist')
    assert corr.degradation_for('no_match') == 'offer to transfer to a parts specialist'


# --- self-healing: learn from how the human rep resolved the inquiry ------------

def test_self_heal_from_rep_said_sku_autonomous_proposes_not_live():
    """Autonomous ride-along PROPOSES but the alias is NOT live until gated +
    human-released (invariant: autonomous cannot auto-release, §7)."""
    svc, cat = _svc()
    corr = CorrectionStore(cat)
    obs = ShadowObserver(svc, catalog=cat, corrections=corr)
    # customer fails; the rep then states the real SKU.
    call = [('customer', FAIL),
            ('rep', f'no problem, that one is {REAL_SKU}, want it shipped?')]
    attempts, heals = obs.observe_call_with_healing(call, autonomous=True)
    assert attempts[0].outcome != 'success'              # tool missed it
    assert heals and heals[0].source == 'rep_said_sku'
    assert heals[0].healed_sku == REAL_SKU and heals[0].applied is True
    # proposed but NOT live — alias_for returns None for non-ACTIVE
    a = corr.get_alias(FAIL)
    assert a is not None and a.state == PROPOSED
    assert corr.alias_for(FAIL) is None                  # gate holds
    # the same failure still fails (not live yet)
    again = obs.observe('customer', FAIL)
    assert again.outcome != 'success'


def test_self_heal_proposed_not_applied_when_not_autonomous():
    # Default (human-gated): the heal is PROPOSED, not auto-applied.
    svc, cat = _svc()
    corr = CorrectionStore(cat)
    obs = ShadowObserver(svc, catalog=cat, corrections=corr)
    call = [('customer', FAIL), ('rep', f'that is {REAL_SKU}')]
    _, heals = obs.observe_call_with_healing(call, autonomous=False)
    assert heals and heals[0].healed_sku == REAL_SKU
    assert heals[0].applied is False                     # awaits the HITL gate
    assert obs.observe('customer', FAIL).outcome != 'success'   # not learned yet


def test_self_heal_never_invents():
    svc, cat = _svc()
    obs = ShadowObserver(svc, catalog=cat)
    # rep says a bogus, non-catalog code -> no heal harvested
    call = [('customer', FAIL), ('rep', 'oh that is BOGUS-9999-XX, easy')]
    _, heals = obs.observe_call_with_healing(call, autonomous=True)
    assert all(h.healed_sku != 'BOGUS-9999-XX' for h in heals)


def test_campaign_accumulates_self_heals():
    svc, cat = _svc()
    corr = CorrectionStore(cat)
    camp = ShadowCampaign(ShadowObserver(svc, catalog=cat, corrections=corr))
    for _ in range(2):
        camp.observe_call([('customer', FAIL),
                           ('rep', f'that is {REAL_SKU}')], heal=True,
                          autonomous=True)
    assert len(camp.heals) >= 1 and camp.heals[0].healed_sku == REAL_SKU
    # autonomous heals are PROPOSED, not ACTIVE (gate holds)
    a = corr.get_alias(FAIL)
    assert a is not None and a.state == PROPOSED


# --- always-on continuous self-improvement (3 sources + periodic HITL) ----------

def _ci(review_every=1):
    from gateway import ContinuousImprovement
    svc, cat = _svc()
    corr = CorrectionStore(cat)
    obs = ShadowObserver(svc, catalog=cat, corrections=corr)
    return ContinuousImprovement(obs, corr, review_every=review_every), cat, corr, svc


def test_source1_training_ride_along_proposes_not_live():
    """Ride-along auto-heals PROPOSE but the alias stays gated — not live
    until battery + human release (§7: autonomous cannot auto-release)."""
    ci, cat, corr, svc = _ci()
    ci.ingest_call([('customer', FAIL), ('rep', f'that is {REAL_SKU}')])
    assert any(h.applied and h.origin == 'shadow' for h in ci.auto_applied)
    # proposed but NOT live
    a = corr.get_alias(FAIL)
    assert a is not None and a.state == PROPOSED
    assert corr.alias_for(FAIL) is None              # gate holds


def test_source2_post_handoff_learning_proposes_not_live():
    """Post-handoff strong heal PROPOSES; not live without gate + release."""
    ci, cat, corr, svc = _ci()
    # agent degraded + transferred; the human then states the real SKU
    heal = ci.ingest_handoff(FAIL, ['let me grab that', f'it is {REAL_SKU}'])
    assert heal and heal.origin == 'post_handoff' and heal.applied is True
    # proposed but NOT live
    a = corr.get_alias(FAIL)
    assert a is not None and a.state == PROPOSED
    assert corr.alias_for(FAIL) is None              # gate holds


def test_source3_self_monitoring_flags_opportunities_no_autoapply():
    ci, cat, corr, svc = _ci()
    opps = ci.ingest_self_monitored_call(
        [('customer', FAIL), ('customer', f'need {REAL_SKU}'), ('customer', 'hi')])
    assert opps and opps[0].origin == 'self_monitor'
    assert opps[0].outcome in ('no_match', 'ambiguous')
    assert corr.alias_for(FAIL) is None              # nothing auto-applied


def test_periodic_review_cadence_and_apply():
    ci, cat, corr, svc = _ci(review_every=1)
    # a restatement heal (descriptive, no bare SKU token) -> PENDING, not auto
    heal = ci.ingest_handoff(FAIL, ['5 inch chrome curved 24 long SB'])
    assert heal and heal.source == 'rep_restatement' and not heal.applied
    assert ci.review_due()
    batch = ci.pending_review()
    assert batch.proposals and cat.is_canonical(batch.proposals[0].healed_sku)
    applied = ci.apply_review(batch.proposals)        # SME confirms -> PROPOSED
    assert applied == 1
    # apply_review now PROPOSES (gated), not instant-live
    a = corr.get_alias(FAIL)
    assert a is not None and a.state == PROPOSED
    assert corr.alias_for(FAIL) is None               # still gated
    assert ci.pending_review().proposals == ()        # queue cleared


def test_continuous_improvement_alerts_when_review_due(tmp_path):
    from gateway import ContinuousImprovement
    from observability import AlertRouter
    svc, cat = _svc()
    corr = CorrectionStore(cat)
    alerts = AlertRouter(tmp_path / 'alerts.jsonl')   # file sink, no webhook
    ci = ContinuousImprovement(
        ShadowObserver(svc, catalog=cat, corrections=corr), corr,
        review_every=1, alerts=alerts, now_iso=lambda: 'T')
    ci.ingest_self_monitored_call([('customer', FAIL)])   # one opportunity -> due
    rows = (tmp_path / 'alerts.jsonl').read_text().strip().splitlines()
    assert rows and 'HITL review due' in rows[0]


def test_self_monitor_opportunities_go_to_review_batch():
    ci, cat, corr, svc = _ci(review_every=1)
    ci.ingest_self_monitored_call([('customer', FAIL)])
    assert ci.review_due()
    assert ci.pending_review().opportunities


# --- learned corrections reach the LIVE resolver (MISS 1) + persist (MISS 2) ----

def test_learned_alias_changes_the_live_resolver():
    """A gated alias (propose → battery → release) reaches the live resolver
    with source='learned_alias'."""
    from resolution import ResolutionService
    from sku_translator import InMemoryStore
    cat, ver = _shared_catalog()
    corr = CorrectionStore(cat)
    svc = ResolutionService(cat, InMemoryStore(), catalog_version=ver,
                            learned_aliases=corr)
    assert svc.resolve(FAIL).state != 'resolved'         # before learning
    _gate_and_release(corr, FAIL, REAL_SKU)               # full gated pipeline
    r = svc.resolve(FAIL)
    assert r.state == 'resolved' and r.sku == REAL_SKU and r.source == 'learned_alias'


def test_auto_confirm_alias_resolves_with_readback():
    """POSITIVE PROOF: an ACTIVE auto_confirm alias → source='learned_alias',
    confidence='medium', needs_review=True → readback. The confirm-on-alias path
    where baseline resolved silently (DELTA A)."""
    from gateway.alias_store import on_confirm
    from gateway.alias_store import resolution_mode as rm
    from resolution import ResolutionService
    from sku_translator import InMemoryStore
    cat, ver = _shared_catalog()
    corr = CorrectionStore(cat)
    # Propose with rep_label: c0=0.30 + 0.25 = 0.55
    a = corr.propose_correction(FAIL, REAL_SKU, source='rep_label', now=1000.0)
    # Add a caller_disambiguation: 0.55 + 0.15 = 0.70 (auto_resolve threshold)
    on_confirm(a, 'caller_disambiguation', now=1000.0)
    assert corr.clear_for_release(FAIL, verdict=Verdict.injected_pass())
    corr.release(FAIL)
    assert rm(a) == 'auto_confirm'                        # NOT auto_silent
    svc = ResolutionService(cat, InMemoryStore(), catalog_version=ver,
                            learned_aliases=corr)
    r = svc.resolve(FAIL)
    assert r.state == 'resolved' and r.sku == REAL_SKU
    assert r.source == 'learned_alias'
    assert r.confidence == 'medium'                       # NOT high
    assert r.needs_review is True                         # readback required
    assert 'needs_readback' in r.flags                    # flag present


def test_shared_store_makes_self_heal_reach_production_after_gate():
    """The real loop: ONE store shared by the shadow observer and the live svc.
    Autonomous ride-along proposes; only after battery+release does it resolve."""
    from gateway.alias_store import on_confirm
    from resolution import ResolutionService
    from sku_translator import InMemoryStore
    cat, ver = _shared_catalog()
    corr = CorrectionStore(cat)
    svc = ResolutionService(cat, InMemoryStore(), catalog_version=ver,
                            learned_aliases=corr)
    obs = ShadowObserver(svc, catalog=cat, corrections=corr)
    obs.observe_call_with_healing(
        [('customer', FAIL), ('rep', f'that is {REAL_SKU}')], autonomous=True)
    # autonomous ride-along PROPOSES but the alias is NOT live
    assert svc.resolve(FAIL).state != 'resolved'
    a = corr.get_alias(FAIL)
    assert a is not None and a.state == PROPOSED
    # now gate it through: bump confidence, battery, human release
    on_confirm(a, 'order_not_returned', now=2000.0)
    assert corr.clear_for_release(FAIL, verdict=Verdict.injected_pass())
    corr.release(FAIL)
    assert svc.resolve(FAIL).source == 'learned_alias'


def test_corrections_persist_and_revalidate_on_load(tmp_path):
    cat = _shared_catalog()[0]
    p = tmp_path / 'corr.json'
    store = CorrectionStore(cat, path=p)
    _gate_and_release(store, FAIL, REAL_SKU)
    result = CorrectionStore(cat, path=p).alias_for(FAIL)
    assert result is not None and result[0] == REAL_SKU   # survived (ACTIVE)
    # never-invent survives a hand-edited file: a bogus target is dropped on load
    p.write_text('{"aliases": {"x": "BOGUS-NOPE-SKU"}, "degradations": {}}')
    assert CorrectionStore(cat, path=p).alias_for('x') is None
    # legacy plain-string format is silently skipped
    p.write_text('{"aliases": {"x": "K5-24SBC"}, "degradations": {}}')
    assert CorrectionStore(cat, path=p).alias_for('x') is None  # string, not Alias


# --- live-audio bridge (dual-channel call -> transcript -> loop) ----------------

def test_shadow_stream_bridge_ride_along_self_heals_from_audio():
    import base64
    import json

    from gateway import (
        ContinuousImprovement,
        ShadowObserver,
        ShadowStreamBridge,
        SimulatedStreamingASR,
        Transcript,
    )
    svc, cat = _svc()
    corr = CorrectionStore(cat)
    ci = ContinuousImprovement(
        ShadowObserver(svc, catalog=cat, corrections=corr), corr, review_every=99)
    scripts = {
        'inbound': [Transcript(text=FAIL, confidence=0.9)],
        'outbound': [Transcript(text=f'that is {REAL_SKU}', confidence=0.9)],
    }
    bridge = ShadowStreamBridge(
        lambda track: SimulatedStreamingASR(
            script=scripts[track], bytes_per_turn=160).open(
                sample_rate=8000, encoding='pcm_mulaw'),
        ci)

    def media(track, seq):
        return json.dumps({'event': 'media', 'streamSid': 'MZ1',
                           'sequenceNumber': str(seq),
                           'media': {'track': track,
                                     'payload': base64.b64encode(b'\xff' * 160).decode()}})

    bridge.feed(json.dumps({'event': 'start',
                            'start': {'callSid': 'CA1', 'streamSid': 'MZ1'}}))
    bridge.feed(media('inbound', 1))      # customer: the miss
    bridge.feed(media('outbound', 2))     # rep: the resolution
    turns = bridge.finish()
    assert ('customer', FAIL) in turns and ('rep', f'that is {REAL_SKU}') in turns
    # the rep's resolution was harvested from AUDIO and PROPOSED (gated, not live)
    a = corr.get_alias(FAIL)
    assert a is not None and a.state == PROPOSED
    assert corr.alias_for(FAIL) is None              # gate holds


def test_continuous_improvement_persists_review_queue(tmp_path):
    from gateway import ContinuousImprovement
    svc, cat = _svc()
    corr = CorrectionStore(cat)
    sp = tmp_path / 'imp_state.json'
    ci = ContinuousImprovement(
        ShadowObserver(svc, catalog=cat, corrections=corr), corr,
        review_every=99, state_path=sp)
    ci.ingest_self_monitored_call([('customer', FAIL)])
    assert ci.pending_review().opportunities
    # a fresh instance restores the pending review queue from disk
    ci2 = ContinuousImprovement(
        ShadowObserver(svc, catalog=cat, corrections=corr), corr,
        review_every=99, state_path=sp)
    assert ci2.pending_review().opportunities


# --- anonymized service-improvement capture -------------------------------------

def test_improvement_log_scrubs_and_anonymizes(tmp_path):
    svc, _ = _svc()
    log = ImprovementLog(tenant='acme-corp', now_iso=lambda: 'T')
    obs = ShadowObserver(svc, log=log)
    obs.observe('customer', f'my number is 5550100100 i need {REAL_SKU}')
    row = log.rows[-1]
    assert '5550100100' not in row['utterance'] and '[redacted]' in row['utterance']
    assert row['tenant'] == anon_key('acme-corp') and row['tenant'] != 'acme-corp'

    log.record_answer(correlation_id='c1', account='ACCT-123', sku=REAL_SKU,
                      answer_kind='availability')
    ans = log.rows[-1]
    assert ans['account'] == anon_key('ACCT-123') and ans['correlation_id'] == 'c1'
