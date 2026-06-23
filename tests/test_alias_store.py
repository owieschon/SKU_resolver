"""The correction store is the only thing the self-improvement loop may mutate, so
every transition is pinned here — especially the stale-vs-retired split (Bug 2,
most likely to regress) and the structural guarantees that make the bad states
unrepresentable.
"""
from __future__ import annotations

import dataclasses

import pytest

from gateway.alias_store import (
    ACTIVE,
    AWAITING_RELEASE,
    CONTESTED,
    DORMANT,
    PROPOSED,
    RETIRED,
    STALE,
    AliasParams,
    mark_contested,
    may_promote,
    on_catalog_churn,
    on_confirm,
    on_contradicting_correction,
    on_disuse_window,
    on_failed_live_lookup,
    on_hit,
    propose,
    release,
    resolution_mode,
    stage_for_release,
    wallclock_floor_sweep,
)

P = AliasParams()


def _proposed(c=None):
    a = propose('the 5 by 24 chrome', 'K5-24SBC', now=1000.0)
    if c is not None:
        a.confidence = c
    return a


# -- structural: bad states are unrepresentable ------------------------------

def test_alias_value_is_a_part_number_with_no_price_field():
    a = _proposed()
    fields = {f.name for f in dataclasses.fields(a)}
    assert 'target_sku' in fields
    assert not (fields & {'price', 'unit_price', 'availability', 'lead_time'})
    with pytest.raises(TypeError):                 # nowhere to put a fabricated price
        dataclasses.replace(a, price=187.71)


def test_birth_is_inert():
    a = _proposed()
    assert a.state == PROPOSED and a.confidence == P.c0
    assert resolution_mode(a) == 'disambiguate'    # proposed never resolves


# -- confidence: only reality raises it, weighted by tier --------------------

def test_hit_does_not_raise_confidence():
    a = _proposed(); before = a.confidence
    on_hit(a, 5)
    assert a.confidence == before and a.last_hit_call == 5     # W_HIT = 0

def test_strong_vs_weak_confirm_weights():
    a = _proposed(); on_confirm(a, 'order_not_returned', now=2000.0)
    assert a.confidence == pytest.approx(0.30 + 0.40) and a.strong_labels == 1
    b = _proposed(); on_confirm(b, 'caller_acquiescence', now=2000.0)
    assert b.confidence == pytest.approx(0.30 + 0.05) and b.strong_labels == 0  # Bug 1
    c = _proposed(); on_confirm(c, 'caller_disambiguation', now=2000.0)
    assert c.confidence == pytest.approx(0.30 + 0.15)          # choice > acquiescence


# -- Bug 1: an all-caller path can never reach silent trust ------------------

def test_all_caller_path_never_silent():
    a = _proposed(); promote_active(a)
    for _ in range(20):                            # pile on cheap caller yeses
        on_confirm(a, 'caller_acquiescence', now=3000.0)
        on_confirm(a, 'caller_disambiguation', now=3000.0)
    assert a.confidence >= P.trust                 # confidence is high...
    assert a.strong_labels == 0
    assert resolution_mode(a) == 'auto_confirm'    # ...but NEVER auto_silent

def test_one_strong_label_unlocks_silent_when_confident():
    a = _proposed(); promote_active(a)
    on_confirm(a, 'order_not_returned', now=3000.0)
    on_confirm(a, 'order_not_returned', now=3000.0)   # 0.30+0.40+0.40 -> 1.0, 2 strong
    assert resolution_mode(a) == 'auto_silent'


def promote_active(a):
    a.state = ACTIVE


# -- decay: usage-window + wall-clock floor ----------------------------------

def test_disuse_decays_then_goes_dormant():
    a = _proposed(0.25)
    on_disuse_window(a)                            # 0.25*0.9 = 0.225, still > 0.20
    assert a.state == PROPOSED
    on_disuse_window(a)                            # 0.2025 -> below dormancy next
    on_disuse_window(a)
    assert a.state == DORMANT

def test_wallclock_floor_sweeps_unearned_proposals_only():
    old = _proposed()                              # created_at=1000, no labels
    wallclock_floor_sweep(old, now=1000.0 + P.wallclock_floor_secs + 1)
    assert old.state == DORMANT
    earned = _proposed(); on_confirm(earned, 'rep_label', now=1000.0)
    wallclock_floor_sweep(earned, now=1000.0 + P.wallclock_floor_secs + 1)
    assert earned.state == PROPOSED               # earned a label -> not swept


# -- Bug 2: stale-vs-retired split (the one most likely to regress) ----------

def test_failed_live_lookup_goes_stale_first_then_retires_after_K():
    a = _proposed(); promote_active(a)
    on_failed_live_lookup(a)
    assert a.state == STALE and a.confidence > 0   # transient: not retired yet
    on_failed_live_lookup(a)
    assert a.state == STALE
    on_failed_live_lookup(a)                       # K=3rd failure
    assert a.state == RETIRED and a.confidence == 0.0

def test_a_strong_signal_resets_failed_lookup_count():
    a = _proposed(); promote_active(a)
    on_failed_live_lookup(a); on_failed_live_lookup(a)
    on_confirm(a, 'order_not_returned', now=4000.0)   # reached the world, it agreed
    assert a.failed_lookups == 0
    on_failed_live_lookup(a)
    assert a.state == STALE                        # count reset -> not retired

