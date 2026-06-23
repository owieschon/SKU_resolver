"""Shadow replay with re-grounding + per-stream divergence detection.

Re-ground, not drift: at each turn the agent is fed the real conversation-so-far,
its proposed move is captured, and then the state is advanced using the HUMAN's
actual outcome — so the agent never runs off onto a fictional path; it always
proposes against the conversation that actually happened. That turns "where does the
counterfactual become fiction" into a DETECTABLE split: the first turn the agent's
move disagrees with the human's.

Two streams, tracked separately (a turn can be resolution-aligned but behavior-
divergent — the resolution stream is catalog-checkable and stays aligned longer):

  * RESOLUTION  — agent's resolved SKU vs the human's (which part the call dealt
    with). Exogenous: we know which is RIGHT, not just that they differ.
  * BEHAVIORAL  — agent's branch (escalate/gate/disambiguate/disclose) vs the rep's.

Re-ground's gain (resolution signal flowing past the behavioral split) carries one
obligation, encoded here: a resolution the agent reaches AFTER the behavioral
divergence is correct-but-conditional — it resolved against a history the agent
wouldn't have produced (e.g. a clarifying question the rep asked). It is evidence of
resolution CAPABILITY, not of the agent's AUTONOMOUS path to it. So resolution
labels are tagged `autonomous` (pre-behavioral-divergence) vs `conditional`
(post-) — letting conditional masquerade as autonomous would inflate the agent's
standalone accuracy. The divergence LOCATION is rep-adjudicable (the structural
detector proposes; a misfire there mistags everything downstream).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pilot.capture import RawCall, ScrubbedCall, scrub_call
from pilot.decision_point import DecisionPoint


def agent_branch(dp: DecisionPoint) -> str:
    """Map the agent's structured decision into the shared branch vocabulary so it
    is comparable to the human's move."""
    if dp.escalated:
        return 'escalate'
    if dp.move == 'price':
        return 'disclose_price' if dp.disclosed else 'gate_price'
    if dp.move == 'availability':
        if dp.disclosed:
            return 'disclose_availability'
        if dp.candidates or dp.resolution == 'ambiguous':
            return 'clarify'                 # disambiguating an ambiguous part
        return 'other'
    if dp.resolution == 'ambiguous' or dp.candidates:
        return 'clarify'
    if dp.move == 'establish_account':
        return 'establish_account'
    return 'other'


# Branches that actually involve a part resolution (so the resolution stream gets a
# comparison point). An account/close/escalate-only turn is NOT resolution-bearing:
# the durable focus part carries forward, but no resolution DECISION was made, so it
# must not register a false resolution divergence.
_PART_FACING = frozenset({'disclose_availability', 'disclose_price', 'gate_price',
                          'clarify'})


@dataclass(frozen=True)
class TurnComparison:
    index: int
    agent_resolved: str | None
    human_resolved: str | None
    agent_branch: str
    human_branch: str

    @property
    def resolution_bearing(self) -> bool:
        return (self.human_resolved is not None
                or self.human_branch in _PART_FACING
                or (self.agent_resolved is not None
                    and self.agent_branch in _PART_FACING))

    @property
    def resolution_match(self) -> bool:
        return self.agent_resolved == self.human_resolved

    @property
    def behavioral_match(self) -> bool:
        return self.agent_branch == self.human_branch


@dataclass
class DivergenceMarker:
    """Per-stream first-disagreement indices. REP-ADJUDICABLE: the structural
    detector proposes these; `adjudicate()` lets a rep correct a misfired location,
    and every downstream tag re-derives from the corrected marker."""
    resolution: int | None = None        # first turn resolution differs
    behavioral: int | None = None        # first turn branch differs

    def adjudicate(self, *, resolution: object = '_keep',
                   behavioral: object = '_keep') -> 'DivergenceMarker':
        return DivergenceMarker(
            resolution=self.resolution if resolution == '_keep' else resolution,
            behavioral=self.behavioral if behavioral == '_keep' else behavioral)


def _reground(gw, caller_id, tok, hm) -> None:
    """Advance the agent's state to the HUMAN's actual outcome, so the next turn is
    proposed against the real conversation, not the agent's counterfactual."""
    conv = gw._conversations.get(caller_id)
    if conv is None:
        return
    if hm.resolved_sku:
        ctx = f'part:{hm.resolved_sku}'
        if ctx not in conv.state.parts:
            conv.add_part(ctx)
        conv.identify_part(ctx, hm.resolved_sku)
        conv.set_focus(ctx)
        gw.sessions.remember_sku(caller_id, tok, hm.resolved_sku)
    if hm.established_account:
        gw.sessions.verify(caller_id, tok, account_no=hm.established_account, name=None)
        conv.establish_account(hm.established_account)


