"""Shadow / listen-in onboarding mode — learn from real human-to-human calls.

During onboarding the agent can ride along on a real rep<->customer call,
**observe-only**: it never speaks or acts. For each customer utterance it runs
the resolution pipeline and records what it WOULD have done and whether it
succeeded — producing a capability/failure map of the live conversation. That
map drives a dedicated HITL session where the SME teaches the tool how to handle
each failure point, either by:

  - a grammar/semantic correction (a phrase -> a REAL catalog SKU), or
  - a chosen graceful-degradation behavior for a failure category.

Corrections are applied through a `CorrectionStore` the observer consults FIRST,
so a fixed failure point resolves on the very next pass — the HITL improvement
loop, closed. Never-invent is preserved: an alias may only target a SKU that
exists in the catalog; an alias to a non-existent SKU is rejected.

The observe→map→correct artifacts are also the anonymized record used to improve
the service over time (see observability.service_improvement).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Runtime imports are done inside methods to avoid a circular import with
    # alias_store; this resolves the name for annotations / type-checkers.
    from gateway.alias_store import Alias

# A customer utterance is "part-like" (worth attempting) if it carries a
# code-shaped token (letters+digits) or a product descriptor. Chit-chat and
# affirmations fall through as 'not_a_part' rather than counting as failures.
_CODE_HINT = re.compile(r'[A-Za-z]*\d[A-Za-z0-9]+')
_DESC_HINT = re.compile(
    r'\b(inch|"|chrome|stack|elbow|curve|stainless|aluminized|aluminium|'
    r'aluminum|flange|gasket|bracket|clamp|pipe|tube|od|id|diameter)\b', re.I)

_OUTCOME = {'resolved': 'success', 'pending_disambiguation': 'ambiguous',
            'unresolvable': 'no_match'}

# A code-shaped token (has a digit) the rep might say when resolving an inquiry.
_SKU_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9\-/]{3,}')


def looks_part_like(text: str) -> bool:
    return bool(_CODE_HINT.search(text or '')) or bool(_DESC_HINT.search(text or ''))


def _norm(s: str) -> str:
    return ' '.join((s or '').lower().split())


def opportunity_from(att) -> 'ImprovementOpportunity | None':
    """Derive a self-monitoring opportunity from one of the agent's own
    attempts: it struggled, or resolved only at non-high confidence."""
    if att.outcome == 'not_a_part':
        return None
    if att.outcome in ('no_match', 'ambiguous'):
        reason = f'agent could not resolve ({att.outcome})'
    elif att.outcome == 'success' and att.confidence != 'high':
        reason = f'resolved only at {att.confidence} confidence'
    else:
        return None
    return ImprovementOpportunity(
        utterance=att.utterance, outcome=att.outcome, confidence=att.confidence,
        candidate_skus=att.candidate_skus, reason=reason)


@dataclass(frozen=True)
class ShadowAttempt:
    utterance: str
    speaker: str               # 'customer' | 'rep'
    state: str                 # resolution state, or '-' when skipped
    sku: str | None
    confidence: str
    candidate_skus: tuple
    outcome: str               # success | ambiguous | no_match | not_a_part
    source: str                # resolution | learned_correction | skipped


@dataclass(frozen=True)
class FailurePoint:
    category: str              # ambiguous | no_match
    count: int
    example_utterances: tuple


@dataclass(frozen=True)
class SelfHeal:
    """What the human rep did after the tool failed — harvested as a proposed
    correction. `source`:
      - 'rep_said_sku'     : the rep stated a REAL catalog SKU (strongest signal)
      - 'rep_restatement'  : the rep restated the part and the tool could resolve
                             that restatement to a real SKU
    Never-invent: healed_sku is always a real catalog row."""
    failed_utterance: str
    healed_sku: str
    source: str
    rep_turn: str
    confidence: str            # high | medium
    applied: bool = False      # auto-applied to the CorrectionStore?
    origin: str = 'shadow'     # shadow | post_handoff


@dataclass(frozen=True)
class ImprovementOpportunity:
    """Spotted while the agent handled a call ITSELF — a moment it was uncertain
    (struggled, or resolved only at low confidence). No proposed SKU; surfaced
    for a human to review and decide whether/how to improve. Never auto-applied
    — the agent monitoring itself does not change its own behavior unattended."""
    utterance: str
    outcome: str
    confidence: str
    candidate_skus: tuple
    reason: str
    origin: str = 'self_monitor'


@dataclass(frozen=True)
class CapabilityMap:
    attempted: int
    succeeded: int
    failure_points: tuple

    @property
    def success_rate(self) -> float:
        return round(self.succeeded / self.attempted, 3) if self.attempted else 0.0

    @classmethod
    def from_attempts(cls, attempts) -> 'CapabilityMap':
        considered = [a for a in attempts if a.outcome != 'not_a_part']
        succeeded = sum(1 for a in considered if a.outcome == 'success')
        buckets: dict[str, list] = defaultdict(list)
        for a in considered:
            if a.outcome in ('ambiguous', 'no_match'):
                buckets[a.outcome].append(a.utterance)
        fps = tuple(sorted(
            (FailurePoint(cat, len(u), tuple(u[:5])) for cat, u in buckets.items()),
            key=lambda f: -f.count))
        return cls(attempted=len(considered), succeeded=succeeded,
                   failure_points=fps)


class CorrectionStore:
    """Gated correction store — aliases go through the propose → label →
    battery → human-release pipeline. The ONLY path to ACTIVE is:
      propose_correction() → on_confirm() → clear_for_release(verdict) →
      release() → ACTIVE.
    `alias_for` returns ACTIVE aliases only, with resolution_mode.

    Degradations map a failure category to a chosen graceful-degradation
    behavior (unchanged — they are not aliases)."""

    def __init__(self, catalog, path=None) -> None:
        self._catalog = catalog
        self._path = path                  # persist here if given (JSON)
        self._aliases: dict[str, Alias] = {}
        self._degradations: dict[str, str] = {}
        if path is not None:
            self.load()

    # -- the gated entry point (replaces the deleted add_alias) ----------------

    def propose_correction(self, phrase: str, sku: str, *, source: str = 'rep_label',
                           now: float = 0.0) -> 'Alias':
        """Propose a correction: creates a PROPOSED alias and applies an initial
        exogenous label. The alias is INERT until it clears the battery and is
        human-released. Never-invent enforced at entry."""
        from gateway.alias_store import on_confirm, propose
        if not self._catalog.is_canonical(sku):
            raise ValueError(f'alias target {sku!r} is not a real catalog SKU '
                             f'(never-invent: corrections must reference a real row)')
        key = _norm(phrase)
        a = propose(key, sku, now=now)
        on_confirm(a, source, now=now)
        self._aliases[key] = a
        self._save()
        return a

    def confirm_alias(self, phrase: str, source: str, *, now: float = 0.0) -> None:
        """Add an exogenous label to an existing alias (raises confidence)."""
        from gateway.alias_store import on_confirm
        key = _norm(phrase)
        if key not in self._aliases:
            return
        on_confirm(self._aliases[key], source, now=now)
        self._save()

    def clear_for_release(self, phrase: str, *, verdict) -> bool:
        """Run the alias through the promotion gate: may_promote checks confidence
        floor + exogenous labels + battery verdict. If cleared, stages for human
        release (AWAITING_RELEASE). Returns True if staged."""
        from gateway.alias_store import may_promote, stage_for_release
        key = _norm(phrase)
        a = self._aliases.get(key)
        if a is None:
            return False
        if may_promote(a, verdict=verdict):
            stage_for_release(a)
            self._save()
            return True
        return False

    def release(self, phrase: str) -> None:
        """Human releases a battery-cleared alias into ACTIVE. The ONLY transition
        to ACTIVE — invariant 4b."""
        from gateway.alias_store import release as _release
        key = _norm(phrase)
        a = self._aliases.get(key)
        if a is None:
            raise ValueError(f'no alias for {phrase!r}')
        _release(a)
        self._save()

    def alias_for(self, text: str) -> 'tuple[str, str] | None':
        """Returns (sku, resolution_mode) for the best matching ACTIVE alias,
        or None if no ACTIVE alias matches. Non-ACTIVE aliases are invisible
        to the resolver — the gate holds."""
        from gateway.alias_store import ACTIVE, resolution_mode
        norm = _norm(text)
        for phrase in sorted(self._aliases, key=len, reverse=True):
            if phrase and phrase in norm:
                a = self._aliases[phrase]
                if a.state == ACTIVE:
                    return (a.target_sku, resolution_mode(a))
        return None

    def get_alias(self, phrase: str) -> 'Alias | None':
        """Direct access to an alias object (for inspection/testing)."""
        return self._aliases.get(_norm(phrase))

    def set_degradation(self, category: str, behavior: str) -> None:
        self._degradations[category] = behavior
        self._save()

    def degradation_for(self, category: str) -> str | None:
        return self._degradations.get(category)

    # -- persistence: learned corrections survive restart ----------------------
    def _save(self) -> None:
        if self._path is None:
            return
        import dataclasses
        import json
        from pathlib import Path
        p = Path(self._path)
        p.parent.mkdir(parents=True, exist_ok=True)
        aliases_data = {k: dataclasses.asdict(v) for k, v in self._aliases.items()}
        p.write_text(json.dumps({'aliases': aliases_data,
                                 'degradations': self._degradations},
                                indent=1, sort_keys=True))

    def load(self) -> None:
        import json
        from pathlib import Path

        from gateway.alias_store import Alias
        p = Path(self._path)
        if not p.exists():
            return
        data = json.loads(p.read_text())
        raw_aliases = data.get('aliases', {})
        self._aliases = {}
        for k, v in raw_aliases.items():
            if isinstance(v, str):
                # Legacy format: plain {phrase: sku} — skip (old ungated data)
                continue
            if isinstance(v, dict) and self._catalog.is_canonical(v.get('target_sku', '')):
                if 'contested_with' in v and isinstance(v['contested_with'], list):
                    v['contested_with'] = tuple(v['contested_with'])
                if 'originating_case' not in v:
                    v['originating_case'] = {}
                self._aliases[k] = Alias(**v)
        self._degradations = dict(data.get('degradations', {}))


class ShadowObserver:
    """Observe-only run of the pipeline over a real call. Records attempts;
    never speaks or mutates a session. Consults the CorrectionStore first."""

    def __init__(self, service, *, catalog=None,
                 corrections: 'CorrectionStore | None' = None,
                 customer: str | None = None, log=None) -> None:
        self._svc = service
        self._catalog = catalog
        self._corr = corrections
        self._customer = customer
        self._log = log

    def observe(self, speaker: str, text: str) -> ShadowAttempt:
        if speaker != 'customer' or not looks_part_like(text):
            att = ShadowAttempt(text, speaker, '-', None, 'none', (),
                                'not_a_part', 'skipped')
            return self._emit(att)
        if self._corr is not None:
            result = self._corr.alias_for(text)
            if result is not None:
                sku, mode = result
                return self._emit(ShadowAttempt(
                    text, speaker, 'resolved', sku, 'high', (sku,),
                    'success', 'learned_correction'))
        res = self._svc.resolve(text, customer=self._customer)
        return self._emit(ShadowAttempt(
            text, speaker, res.state, res.sku, res.confidence,
            tuple(c.sku for c in res.candidates),
            _OUTCOME.get(res.state, 'no_match'), 'resolution'))

    def observe_call(self, turns) -> list[ShadowAttempt]:
        return [self.observe(speaker, text) for speaker, text in turns]

    def observe_call_with_healing(self, turns, *, autonomous: bool = False
                                  ) -> tuple[list, list]:
        """Sequential pass: observe each customer utterance and, when one FAILS,
        look at how the human rep handled it next — harvesting the rep's
        resolution as a SelfHeal. Returns (attempts, heals).

        Self-healing/autonomous learning: with `autonomous=True` AND a
        CorrectionStore, the strongest signal ('rep_said_sku' — a human literally
        stated a real catalog SKU) is auto-applied, so the same failure resolves
        on the next pass. Weaker signals ('rep_restatement', the tool's own
        inference) stay PROPOSED for the HITL session — rules + human bind."""
        attempts, heals = [], []
        for i, (speaker, text) in enumerate(turns):
            att = self.observe(speaker, text)
            attempts.append(att)
            if att.outcome in ('no_match', 'ambiguous'):
                heal = self._harvest(text, turns, i + 1)
                if heal is None:
                    continue
                if (autonomous and self._corr is not None
                        and heal.source == 'rep_said_sku'):
                    self._corr.propose_correction(
                        heal.failed_utterance, heal.healed_sku,
                        source='rep_label', now=0.0)
                    heal = replace(heal, applied=True)
                heals.append(heal)
                if self._log is not None:
                    self._log.record_self_heal(heal)
        return attempts, heals

    def _harvest(self, failed_text: str, turns, start: int,
                 origin: str = 'shadow') -> 'SelfHeal | None':
        """Scan the human turns following a failure (until the next customer
        turn) for how the human resolved it."""
        for speaker, text in turns[start:]:
            if speaker == 'customer':
                break
            # (a) the human stated a real catalog SKU
            if self._catalog is not None:
                for tok in _SKU_TOKEN.findall(text):
                    if any(c.isdigit() for c in tok) and \
                            self._catalog.is_canonical(tok.upper()):
                        return SelfHeal(failed_text, tok.upper(), 'rep_said_sku',
                                        text, 'high', origin=origin)
            # (b) the human restated the part precisely enough to resolve
            res = self._svc.resolve(text, customer=self._customer)
            if res.state == 'resolved' and res.sku:
                return SelfHeal(failed_text, res.sku, 'rep_restatement', text,
                                res.confidence or 'medium', origin=origin)
        return None

    def harvest_handoff(self, failed_utterance: str,
                        human_turns) -> 'SelfHeal | None':
        """Post-handoff learning: after the agent gracefully degraded and the
        call was transferred to a human, observe what the human did with the
        inquiry the agent couldn't handle — harvested as a 'post_handoff' heal."""
        turns = [('human', t) for t in human_turns]
        return self._harvest(failed_utterance, turns, 0, origin='post_handoff')

    def self_monitor(self, speaker: str, text: str) -> 'ImprovementOpportunity | None':
        """On a call the agent handles ITSELF: flag a turn where it was uncertain
        as an opportunity for human review. No SKU proposed; nothing auto-applied."""
        return opportunity_from(self.observe(speaker, text))

    def _emit(self, att: ShadowAttempt) -> ShadowAttempt:
        if self._log is not None:
            self._log.record_attempt(att)
        return att


