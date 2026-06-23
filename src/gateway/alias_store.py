"""Correction store — a ground-truth-gated, decaying alias map (Phase 5, Part B).

The ONLY thing the self-improvement loop is allowed to mutate: resolution-layer
priors of the form `customer phrase -> part number`. Hard architectural rules,
enforced in TYPES not policy:

  * An alias's value is a part number string. There is NO field for price /
    availability / lead time — a poisoned correction has nowhere to put a fake
    price. Binding facts always come live from the ERP on the resolved part;
    live-lookup-wins, and a failed live lookup degrades the alias (never the
    reverse).
  * Usage is not evidence (W_HIT = 0): the loop using its own alias can't raise
    its confidence. Only EXOGENOUS reality does — a caller's disambiguation
    choice, a rep label, or an order placed and not returned.
  * Silent trust requires a STRONG-tier label (order / rep). An all-caller path
    (acquiescence + disambiguation) can reach confirm-on-alias but never silent
    auto-resolve, so cheap "yeses" can't defeat the high bar.
  * Approval is the proposal; the EVAL is the commit. A correction is `proposed`
    (inert) until it (a) has a positive exogenous label on the specific mapping
    AND (b) a challenger carrying it clears the frozen eval without regression.

All wall-clock is injected (`now` epoch seconds) so the decay backstop is
deterministic in tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# Exogenous label sources, by tier. STRONG = reality/expert; WEAK = caller.
STRONG_SOURCES = ('order_not_returned', 'rep_label')
WEAK_SOURCES = ('caller_disambiguation', 'caller_acquiescence')


@dataclass(frozen=True)
class AliasParams:
    """All grid-tunable. Values are the v1 starting points, not final."""
    c0: float = 0.30                       # initial proposal confidence (inert)
    w_confirm: dict = field(default_factory=lambda: {
        'order_not_returned': 0.40,        # gold: reality kept the part
        'rep_label': 0.25,                 # expert says so
        'caller_disambiguation': 0.15,     # chose A over B = real information
        'caller_acquiescence': 0.05,       # "yes" to a single offer = weak (Bug 1)
    })
    w_hit: float = 0.0                      # usage is NOT evidence (anti self-training)
    decay_factor: float = 0.90             # per unused window
    n_unused: int = 50                     # calls without a hit -> one decay step
    dormancy: float = 0.20
    auto_resolve: float = 0.70
    trust: float = 0.90
    k_trust_labels: int = 2
    stale_retire_after: int = 3            # K failed live lookups -> retire (Bug 2)
    wallclock_floor_secs: float = 30 * 86400.0   # proposal earns nothing in T -> dormant


# Lifecycle states. A correction is born `proposed` (inert) and only `active`
# aliases ever resolve; `contested` always asks; the rest never auto-resolve.
PROPOSED, ACTIVE, CONTESTED, DORMANT, STALE, RETIRED, AWAITING_RELEASE = (
    'proposed', 'active', 'contested', 'dormant', 'stale', 'retired',
    'awaiting_release')


@dataclass
class Alias:
    phrase: str
    target_sku: str                        # <-- the ONLY learnable value. No price.
    confidence: float
    exogenous_labels: int = 0
    strong_labels: int = 0                 # from STRONG_SOURCES only
    last_hit_call: int = -1
    failed_lookups: int = 0
    created_at: float = 0.0                 # epoch (injected)
    last_confirm_at: float = 0.0
    originating_case: dict = field(default_factory=dict)
    state: str = PROPOSED
    contested_with: tuple = ()


def propose(phrase: str, target_sku: str, *, now: float, params: AliasParams = AliasParams(),
            originating_case: dict | None = None) -> Alias:
    """A human/rep correction or harvested resolution enters as PROPOSED — inert,
    does not resolve anything until promoted through the eval gate."""
    return Alias(phrase=phrase, target_sku=target_sku, confidence=params.c0,
                 created_at=now, originating_case=originating_case or {})


# -- transitions (each its own function; tested individually) -----------------

def on_confirm(a: Alias, source: str, *, now: float, params: AliasParams = AliasParams()) -> None:
    """An EXOGENOUS reality signal confirms the mapping. The only thing that
    raises confidence."""
    if source not in params.w_confirm:
        raise ValueError(f'unknown confirm source {source!r}')
    a.confidence = min(1.0, a.confidence + params.w_confirm[source])
    a.exogenous_labels += 1
    if source in STRONG_SOURCES:
        a.strong_labels += 1
    a.last_confirm_at = now
    a.failed_lookups = 0


def on_hit(a: Alias, call_idx: int, *, params: AliasParams = AliasParams()) -> None:
    """The alias was used. Keeps it warm (resets disuse) but adds W_HIT (=0):
    usage is not correctness."""
    a.last_hit_call = call_idx
    a.confidence = min(1.0, a.confidence + params.w_hit)


def on_disuse_window(a: Alias, *, params: AliasParams = AliasParams()) -> None:
    """One unused window elapsed (call_idx - last_hit_call >= n_unused). Decay."""
    a.confidence *= params.decay_factor
    if a.confidence < params.dormancy and a.state in (PROPOSED, ACTIVE):
        a.state = DORMANT


def on_failed_live_lookup(a: Alias, *, params: AliasParams = AliasParams()) -> None:
    """The resolved part failed a LIVE lookup — but 'I couldn't reach the world'
    is not 'the world disagrees' (Bug 2). Go STALE first; only RETIRE after K
    independent failures, so a transient ERP blip doesn't retire a good alias."""
    a.failed_lookups += 1
    if a.failed_lookups >= params.stale_retire_after:
        a.state = RETIRED
        a.confidence = 0.0
    else:
        a.state = STALE


