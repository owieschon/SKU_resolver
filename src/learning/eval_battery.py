"""The rule-release eval battery (see docs/RESOLUTION_LEARNING_LOOP.md §4) — the
core of the learning loop.

A proposed resolution alias reaches live behavior only by clearing THIS, and it is
the ONLY path (it replaces the stub boolean inside `alias_store.may_promote`, not a
second gate in front of it — two promotion paths is a safety hole). "Rigorous" is
not "held-out accuracy > X%"; the battery has THREE components, all required:

  4.1 Held-out accuracy — on calls the rule was NOT extracted from, it predicts the
      catalog-true SKU at threshold reliability with enough support.
  4.2 No-regression — added to the released set, it does not break a held-out call
      that currently resolves correctly (champion-challenger).
  4.3 Subclass-stratified — it must not pass in aggregate while failing an
      identifiable subclass (product family of the true SKU). The Goodhart guard.

The held-out cases carry an EXOGENOUS true SKU (from the order/quote cross-reference
or a rep label) — NEVER the agent's own resolution — so the agent cannot seed its own
exam (self-laundering guard, invariant 3). `evaluate(..., include_subclass=False)`
runs the 2-component battery, used to DEMONSTRATE that 4.3 is critical: a
subclass-failing rule passes the 2-component and fails the 3-component.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Minimal normalization for key<->phrase matching. Production reuses the SKU-
# translator's tokenizer; kept local + simple here so the battery is self-contained
# and unit-testable. A key matches a phrase iff its content tokens are a subset of
# the phrase's — so a general key ("chrome stack") matches many calls, a specific key
# (full phrase) matches few. That subset relation IS the granularity ladder's
# support/reliability tradeoff, measured rather than asserted.
_STOP = frozenset({
    'a', 'an', 'the', 'for', 'my', 'i', 'need', 'looking', 'want', 'some', 'one',
    'that', 'this', 'is', 'it', 'to', 'of', 'please', 'got', 'have', 'me', 'you',
    'on', 'in', 'and', 'with', 'just', 'about', 'm', "i'm",
})


def _tokens(s: str) -> frozenset:
    return frozenset(t for t in re.findall(r'[a-z0-9]+', (s or '').lower())
                     if t not in _STOP)


def matches(key: str, phrase: str) -> bool:
    kt = _tokens(key)
    return bool(kt) and kt <= _tokens(phrase)


@dataclass(frozen=True)
class HeldOutCase:
    """A held-out resolution case. `true_sku` is EXOGENOUS (order cross-reference /
    rep label), never an agent resolution. `baseline_sku` is what the current system
    resolves it to WITHOUT the candidate (the champion's answer) — `== true_sku`
    means it currently resolves correctly (the no-regression frontier)."""
    phrase: str
    true_sku: str
    family: str                          # subclass stratum (product family of true_sku)
    baseline_sku: str | None = None      # champion's current answer (None = catalog/miss)
    source: str = 'order_not_returned'   # exogenous tier


@dataclass(frozen=True)
class CandidateRule:
    key: str                             # the (normalized) phrase-pattern rung
    target_sku: str


@dataclass(frozen=True)
class ComponentResult:
    passed: bool
    detail: str
    support: int = 0
    accuracy: float = 0.0


@dataclass(frozen=True)
class Verdict:
    passed: bool
    held_out: ComponentResult
    no_regression: ComponentResult
    subclass: ComponentResult

    @classmethod
    def injected_pass(cls) -> 'Verdict':
        """A pre-built PASS verdict for LEGACY transition tests to hand into
        `may_promote` so they keep testing their downstream transition. This is
        VISIBLY A TEST FIXTURE, not a battery stub on the production path — the
        battery's own correctness is proven separately by the three injected-bad-rule
        reds. Do not call this from production code."""
        ok = ComponentResult(True, 'injected (legacy transition fixture)')
        return cls(True, ok, ok, ok)

    @classmethod
    def injected_fail(cls) -> 'Verdict':
        bad = ComponentResult(False, 'injected (legacy transition fixture)')
        ok = ComponentResult(True, 'injected')
        return cls(False, bad, ok, ok)


@dataclass(frozen=True)
class BatteryParams:
    s_min: int = 3                       # min held-out matches (anti-anecdote)
    r_min: float = 0.90                  # min accuracy
    subclass_min_support: int = 2        # a family needs this many matches to be checkable


# -- the three components ----------------------------------------------------

def held_out_accuracy(cand: CandidateRule, holdout, params: BatteryParams) -> ComponentResult:
    matched = [c for c in holdout if matches(cand.key, c.phrase)]
    n = len(matched)
    if n < params.s_min:
        return ComponentResult(False, f'support {n} < s_min {params.s_min}', n, 0.0)
    correct = sum(1 for c in matched if c.true_sku == cand.target_sku)
    acc = correct / n
    return ComponentResult(acc >= params.r_min,
                           f'accuracy {acc:.2f} on {n} held-out (r_min {params.r_min})',
                           n, acc)


def no_regression(cand: CandidateRule, holdout, params: BatteryParams) -> ComponentResult:
    # a regression: the candidate matches a held-out call that currently resolves
    # CORRECTLY (baseline_sku == true_sku) and would change it to a different (wrong)
    # SKU. An alias that matches a call resolves it to the alias target, so any
    # currently-correct call the candidate matches-but-points-elsewhere is broken.
    broken = [c for c in holdout
              if c.baseline_sku == c.true_sku
              and matches(cand.key, c.phrase)
              and cand.target_sku != c.true_sku]
    return ComponentResult(not broken,
                           f'{len(broken)} currently-correct call(s) regressed',
                           len(broken))


def subclass_stratified(cand: CandidateRule, holdout, params: BatteryParams) -> ComponentResult:
    matched = [c for c in holdout if matches(cand.key, c.phrase)]
    by_family: dict = {}
    for c in matched:
        by_family.setdefault(c.family, []).append(c)
    failing = []
    for fam, cases in by_family.items():
        if len(cases) < params.subclass_min_support:
            continue                                  # too few to judge this subclass
        correct = sum(1 for c in cases if c.true_sku == cand.target_sku)
        acc = correct / len(cases)
        if acc < params.r_min:
            failing.append(f'{fam}={acc:.2f}(n={len(cases)})')
    return ComponentResult(not failing,
                           ('subclass(es) below r_min: ' + ', '.join(failing))
                           if failing else 'all checkable subclasses clear',
                           len(matched))


# -- the battery (the single promotion gate) ---------------------------------

def evaluate(cand: CandidateRule, holdout, *, params: BatteryParams = BatteryParams(),
             include_subclass: bool = True) -> Verdict:
    """Run the battery. `include_subclass=False` runs the 2-component version — used
    ONLY to demonstrate that 4.3 is critical (a subclass-failing rule passes the
    2-component and fails the 3-component). Production always runs the full battery."""
    ho = held_out_accuracy(cand, holdout, params)
    nr = no_regression(cand, holdout, params)
    sc = (subclass_stratified(cand, holdout, params) if include_subclass
          else ComponentResult(True, 'subclass check disabled (2-component demo only)'))
    return Verdict(passed=ho.passed and nr.passed and sc.passed,
                   held_out=ho, no_regression=nr, subclass=sc)