def test_contradicting_correction_retires_immediately():
    a = _proposed(); promote_active(a)
    on_contradicting_correction(a)                 # human says it maps elsewhere
    assert a.state == RETIRED and a.confidence == 0.0

def test_catalog_churn_is_stale_not_retired():
    a = _proposed(); promote_active(a)
    on_catalog_churn(a)
    assert a.state == STALE


# -- contested clamps confidence (defense in depth) --------------------------

def test_contested_demotes_and_clamps_confidence():
    a = _proposed(0.95); promote_active(a)
    mark_contested(a, 'K5-26SBC')
    assert a.state == CONTESTED
    assert a.confidence < P.auto_resolve           # clamp, not just state
    assert resolution_mode(a) == 'disambiguate'    # ask A or B


# -- promotion gate: positive label AND no-regress ---------------------------

def test_promotion_requires_label_and_passing_battery():
    # the §4 battery's Verdict replaces the old bool INSIDE may_promote. This legacy
    # transition test hands in an INJECTED verdict (a visible test fixture, NOT a
    # battery stub on the production path) so it keeps testing the GATE LOGIC — label
    # AND eval both required. The battery's own correctness is proven separately in
    # tests/test_eval_battery.py (the three injected-bad-rule reds).
    from learning.eval_battery import Verdict
    a = _proposed(0.85)                            # confident but unlabeled
    assert may_promote(a, verdict=Verdict.injected_pass()) is False   # no label -> no
    on_confirm(a, 'rep_label', now=5000.0)
    assert may_promote(a, verdict=Verdict.injected_fail()) is False   # battery fails -> no
    assert may_promote(a, verdict=Verdict.injected_pass()) is True    # both halves -> yes


def test_release_is_the_only_path_to_active_and_requires_a_human_step():
    # invariant 4b structural: clearing the battery STAGES (awaiting_release, NOT
    # live); only an explicit human release() reaches ACTIVE.
    a = _proposed(0.85)
    on_confirm(a, 'rep_label', now=5000.0)
    stage_for_release(a)                               # battery cleared
    assert a.state == AWAITING_RELEASE
    assert resolution_mode(a) == 'disambiguate'       # NOT live while awaiting release
    release(a)                                         # the human confirm
    assert a.state == ACTIVE


def test_cannot_release_an_unstaged_alias():
    a = _proposed(0.85)                                # never cleared the battery
    with pytest.raises(ValueError):
        release(a)                                     # no PROPOSED -> ACTIVE shortcut


# -- §6 confidence floor at the gate: single SME label is NOT enough ----------

def test_confidence_floor_single_sme_label_blocked_at_gate():
    """§6: 0.55 (c0=0.30 + rep_label=0.25) with a PASSING battery verdict
    does NOT reach ACTIVE via may_promote. The floor gates at the promotion
    step, not as arithmetic coincidence."""
    from learning.eval_battery import Verdict
    a = _proposed()                                       # c0 = 0.30
    on_confirm(a, 'rep_label', now=5000.0)                # +0.25 -> 0.55
    assert a.confidence == pytest.approx(0.55)
    assert a.exogenous_labels == 1
    # battery passes, but confidence < auto_resolve (0.70) -> BLOCKED
    assert may_promote(a, verdict=Verdict.injected_pass()) is False
    assert a.state == PROPOSED                            # never left proposed
    # now add a second strong label: 0.55 + 0.40 = 0.95 -> clears
    on_confirm(a, 'order_not_returned', now=6000.0)
    assert a.confidence >= P.auto_resolve
    assert may_promote(a, verdict=Verdict.injected_pass()) is True


# -- §7 autonomous-cannot-auto-release: strong-heal proposes, no release ------

def test_autonomous_strong_heal_cannot_auto_release_via_correction_store():
    """§7: A strong-heal (rep_said_sku) through propose_correction lands as
    PROPOSED with confidence 0.55 — NOT ACTIVE. The CorrectionStore has no
    path to auto-release. Human release() is the only way."""
    from gateway_fixtures import _shared_catalog

    from gateway.shadow import CorrectionStore
    cat, _ = _shared_catalog()
    corr = CorrectionStore(cat)
    a = corr.propose_correction('qq9zz adapter', 'K5-24SBC',
                                source='rep_label', now=1000.0)
    assert a.state == PROPOSED
    assert a.confidence == pytest.approx(0.55)            # c0 + rep_label
    assert corr.alias_for('qq9zz adapter') is None        # NOT live
    # even with a passing battery, single label + 0.55 → blocked
    from learning.eval_battery import Verdict
    assert corr.clear_for_release('qq9zz adapter',
                                  verdict=Verdict.injected_pass()) is False


# -- resolution_mode: active-but-decayed falls back to confirm/ask -----------

def test_active_decayed_below_auto_resolve_disambiguates():
    a = _proposed(0.5); promote_active(a)
    on_confirm(a, 'rep_label', now=6000.0)         # 0.5+0.25=0.75 -> auto_confirm
    assert resolution_mode(a) == 'auto_confirm'
    for _ in range(5):
        on_disuse_window(a)                        # decay below auto_resolve
    assert resolution_mode(a) in ('disambiguate', )    # explicit re-confirm path
