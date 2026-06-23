"""The labeling UNIT: a decision point, extracted from a REAL turn's structured
emission (`resp.meta['decision']` from the wired orchestration) — never parsed from
prose. A call has many decisions, each independently right or wrong; "was the call
good" is too coarse to route and "label every token" is too fine to ask a human.
The decision point is the right grain, and it falls out of the trace the
orchestration already emits.

`exercised()` is the not-exercised trichotomy applied to the HUMAN: ask only the
label questions for decisions that ACTUALLY OCCURRED this turn. Asking a rep "was
the pricing gate correct?" on a turn where pricing never came up produces a guess,
and a guess that looks like a label is noise. So the trace drives the questions.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DecisionPoint:
    caller_text: str               # the utterance being decided on
    say: str                       # what the agent said (for QUALITY only)
    move: str | None               # availability | price | establish_account | escalate | close
    resolved_sku: str | None       # the part the resolution landed on, if identified
    resolution: str                # identified | ambiguous | unknown | none
    candidates: tuple = ()         # candidate SKUs when ambiguous
    account_established: bool = False
    disclosed: bool = False        # a binding fact was disclosed
    refused: str | None = None
    escalated: bool = False

    @classmethod
    def from_turn(cls, caller_text: str, resp) -> 'DecisionPoint':
        """Build from a REAL converse TurnResponse — the decision point the live
        orchestration emits, not a hand-made dict."""
        d = (getattr(resp, 'meta', None) or {}).get('decision', {})
        return cls(
            caller_text=caller_text,
            say=getattr(resp, 'text', '') or '',
            move=d.get('move'),
            resolved_sku=d.get('resolved_sku'),
            resolution=d.get('resolution', 'none'),
            candidates=tuple(d.get('candidates') or ()),
            account_established=bool(d.get('account_established')),
            disclosed=bool(d.get('disclosed')),
            refused=d.get('refused'),
            escalated=bool(d.get('escalated')),
        )

    def exercised(self) -> set:
        """The decisions this turn ACTUALLY made — only these generate label
        questions. A not-exercised decision is never asked about (no guess-noise)."""
        ex = set()
        if self.resolved_sku or self.candidates or \
                self.resolution in ('identified', 'ambiguous'):
            ex.add('resolution')              # a phrase -> SKU resolution happened
        if self.move == 'price':
            ex.add('pricing_gate')            # a pricing/gate decision occurred
        if self.move == 'availability' and self.disclosed:
            ex.add('availability')            # an availability disclosure occurred
        if self.escalated:
            ex.add('escalation')              # an escalation choice was made
        if self.say:
            ex.add('quality')                 # there is an utterance to judge (quarantined)
        return ex