def on_contradicting_correction(a: Alias) -> None:
    """A human/exogenous correction says this phrase maps ELSEWHERE — real
    evidence the alias is wrong. Retire; the case returns to dev."""
    a.state = RETIRED
    a.confidence = 0.0


def on_catalog_churn(a: Alias) -> None:
    """The target part changed/renamed/dropped scope upstream. Recheck before any
    auto-resolve — stale, not retired."""
    a.state = STALE


def mark_contested(a: Alias, other_sku: str, *, params: AliasParams = AliasParams()) -> None:
    """Two targets for one phrase. CONTESTED -> disambiguation prompt. Clamp
    confidence below auto_resolve too, so safety doesn't depend on clause order
    in resolution_mode (defense in depth on the most expensive transition)."""
    a.state = CONTESTED
    a.contested_with = tuple(sorted(set(a.contested_with) | {other_sku}))
    a.confidence = min(a.confidence, params.auto_resolve - 1e-9)


def wallclock_floor_sweep(a: Alias, *, now: float, params: AliasParams = AliasParams()) -> None:
    """Backstop to usage-based decay: a PROPOSED alias that earned NO exogenous
    label within T real-time and is below auto_resolve goes DORMANT, so a wrong
    alias on a never-recurring phrase doesn't sit immortal. (Timer as a floor,
    not the primary mechanism.)"""
    if (a.state == PROPOSED and a.exogenous_labels == 0
            and a.confidence < params.auto_resolve
            and (now - a.created_at) > params.wallclock_floor_secs):
        a.state = DORMANT


# -- promotion gate (eval is the commit) -------------------------------------

def may_promote(a: Alias, *, verdict, params: AliasParams = AliasParams()) -> bool:
    """A PROPOSED alias CLEARS the release gate only with BOTH a positive exogenous
    label on the SPECIFIC mapping AND a battery `verdict` that passes — the §4
    three-component battery (held-out accuracy + no-regression + subclass-stratified)
    REPLACING the old stub boolean INSIDE this gate, so there is exactly ONE path to
    release and it is the rigorous one. Clearing the gate STAGES the rule for human
    release (`stage_for_release`); it does NOT make it live. `verdict` is duck-typed
    (anything exposing `.passed`) so alias_store stays decoupled from `learning`."""
    return (a.state == PROPOSED
            and a.confidence >= params.auto_resolve
            and a.exogenous_labels >= 1
            and bool(getattr(verdict, 'passed', False)))


def stage_for_release(a: Alias) -> None:
    """A battery-cleared challenger WAITS for a human. AWAITING_RELEASE is NOT live —
    it resolves nothing until released."""
    a.state = AWAITING_RELEASE


def release(a: Alias) -> None:
    """The human confirms a battery-cleared challenger into live behavior. The ONLY
    transition into ACTIVE — so 'a human releases on a mechanically-cleared eval' is
    enforced in code, not convention (invariant 4b). Confirm, not judge: the rule has
    already mechanically cleared the gate; the human is releasing, not re-grading."""
    if a.state != AWAITING_RELEASE:
        raise ValueError(f'release requires awaiting_release state, got {a.state!r}')
    a.state = ACTIVE


# -- how the resolver may use an alias ---------------------------------------

def resolution_mode(a: Alias, *, params: AliasParams = AliasParams()) -> str:
    """'auto_silent' | 'auto_confirm' | 'disambiguate'. Confirm-on-alias is the
    default for learned content; silent trust is a high, strong-tier-gated bar."""
    if a.state in (PROPOSED, AWAITING_RELEASE, DORMANT, STALE, RETIRED, CONTESTED):
        return 'disambiguate'        # not-live (incl. awaiting human release) -> ask
    if a.state == ACTIVE:
        if (a.confidence >= params.trust
                and a.exogenous_labels >= params.k_trust_labels
                and a.strong_labels >= 1):          # never an all-caller path (Bug 1)
            return 'auto_silent'
        if a.confidence >= params.auto_resolve and a.exogenous_labels >= 1:
            return 'auto_confirm'                   # confirm-on-alias; caller in loop
        return 'disambiguate'                       # active but decayed -> re-confirm (explicit)
    return 'disambiguate'
