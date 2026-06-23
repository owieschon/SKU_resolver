"""The rule-release battery's fault-injection check (dispatch §4.4): a known-bad rule
of EACH failure class is shown to FAIL the component that owns it, and a good rule
PASSES. An eval that has only ever passed rules is unproven; these reds are the proof
the battery tests what it claims. The subclass red is built as a DELTA — it passes the
2-component battery and fails the 3-component — proving 4.3 is critical, not
decorative.
"""
from __future__ import annotations

from learning.eval_battery import (
    CandidateRule,
    HeldOutCase,
    evaluate,
    matches,
)


def _ho(phrase, sku, fam, baseline=None):
    return HeldOutCase(phrase=phrase, true_sku=sku, family=fam, baseline_sku=baseline)


# -- GREEN: a good rule clears all three -------------------------------------

def test_good_rule_passes_the_full_battery():
    # one part, many vernaculars -> the lexicon the loop is meant to learn
    holdout = [
        _ho('the chrome stack', 'K5-24SBC', 'K'),
        _ho('a shiny chrome stack 5 inch', 'K5-24SBC', 'K'),
        _ho('chrome stack for my pete', 'K5-24SBC', 'K'),
        _ho('chrome stack', 'K5-24SBC', 'K'),
    ]
    v = evaluate(CandidateRule('chrome stack', 'K5-24SBC'), holdout)
    assert v.passed
    assert v.held_out.passed and v.no_regression.passed and v.subclass.passed


# -- RED 1: OVERFIT -> fails 4.1 (held-out accuracy) -------------------------

def test_overfit_rule_fails_held_out_accuracy():
    # right on the one call it came from; on held-out, "shiny five inch" means many
    # different parts -> low held-out accuracy.
    holdout = [
        _ho('shiny five inch elbow', 'L5-90', 'L'),
        _ho('shiny five inch reducer', 'R5-X', 'R'),
        _ho('shiny five inch stack', 'K5-24SBC', 'K'),
        _ho('the shiny five inch one', 'BH6-36SBC', 'BH'),
    ]
    v = evaluate(CandidateRule('shiny five inch', 'K5-24SBC'), holdout)
    assert not v.passed
    assert not v.held_out.passed                       # 4.1 is the catch
    assert v.held_out.accuracy < 0.90


# -- RED 2: REGRESSING -> passes 4.1, fails 4.2 (no-regression) --------------

def test_regressing_rule_passes_accuracy_but_fails_no_regression():
    # the candidate is accurate on its matched calls (4.1 passes) but its key also
    # matches a call that ALREADY resolves correctly to a different part -> it would
    # break a currently-correct resolution.
    holdout = [_ho(f'chrome stack {i}', 'K5-24SBC', 'K') for i in range(10)]
    holdout.append(_ho('bullhorn chrome stack', 'BH6-36SBC', 'BH',
                       baseline='BH6-36SBC'))          # currently CORRECT
    v = evaluate(CandidateRule('chrome stack', 'K5-24SBC'), holdout)
    assert v.held_out.passed                            # 4.1 tolerated it (10/11 = 0.91)
    assert not v.no_regression.passed                   # 4.2 is the catch
    assert not v.passed


# -- RED 3: SUBCLASS-FAILING -> the DELTA (passes 2-comp, fails 3-comp) ------

def test_subclass_failing_rule_is_caught_ONLY_by_the_subclass_component():
    # right on aggregate AND on family K, but 0% on the BH-family subclass its key
    # also matches. This is the failure that looks like success.
    holdout = [_ho(f'chrome stack {i}', 'K5-24SBC', 'K') for i in range(20)]
    holdout += [_ho('bullhorn stack one', 'BH6-36SBC', 'BH'),     # baseline None -> not a 4.2 regression
                _ho('bullhorn stack two', 'BH6-36SBC', 'BH')]
    cand = CandidateRule('stack', 'K5-24SBC')

    two = evaluate(cand, holdout, include_subclass=False)
    three = evaluate(cand, holdout, include_subclass=True)

    assert two.passed                                   # 2-component battery: PASSES
    assert two.held_out.passed and two.no_regression.passed
    assert not three.passed                             # 3-component battery: FAILS
    assert not three.subclass.passed                    # ...because of 4.3, and ONLY 4.3
    assert three.held_out.passed and three.no_regression.passed
    # the delta is the proof 4.3 is critical: same rule, +subclass-check = caught


# -- the matcher (the granularity-ladder mechanic) ---------------------------

def test_key_matching_is_token_subset_general_keys_match_more():
    assert matches('chrome stack', 'a shiny chrome stack for my pete')   # general -> matches
    assert not matches('chrome stack 5 inch', 'chrome stack')            # specific -> no match
    assert matches('stack', 'bullhorn stack')                           # coarser -> matches more