class ShadowCampaign:
    """Observe MANY real calls over a configurable period, then build ONE
    aggregate capability/failure map across the whole window — so the HITL
    session targets the failures that recur most across the period, not a single
    call. `window_days=None` means continuous (open-ended ride-along)."""

    def __init__(self, observer: ShadowObserver, *,
                 window_days: int | None = None) -> None:
        self._obs = observer
        self.window_days = window_days
        self._attempts: list[ShadowAttempt] = []
        self._heals: list = []
        self.calls = 0

    @property
    def continuous(self) -> bool:
        return self.window_days is None

    def window_open(self, elapsed_days: float) -> bool:
        """Whether the observation window is still open after `elapsed_days`."""
        return self.continuous or elapsed_days < self.window_days

    def observe_call(self, turns, *, heal: bool = False,
                     autonomous: bool = False) -> list[ShadowAttempt]:
        if heal:
            atts, heals = self._obs.observe_call_with_healing(
                turns, autonomous=autonomous)
            self._heals.extend(heals)
        else:
            atts = self._obs.observe_call(turns)
        self._attempts.extend(atts)
        self.calls += 1
        return atts

    @property
    def heals(self) -> tuple:
        return tuple(self._heals)

    def capability_map(self) -> CapabilityMap:
        """Aggregate map across every call observed so far; failure points are
        ranked by how often they recur across the period."""
        return CapabilityMap.from_attempts(self._attempts)

    @property
    def attempts(self) -> tuple:
        return tuple(self._attempts)


