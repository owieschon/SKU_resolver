"""The labeling boundary — three label types, three destinations, kept from
contaminating each other. This is the eval-isolation discipline applied to the
human instead of the optimizer: a human in the pilot is NOT labeling "was this call
good" (no single destination, corrupts everything it touches). There are three
distinct things a human could label, with three trust profiles and three routes:

  RESOLUTION  did "the big chrome stack" resolve to K5-24SBC?  -> CorrectionStore
              candidate (checkable against the catalog; the strong, exogenous kind).
  BEHAVIORAL  did the agent do the right THING — escalate vs resolve, gate price,
              ask vs guess?  -> eval candidate pool (DEV, never straight to frozen).
  QUALITY     was the phrasing natural, the question well-formed?  -> a QUARANTINE
              store that NEVER feeds the eval or the CorrectionStore.

The contamination failures this prevents: a "tone was off" QUALITY judgment flowing
into the eval as a behavioral fail corrupts the accuracy number into a mix of "did
the right thing" and "sounded nice" — the unmeasurable mush you can't show a
customer. And a RESOLUTION thumbs-up writing a live alias reopens approval-is-the-
commit. The separation is the two boundaries already drawn — facts vs prose,
approval vs commit — applied to the labeler.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pilot.decision_point import DecisionPoint


class LabelType(Enum):
    RESOLUTION = 'resolution'      # phrase->SKU  -> CorrectionStore candidate
    BEHAVIORAL = 'behavioral'      # right move?  -> eval candidate pool (dev)
    QUALITY = 'quality'            # naturalness  -> quarantine (never the loop)


class Provenance(Enum):
    """A labeler is a NOISY INSTRUMENT, not ground truth — weight by provenance, the
    same confidence design as an alias. A rep's quick thumbs-up is acquiescence-grade
    (the weak signal of a caller saying yes to a confirmation); an explicit
    correction is stronger; the gold tier is downstream reality — the order was
    placed and not returned. An accuracy number built on acquiescence thumbs-ups is
    not defensible; one anchored in order-placed-not-returned is."""
    ACQUIESCENCE = 'acquiescence'  # rep thumbs-up
    CORRECTION = 'correction'      # explicit "no, it should have been X"
    GOLD = 'gold'                  # downstream reality (consequence-grounded)


PROVENANCE_WEIGHT = {
    Provenance.ACQUIESCENCE: 0.2,
    Provenance.CORRECTION: 0.6,
    Provenance.GOLD: 1.0,
}


@dataclass(frozen=True)
class Question:
    key: str
    label_type: LabelType
    prompt: str


@dataclass(frozen=True)
class Label:
    key: str
    label_type: LabelType
    value: object                  # pass/fail, the corrected SKU, a quality note...
    provenance: Provenance
    # shadow-mode tags (empty for direct/propose labels). Carry the per-stream
    # divergence context so the eval can tell an AUTONOMOUS resolution label
    # (agent's own path) from a CONDITIONAL one (resolved given human-supplied
    # context) — letting conditional masquerade as autonomous inflates the agent's
    # standalone accuracy. Also carries the rep_self_comparison bias flag on
    # behavioral labels at/after the behavioral divergence.
    tags: tuple = ()

    @property
    def weight(self) -> float:
        return PROVENANCE_WEIGHT[self.provenance]

    def has(self, tag: str) -> bool:
        return tag in self.tags


def label_questions(dp: DecisionPoint) -> list:
    """Generate the label questions for a decision point — ONLY for the decisions it
    exercised (the not-exercised trichotomy: never ask about a decision that didn't
    occur). Each question is typed, so its answer routes deterministically."""
    ex = dp.exercised()
    qs = []
    if 'resolution' in ex:
        target = dp.resolved_sku or (', '.join(dp.candidates) if dp.candidates else '?')
        qs.append(Question('resolution', LabelType.RESOLUTION,
                           f'Did "{dp.caller_text}" resolve to the right part ({target})?'))
    if 'pricing_gate' in ex:
        qs.append(Question('pricing_gate', LabelType.BEHAVIORAL,
                           'Was gating price on verification the right call here?'))
    if 'availability' in ex:
        qs.append(Question('availability', LabelType.BEHAVIORAL,
                           'Was the availability answer correct?'))
    if 'escalation' in ex:
        qs.append(Question('escalation', LabelType.BEHAVIORAL,
                           'Was escalating the right move?'))
    if 'quality' in ex:
        qs.append(Question('quality', LabelType.QUALITY,
                           'Was the phrasing natural and clear?'))
    return qs


def route(label: Label, stores) -> None:
    """Route a label to its ONE destination by type. The stores type-check on the
    way in (a wrong-typed label raises), so a QUALITY judgment can never reach the
    eval and a RESOLUTION thumbs-up can never become a live alias. Routing is the
    contamination boundary, enforced structurally."""
    if label.label_type is LabelType.RESOLUTION:
        stores.corrections.add_candidate(label)
    elif label.label_type is LabelType.BEHAVIORAL:
        stores.eval_pool.add_candidate(label)
    elif label.label_type is LabelType.QUALITY:
        stores.quality.add(label)
    else:                                              # pragma: no cover
        raise ValueError(f'unroutable label type {label.label_type}')
