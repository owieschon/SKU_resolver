"""G3 — SKU identification from typed/spoken text, confirmation-gated.

Inherits never-invent: a SKU only reaches a response via the resolution
service. Voice-channel identifications require a DISCRIMINATING readback
(#11) before they count; a bare yes/no is a WEAK signal, insufficient for a
high-consequence (pricing) path. Anaphora (#14) resolves a referring
expression against the session's recent-SKU context, then still confirms.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from gateway.models import (
    Candidate,
    Channel,
    ConfirmationStrength,
    IdentifiedSKU,
)
from gateway.spoken import spoken_description, to_spoken
from resolution import Resolution, ResolutionService

# Short, response-shaped affirmations only (borrowed from a prior agent's conservative
# confirmation-signal gate): a long sentence with "yes" buried in it is NOT a
# confirmation. Discriminating affirmations (naming an attribute) are detected
# separately by readback matching.
_AFFIRM = re.compile(r'^\s*(yes|yep|yeah|yup|correct|right|that\'?s? (it|right)|'
                     r'sure|ok|okay)\s*[.!]?\s*$', re.I)
_ANAPHORA = re.compile(r'\b(that|the)\b.{0,30}\b(one|stack|part|sku)\b', re.I)


@dataclass(frozen=True)
class IdentificationOutcome:
    state: str                 # 'identified' | 'needs_confirmation' | 'candidates' | 'unresolvable'
    identified: IdentifiedSKU | None = None
    candidates: tuple[Candidate, ...] = ()
    readback: str | None = None      # the discriminating question for voice
    open_questions: tuple = ()        # resolution.OpenQuestion — informed-disambig surface


def _readback_for(resolution: Resolution, catalog) -> str:
    """Build a discriminating readback (#11). The SKU's attributes are already
    DECODED by the grammar (diameter, length, body, finish) — so STATE them back
    for the caller to ratify or correct, rather than asking them to recite a
    diameter/finish the part number already encodes. This keeps the near-neighbour
    defense (a garbled K5-26 would be read back as '6 inch' and get corrected)
    while sounding like a person, not an interrogation."""
    sku = resolution.sku
    row = catalog.lookup(sku) if hasattr(catalog, 'lookup') else None
    spoken = spoken_description(row)
    if spoken:
        return f"I have {sku} — that's {spoken}. Is that the one?"
    desc = to_spoken((row.description if row else '') or sku)
    return f"I have {sku} — {desc}. Is that the one?"


def identify(text: str, *, channel: Channel, service: ResolutionService,
             catalog, customer: str | None = None) -> IdentificationOutcome:
    res = service.resolve(text, customer=customer)

    if res.state == 'resolved':
        if channel is Channel.TYPED and res.confidence == 'high':
            return IdentificationOutcome(
                'identified',
                identified=IdentifiedSKU(res.sku, confirmed=True,
                                         strength=ConfirmationStrength.DISCRIMINATING,
                                         source=res.source))
        # Voice (any confidence) or non-high typed -> require readback first.
        return IdentificationOutcome(
            'needs_confirmation',
            identified=IdentifiedSKU(res.sku, confirmed=False,
                                     strength=ConfirmationStrength.NONE,
                                     source=res.source),
            readback=_readback_for(res, catalog))

    if res.state == 'pending_disambiguation':
        return IdentificationOutcome(
            'candidates',
            candidates=tuple(Candidate(c.sku, c.reason) for c in res.candidates),
            open_questions=tuple(res.open_questions))

    return IdentificationOutcome('unresolvable')


def _attribute_vocab(row) -> set[str]:
    """Discriminating-attribute tokens for a catalog row: the DECODED parser
    meanings (finish='Chrome', family='Curved-top stack', diameter=5) plus the
    raw description. Using the decoded meanings is what lets a caller saying
    'chrome' match a SKU whose description abbreviates it 'CHR' (#11)."""
    vocab: set[str] = set()
    if row is None:
        return vocab
    parsed = getattr(row, 'raw_parser_result', {}) or {}
    for key in ('family_meaning', 'finish_meaning', 'body_meaning',
                'oem_meaning'):
        val = parsed.get(key)
        if val:
            vocab |= {t for t in re.findall(r'[a-z]+', str(val).lower())
                      if len(t) >= 3}
    for key in ('diameter', 'length'):
        val = parsed.get(key)
        if val is not None:
            vocab.add(str(int(val)) if float(val).is_integer() else str(val))
    vocab |= {t for t in re.findall(r'[a-z0-9]+',
                                    (row.description or '').lower()) if len(t) >= 3}
    return vocab


def classify_confirmation(text: str, *, expected_sku: str, catalog,
                          ) -> ConfirmationStrength:
    """Grade a confirmation reply (#11). A discriminating reply names an
    attribute that matches the candidate (strong); a bare affirmation is weak;
    anything else is none."""
    row = catalog.lookup(expected_sku) if hasattr(catalog, 'lookup') else None
    tokens = {t for t in re.findall(r'[a-z0-9]+', text.lower()) if len(t) >= 2}
    if tokens & _attribute_vocab(row):
        return ConfirmationStrength.DISCRIMINATING
    if _AFFIRM.match(text):
        return ConfirmationStrength.WEAK
    return ConfirmationStrength.NONE


def looks_like_anaphora(text: str) -> bool:
    return bool(_ANAPHORA.search(text)) and not re.search(r'[A-Z0-9]{2,}-?\d', text)
