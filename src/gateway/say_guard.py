"""Structural guard: internal resolution state must never reach the spoken say.

This is the deterministic twin of the provenance-completeness invariant. There,
a say stating a binding fact must structurally carry its provenance; here, a say
reaching the caller must structurally NOT carry the resolver's internal working
state — BM25 scores, source tags, confidence values, or the internal taxonomy
codes (family/body/finish codes like SB/EX/XB, K/BH/BR) the resolver uses to
narrow a match.

Why a structural guard and not a manual reword: two such leaks (a BM25 score and
a taxonomy-code option list) reached a caller through the gateway's own `say` in
the live adversarial run — gateway-authored, not the model. The model never
fabricated them; the say-generation layer rendered its working state into spoken
text. Rewording the two known sites fixes today; a guard on the say fixes the
class, so the next internal field someone threads into a `reason` or an option
list is caught HERE (in CI, over real say outputs) instead of in another
adversarial run. BM25-score and taxonomy-code are its first two tests.

It is collision-safe with the legitimate spelled-SKU readback (`spoken_sku`
renders 'K5-24SBC' as 'K 5, 24 S B C' — space-separated single letters, never a
parenthesized comma-list of multi-letter codes), so it does not false-positive on
a real part number spoken back for confirmation.
"""
from __future__ import annotations

import logging
import re

from gateway.spoken import voice_render

_log = logging.getLogger(__name__)

# Last-resort spoken line if a say ever fails the guard at runtime. We never
# speak internal state and never crash a live call: we escalate. Post-fix this
# is unreachable on real says — it exists so a future regression degrades to a
# safe hand-off instead of leaking, while CI catches the regression loudly.
_LEAK_FALLBACK = "Let me get a rep to confirm that for you — one moment."

# BM25 / score / source-tag markers. Matches BOTH the raw say ('bm25 score 9.3')
# and the voice-rendered say (voice_render spells 'BM25' -> 'B M 25' because it
# contains a digit). The word 'score' and a 'bm25'/'b m 25' fragment never belong
# in caller speech, so banning them outright is safe.
_SCORE = re.compile(r'\bbm\s*-?\s*25\b|\bb\s+m\s+25\b|\bscore\b', re.I)

# An internal-code OPTION LIST: a parenthesized group of >=2 comma-separated
# short uppercase-initial codes, e.g. '(SB, EX, XB)', '(A, SS)', '(K, BH, BR, A)'.
# A spelled SKU is never in this shape (it isn't parenthesized, and its letters
# are space-separated, not comma-separated), and a human description parenthetical
# is lowercase, so neither matches.
_CODE_ENUM = re.compile(
    r'\(\s*[A-Z][A-Z0-9]{0,3}(?:\s*,\s*[A-Z][A-Z0-9]{0,3}){1,}\s*\)')

# On-hand QUANTITY (CONVERSATION_STATE_SPEC §7, invariant 5): availability is
# BOOLEAN — the on-hand count is internal state and must never be spoken, caught
# here the same way BM25 scores and taxonomy codes are. The signal is a number
# ADJACENT to on-hand language ("58 on hand", "58 in stock", "we have 58",
# "qty: 58") — NOT a bare "in stock" (boolean, fine) and NOT ship-times/dimensions/
# prices ("5 PM", "5 days", "24 inch", "42 dollars"), whose trailing words are not
# in the on-hand set, and not the spelled SKU (its digits aren't adjacent to the
# on-hand words).
_QTY = re.compile(
    r'\b\d+\s+(?:on hand|in stock|on the shelf|left|remaining|in inventory|'
    r'units?|pcs|pieces?)\b'
    r'|\b(?:qty|quantity)\b\s*[:=#-]?\s*\d+'
    r"|\b(?:we have|i have|there are|we'?ve got|we stock|got)\s+\d+\b", re.I)


class InternalStateLeak(ValueError):
    """A say carried internal resolution state into the spoken channel."""


def internal_state_tokens(say: str) -> list[str]:
    """Return the internal-state fragments found in a spoken say (empty == clean).
    Pure; safe to call on the final voice-rendered string."""
    if not say:
        return []
    hits = [m.group() for m in _SCORE.finditer(say)]
    hits += [m.group() for m in _CODE_ENUM.finditer(say)]
    hits += [m.group() for m in _QTY.finditer(say)]
    return hits


def assert_no_internal_state(say: str) -> str:
    """Raise if the say carries internal resolution state; else return it. The
    structural CI gate over real say outputs — the analogue of assert_complete."""
    hits = internal_state_tokens(say)
    if hits:
        raise InternalStateLeak(
            f'say carries internal resolution state {hits!r}: {say!r}')
    return say


def safe_voice_say(text: str) -> str:
    """The runtime say boundary: voice-render the gateway's say, then enforce the
    internal-state guard FAIL-SAFE. On a (post-fix unreachable) leak we log loudly
    and hand off rather than speak internal state or crash the call — fail-closed,
    same as the rest of the gateway. Every spoken path routes through here so the
    guard covers agent / Twilio / TTS uniformly."""
    rendered = voice_render(text)
    hits = internal_state_tokens(rendered)
    if hits:
        _log.warning('internal-state leak suppressed in say: %r in %r',
                     hits, rendered)
        return _LEAK_FALLBACK
    return rendered
