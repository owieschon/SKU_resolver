"""G6 — voice connector. ASR is a protocol (D9-style seam): CI runs
SimulatedASR; production uses an AssemblyAIStreaming adapter. Gateway logic is
identical under both — the confirmation protocol (#11) lives in the
orchestrator, not the ASR layer, so a degraded transcript can never become a
silent wrong identification.

Twilio + AssemblyAI is the validated primary stack (the H1 call-capture arc).
Credentials load from the environment at runtime (locations documented in the
spec); NOTHING credential-bearing lives in this module or the repo. The live
adapter is exercised only by the credential-gated smoke suite, never CI.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float          # 0..1 word-level mean from the ASR
    is_final: bool = True


class ASR(Protocol):
    def transcribe(self, audio_ref: str) -> Transcript: ...


@dataclass
class SimulatedASR:
    """Deterministic ASR for CI: maps an audio ref to a scripted transcript +
    confidence. Replays H1-style confusion cases (e.g. a degraded utterance
    that lands on a near-neighbour SKU) so the orchestrator's readback defense
    is exercised without real audio."""
    script: dict[str, Transcript]

    def transcribe(self, audio_ref: str) -> Transcript:
        return self.script.get(audio_ref,
                               Transcript(text='', confidence=0.0))


# Word-confidence floor: below this, the gateway asks the caller to repeat
# rather than attempt a low-confidence resolve (spec G6 DoD).
CONFIDENCE_FLOOR = 0.45


def transcript_is_usable(t: Transcript) -> bool:
    return bool(t.text.strip()) and t.confidence >= CONFIDENCE_FLOOR


def keyterms_from_catalog(catalog, limit: int = 500) -> list[str]:
    """Catalog-derived keyterms for ASR boosting (the H1-validated lever).
    Family words + a sample of SKUs; the boost-hallucination risk it creates
    is defended by the orchestrator's discriminating readback, not here."""
    terms: list[str] = []
    for row in catalog.parsed_rows():
        terms.append(row.sku)
        if len(terms) >= limit:
            break
    return terms