@dataclass(frozen=True)
class ReviewBatch:
    """What a periodic HITL review presents:
      - proposals: SelfHeal (applied=False) to confirm (from ride-along or
        post-handoff learning)
      - opportunities: self-monitored moments the agent was uncertain about on
        its own calls
      - unresolved_failures: recurring failures it could NOT self-heal (need an
        SME grammar/degradation decision)."""
    proposals: tuple
    opportunities: tuple
    unresolved_failures: tuple


class ContinuousImprovement:
    """Always-on background listening feeding a continuous self-improvement loop
    with periodic HITL review.

    It rides along on every call (no window — it never expires), observing in
    read-only mode and auto-applying the strongest self-heal signal
    ('rep_said_sku') so the tool improves on its own between reviews. Weaker
    proposals and failures it could not self-heal accumulate; when the review
    cadence is reached (`review_every` proposals, or `review_every_calls` calls)
    a `ReviewBatch` is surfaced for an SME to confirm/correct. Confirmed
    proposals are applied; the batch clears; the loop continues.

    Strong signals are never-invent-safe (a real catalog SKU a human stated), so
    auto-applying them between reviews is safe; everything inferred by the tool
    itself stays gated behind the periodic review.
    """

    def __init__(self, observer: ShadowObserver, corrections: CorrectionStore, *,
                 review_every: int = 25, review_every_calls: int | None = None,
                 log=None, alerts=None, now_iso=lambda: '',
                 state_path=None) -> None:
        self._obs = observer
        self._corr = corrections
        self._review_every = review_every
        self._review_every_calls = review_every_calls
        self._log = log
        self._alerts = alerts            # optional AlertRouter
        self._now = now_iso
        self._review_cycle = 0
        self._state_path = state_path    # persist the review queue if given
        self._attempts: list[ShadowAttempt] = []
        self._pending: list[SelfHeal] = []      # weak heals awaiting HITL
        self._auto_applied: list[SelfHeal] = []  # strong heals already learned
        self._opportunities: list[ImprovementOpportunity] = []
        self.calls = 0
        self._calls_since_review = 0
        if state_path is not None:       # restore AFTER fields exist
            self.restore()

    def ingest_call(self, turns) -> tuple[list, list]:
        """Source 1 — training ride-along on a rep<->customer call (always-on).
        Strong heals auto-apply immediately; weaker ones queue for review."""
        attempts, heals = self._obs.observe_call_with_healing(
            turns, autonomous=True)
        self._attempts.extend(attempts)
        self.calls += 1
        self._calls_since_review += 1
        for h in heals:
            (self._auto_applied if h.applied else self._pending).append(h)
        self._maybe_alert()
        self._persist()
        return attempts, heals

    def _maybe_alert(self) -> None:
        """When enough has accumulated for a periodic review, notify the SME
        (off unless an AlertRouter was provided)."""
        if self._alerts is None or not self.review_due():
            return
        b = self.pending_review()
        self._alerts.route(
            severity='warning', title='HITL review due',
            summary=(f'{len(b.proposals)} self-heal proposal(s), '
                     f'{len(b.opportunities)} opportunity(ies), '
                     f'{len(b.unresolved_failures)} unresolved failure(s)'),
            now_iso=self._now(), dedup_key=f'review-{self._review_cycle}')

    def ingest_handoff(self, failed_utterance: str, human_turns) -> 'SelfHeal | None':
        """Source 2 — post-handoff learning: the agent gracefully degraded and
        transferred; observe how the human handled the inquiry it could not.
        Strongest signal auto-applies; otherwise it queues for review."""
        heal = self._obs.harvest_handoff(failed_utterance, human_turns)
        if heal is None:
            return None
        if heal.source == 'rep_said_sku':
            self._corr.propose_correction(
                heal.failed_utterance, heal.healed_sku,
                source='rep_label', now=0.0)
            heal = replace(heal, applied=True)
            self._auto_applied.append(heal)
        else:
            self._pending.append(heal)
        if self._log is not None:
            self._log.record_self_heal(heal)
        self._maybe_alert()
        self._persist()
        return heal

    def ingest_self_monitored_call(self, turns) -> list:
        """Source 3 — self-monitoring of a call the agent handled ITSELF: flag
        its own uncertain moments as opportunities for human review. Nothing is
        auto-applied (the agent does not change its own behavior unattended)."""
        found = []
        for speaker, text in turns:
            att = self._obs.observe(speaker, text)
            self._attempts.append(att)
            opp = opportunity_from(att)
            if opp is not None:
                self._opportunities.append(opp)
                found.append(opp)
        self.calls += 1
        self._calls_since_review += 1
        self._maybe_alert()
        self._persist()
        return found

    def observe_agent_turn(self, text: str):
        """Capture one turn from the hosted-voice-agent path (same data as the
        self-monitoring on the agent's own calls): record the attempt + flag an
        uncertain moment as an opportunity. Read-only; no call-count change."""
        att = self._obs.observe('customer', text)
        self._attempts.append(att)
        opp = opportunity_from(att)
        if opp is not None:
            self._opportunities.append(opp)
        self._maybe_alert()
        self._persist()
        return opp

    def review_due(self) -> bool:
        if len(self._pending) + len(self._opportunities) >= self._review_every:
            return True
        return (self._review_every_calls is not None
                and self._calls_since_review >= self._review_every_calls)

    def pending_review(self) -> ReviewBatch:
        healed = {h.failed_utterance for h in self._auto_applied + self._pending}
        cap = CapabilityMap.from_attempts(self._attempts)
        unresolved = tuple(fp for fp in cap.failure_points
                           if not any(u in healed for u in fp.example_utterances))
        return ReviewBatch(proposals=tuple(self._pending),
                           opportunities=tuple(self._opportunities),
                           unresolved_failures=unresolved)

    def apply_review(self, approvals) -> int:
        """Apply the SME's confirmations. `approvals` is an iterable of SelfHeal
        proposals to accept. Accepted heals become corrections; the pending
        queue and reviewed opportunities clear; the cadence resets."""
        approved = {id(a) for a in approvals}
        applied = 0
        for h in self._pending:
            if id(h) in approved:
                self._corr.propose_correction(
                    h.failed_utterance, h.healed_sku,
                    source='rep_label', now=0.0)
                applied += 1
                if self._log is not None:
                    self._log.record_correction(
                        category='self_heal_confirmed', kind='alias',
                        phrase=h.failed_utterance, sku=h.healed_sku)
        self._pending.clear()
        self._opportunities.clear()
        self._calls_since_review = 0
        self._review_cycle += 1          # next review alert is a new dedup key
        self._persist()
        return applied

    def capability_map(self) -> CapabilityMap:
        return CapabilityMap.from_attempts(self._attempts)

    @property
    def auto_applied(self) -> tuple:
        return tuple(self._auto_applied)

    # -- review-queue persistence (survives restart) ---------------------------
    def snapshot(self) -> dict:
        from dataclasses import asdict
        return {'review_cycle': self._review_cycle,
                'pending': [asdict(h) for h in self._pending],
                'opportunities': [asdict(o) for o in self._opportunities]}

    def restore(self) -> None:
        import json
        from pathlib import Path
        p = Path(self._state_path)
        if not p.exists():
            return
        data = json.loads(p.read_text())
        self._review_cycle = data.get('review_cycle', 0)
        self._pending = [SelfHeal(**d) for d in data.get('pending', [])]
        self._opportunities = [ImprovementOpportunity(**d)
                               for d in data.get('opportunities', [])]

    def _persist(self) -> None:
        if self._state_path is None:
            return
        import json
        from pathlib import Path
        p = Path(self._state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.snapshot(), indent=1))
