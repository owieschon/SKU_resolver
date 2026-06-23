"""Retrieval chooser seam (P2). The fallback's candidate list can optionally
be collapsed to ONE pick by an LLM chooser — the validated 88.2%-conditional
selection step. The model proposes; deterministic code binds:

    a chosen SKU is accepted ONLY if it is one of the retrieved candidates
    (which are, by construction, real catalog rows).

So even a hallucinating model cannot make the chooser emit a non-catalog SKU
— never-invent holds through the LLM path. Default is NoChooser (the D5
propose-never-resolve behavior); CI runs an LLM chooser backed by ScriptedProvider.
"""
from __future__ import annotations

from typing import Protocol

from model_provider import LLMClient, ModelUnavailable
from resolution.retrieval import RetrievedCandidate

_CHOOSER_SCHEMA = {
    'type': 'object',
    'properties': {
        'sku': {'type': 'string',
                'description': 'the single best-matching SKU, copied EXACTLY '
                               'from the candidate list, or empty if none fit'},
    },
    'required': ['sku'],
    'additionalProperties': False,
}


class Chooser(Protocol):
    def choose(self, text: str,
               candidates: list[RetrievedCandidate]) -> str | None: ...


class NoChooser:
    """Default: never collapses to a pick (D5). The fallback stays
    propose-only — a human disambiguates."""
    def choose(self, text, candidates):
        return None


class LLMChooser:
    """Asks the model to pick one candidate. Binds the result ONLY if it is in
    the candidate set; otherwise returns None (falls back to the picker)."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def choose(self, text: str,
               candidates: list[RetrievedCandidate]) -> str | None:
        if not candidates:
            return None
        allowed = {c.sku for c in candidates}
        listing = '\n'.join(f'- {c.sku}: {c.description}' for c in candidates)
        try:
            resp = self._llm.propose(
                task='retrieval_select',
                system=('You select the single catalog SKU that best matches a '
                        'customer request. Choose ONLY from the provided '
                        'candidates; copy the SKU exactly. If none fit, return '
                        'an empty sku.'),
                user=f'Request: {text!r}\n\nCandidates:\n{listing}',
                json_schema=_CHOOSER_SCHEMA, max_tokens=128)
        except ModelUnavailable:
            return None                  # graceful: fall back to the picker
        pick = (resp.data or {}).get('sku') if resp.data else None
        # BIND-GUARD: accept only a real candidate. A hallucinated or empty
        # pick is rejected here — never-invent holds through the model.
        return pick if pick in allowed else None