def replay_with_regrounding(raw: RawCall, gw, *, caller_id='shadow'):
    """Replay the call through the LIVE agent (gw.converse) on the RAW text, capturing
    each proposed move and re-grounding to the human's outcome. Returns the per-turn
    comparisons and the proposed (structural) DivergenceMarker."""
    gw._conversations.pop(caller_id, None)               # fresh shadow conversation
    tok = gw.sessions.open(caller_id, caller_id)
    comps = []
    for i, turn in enumerate(raw.turns):
        resp = gw.converse(caller_id, tok, turn.caller_text)   # RAW (pre-scrub)
        dp = DecisionPoint.from_turn(turn.caller_text, resp)
        comps.append(TurnComparison(
            index=i, agent_resolved=dp.resolved_sku,
            human_resolved=turn.human.resolved_sku,
            agent_branch=agent_branch(dp), human_branch=turn.human.branch))
        _reground(gw, caller_id, tok, turn.human)
    return comps, _detect(comps)


def _detect(comps) -> DivergenceMarker:
    # resolution divergence only counts resolution-bearing turns (a verify/close
    # turn isn't a resolution decision); behavioral divergence counts every turn.
    res = next((c.index for c in comps
                if c.resolution_bearing and not c.resolution_match), None)
    beh = next((c.index for c in comps if not c.behavioral_match), None)
    return DivergenceMarker(resolution=res, behavioral=beh)


@dataclass(frozen=True)
class TaggedDecision:
    index: int
    comparison: TurnComparison
    resolution_class: str | None         # autonomous | conditional | resolution_divergence | None
    behavioral_class: str | None         # pre_divergence | at_divergence | post_divergence | None
    tags: tuple = ()                     # flat tags for the Label (incl. rep_self_comparison)


def tag_decisions(comps, marker: DivergenceMarker):
    """Per-stream tags. Resolution autonomous-vs-conditional keys on the BEHAVIORAL
    divergence (a correct resolution AFTER the behavioral split was reached via the
    human's context, not the agent's path). Re-derives entirely from the (possibly
    rep-adjudicated) marker."""
    bd, rd = marker.behavioral, marker.resolution
    out = []
    for c in comps:
        # resolution stream (only resolution-bearing turns)
        res_class = None
        if c.resolution_bearing:
            if rd is not None and c.index >= rd and not c.resolution_match:
                res_class = 'resolution_divergence'      # agent's SKU differs (catalog-checkable)
            elif bd is None or c.index < bd:
                res_class = 'autonomous'                 # agent's own path -> standalone accuracy
            else:
                res_class = 'conditional'                # reached via human context -> capability only
        # behavioral stream
        beh_class = None
        if c.human_branch != 'none' or c.agent_branch != 'other':
            if bd is None or c.index < bd:
                beh_class = 'pre_divergence'
            elif c.index == bd:
                beh_class = 'at_divergence'
            else:
                beh_class = 'post_divergence'
        tags = []
        if res_class:
            tags.append(f'resolution:{res_class}')
        if beh_class:
            tags.append(f'behavioral:{beh_class}')
        if beh_class in ('at_divergence', 'post_divergence'):
            tags.append('rep_self_comparison')           # the bias flag (point 2)
        out.append(TaggedDecision(index=c.index, comparison=c,
                                  resolution_class=res_class,
                                  behavioral_class=beh_class, tags=tuple(tags)))
    return out


@dataclass(frozen=True)
class ShadowIngest:
    """What persists: the SCRUBBED transcript + the tagged decision points + the
    divergence marker. Raw text and audio are dropped after the (raw-window) replay."""
    call_id: str
    scrubbed: ScrubbedCall
    marker: DivergenceMarker
    decisions: tuple = ()                # tuple[TaggedDecision]


def ingest(raw: RawCall, gw, *, names=(), caller_id='shadow') -> ShadowIngest:
    """The ingestion pipeline: replay+detect on RAW (inside this window), tag, then
    scrub for persistence. The raw call is NOT returned — only the scrubbed artifact."""
    comps, marker = replay_with_regrounding(raw, gw, caller_id=caller_id)
    decisions = tag_decisions(comps, marker)
    scrubbed = scrub_call(raw, names=names)             # raw dropped after this
    return ShadowIngest(call_id=raw.call_id, scrubbed=scrubbed, marker=marker,
                        decisions=tuple(decisions))
