"""The three destinations — quarantined from each other by construction. Each
store accepts ONLY its own label type (a wrong-typed label raises), and none of
them is the commit: they populate CANDIDATE pools from which the gate is later
built by a separate, curated act. The pilot populates; the eval still commits.

The isolation invariants, structural:
  * RESOLUTION labels -> correction CANDIDATES, never live aliases (promotion still
    requires clearing the frozen eval + no-regression — approval is not the commit).
  * BEHAVIORAL labels -> a DEV candidate pool, NEVER frozen-visible/holdout: the
    human who labeled the call has now SEEN it, and holdout's whole point is that
    nothing in the optimization path has seen it. Promotion into the frozen sets is
    a separate, deliberate, curated act (this store offers no method to do it).
  * QUALITY labels -> quarantine, which has NO reader that feeds the eval or the
    CorrectionStore.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pilot.labeling import Label, LabelType


class WrongLabelType(ValueError):
    """A label was routed to the wrong store — the contamination boundary tripped."""


@dataclass
class CorrectionCandidateQueue:
    """RESOLUTION labels -> phrase->SKU correction CANDIDATES. Candidates only; the
    CorrectionStore still gates promotion on the frozen eval + no-regression. A
    rep's thumbs-up never writes live resolution from here."""
    candidates: list = field(default_factory=list)

    def add_candidate(self, label: Label) -> None:
        if label.label_type is not LabelType.RESOLUTION:
            raise WrongLabelType(
                f'correction queue takes RESOLUTION, got {label.label_type}')
        self.candidates.append(label)


@dataclass
class EvalCandidatePool:
    """BEHAVIORAL labels -> a DEV candidate pool. Structurally dev-only: there is NO
    method here to write frozen-visible or holdout, because a labeled (human-seen)
    call must not auto-populate the gate it would later be measured against."""
    dev: list = field(default_factory=list)

    def add_candidate(self, label: Label) -> None:
        if label.label_type is not LabelType.BEHAVIORAL:
            raise WrongLabelType(
                f'eval pool takes BEHAVIORAL, got {label.label_type}')
        self.dev.append(label)


@dataclass
class QualityQuarantine:
    """QUALITY labels -> quarantine. Captured (it's how naturalness is calibrated)
    but never read into the eval or the CorrectionStore — no such reader exists."""
    items: list = field(default_factory=list)

    def add(self, label: Label) -> None:
        if label.label_type is not LabelType.QUALITY:
            raise WrongLabelType(
                f'quality quarantine takes QUALITY, got {label.label_type}')
        self.items.append(label)


@dataclass
class LabelStores:
    """The three destinations, bundled. There is deliberately NO cross-store
    transfer method: a label enters exactly one store and the stores never feed
    each other — the quarantine is the absence of a path, not a check that can be
    forgotten."""
    corrections: CorrectionCandidateQueue = field(default_factory=CorrectionCandidateQueue)
    eval_pool: EvalCandidatePool = field(default_factory=EvalCandidatePool)
    quality: QualityQuarantine = field(default_factory=QualityQuarantine)
