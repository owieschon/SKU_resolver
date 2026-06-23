"""Graceful degradation: detect when the gateway should hand off to a human,
and turn what the resolver already knows into an INFORMED follow-up question.

Two deterministic detectors (the P2 LLM intent layer will replace these
behind the same decision points — same return types, better judgment):

  - explicit handoff request  ("can I talk to someone")
  - out-of-scope question      (not about parts / availability / pricing)

Plus the orchestrator drives REPEATED_FAILURE (counter) and LOW_CONFIDENCE
(threshold) escalations. The informed-question builder reads the resolver's
open_questions + candidate differing-fields so the gateway asks "did you mean
the West Coast Curve, and what finish?" instead of a bare "which one?".
"""
from __future__ import annotations

import re

from gateway.models import Escalation, EscalationReason

_HANDOFF = re.compile(
    r'\b(speak|talk|connect|transfer)\b.{0,20}\b(human|person|someone|'
    r'representative|rep|agent|sales)\b|\b(human|real person|live agent)\b',
    re.I)

# Words that signal a request OUTSIDE what this gateway handles (billing,
# order status, returns, complaints, general). Deterministic placeholder for
# the LLM scope classifier (P2).
_OUT_OF_SCOPE = re.compile(
    r'\b(cancel|refund|return|returns|billing|invoice|complaint|dispute|'
    r'my order|order status|where is my|tracking|shipped yet|account balance|'
    r'warranty claim|file a claim|speak to|password|log ?in|weather)\b', re.I)

# In-scope signal: the turn is plausibly about a part / availability / price.
_IN_SCOPE = re.compile(
    r'\b(stock|available|availability|lead time|ship|price|cost|how much|'
    r'quote|part|sku|in stock|on hand|inch|"|diameter|finish|length|chrome|'
    r'stack|muffler|elbow|clamp|tube|pipe)\b', re.I)


def explicit_handoff(text: str) -> Escalation | None:
    if _HANDOFF.search(text):
        return Escalation(reason=EscalationReason.EXPLICIT_REQUEST.value,
                          summary='caller asked to speak with a person')
    return None


def explicit_out_of_scope(text: str) -> Escalation | None:
    """High-confidence non-parts intent by keyword (billing, returns, order
    status, weather...). Runs EARLY — before verify/pricing routing — because
    a phrase like 'dispute an invoice on my account' contains 'account' and
    would otherwise be mis-routed to verification."""
    if _OUT_OF_SCOPE.search(text):
        return Escalation(reason=EscalationReason.OUT_OF_SCOPE.value,
                          summary=f'request is outside parts/availability/'
                                  f'pricing: {text[:60]!r}')
    return None


def no_signal_out_of_scope(text: str, *, has_sku_shape: bool) -> Escalation | None:
    """Low-confidence: a contentful turn with NO part signal at all. Runs LATE
    (after verify/pricing routing). A free-text part description ('5 inch
    chrome curved stack') has no SKU shape but IS in scope, so we require both
    no SKU shape AND no in-scope signal before declaring out-of-scope."""
    if not has_sku_shape and not _IN_SCOPE.search(text) and len(text.split()) >= 3:
        return Escalation(reason=EscalationReason.OUT_OF_SCOPE.value,
                          summary=f'no parts/availability/pricing signal: '
                                  f'{text[:60]!r}')
    return None


def repeated_failure(summary: str = 'could not identify the part after '
                     'multiple attempts') -> Escalation:
    return Escalation(reason=EscalationReason.REPEATED_FAILURE.value,
                      summary=summary)


# -- informed-question builders -----------------------------------------------

# Render an open-question 'type' as a plain noun the caller would recognize.
# The 'type' may arrive suffixed (_unspecified / _conflict / _disambiguation);
# strip the suffix before lookup so we never speak "family conflict" at a caller.
_FIELD_LABEL = {
    'finish': 'finish', 'body': 'body style', 'length': 'length',
    'diameter': 'diameter', 'family': 'product family',
}

# Caller-safe option glosses — human words only, NEVER the internal taxonomy
# codes (SB/EX/XB, K/BH/BR, A/C/P/S3/S4/BS). A field absent here is asked by
# name alone ("what finish?"); we never read codes the caller didn't give us,
# because doing so leaks the resolver's internal vocabulary into the spoken
# channel (see docs/DECISION_LOG.md). Curated, not derived from `reason`.
_FIELD_OPTION_GLOSS = {
    'body': 'an OD-fit, an ID-fit, or a variant',
}


def _field_noun(field: str) -> str:
    base = re.sub(r'_(unspecified|conflict|disambiguation|synonym)$', '', field)
    base = re.sub(r'_(unspecified|conflict|disambiguation|synonym)$', '', base)
    return _FIELD_LABEL.get(base, base.replace('_', ' '))


def informed_question(open_questions, candidates) -> str:
    """Build a targeted follow-up from what the resolver already knows — WITHOUT
    speaking any internal resolution state. The candidate readback uses the
    caller-safe catalog `description` (never the internal `reason`, which carries
    BM25 scores and source tags); the missing-field question names the attribute
    and only ever offers human-word glosses, never the internal codes.

    Prefers naming the specific missing field(s) ('what finish?'); falls back to
    reading the candidate part numbers back with their catalog descriptions."""
    # 1. Missing-field questions (partial spec — e.g. length given, diameter not)
    if open_questions:
        parts = []
        for q in open_questions[:2]:
            base = re.sub(r'_(unspecified|conflict|disambiguation|synonym)$', '',
                          q.field)
            gloss = _FIELD_OPTION_GLOSS.get(base, '')
            parts.append(_field_noun(q.field) + (f' — {gloss}' if gloss else ''))
        fields = ' and '.join(parts)
        return f'I can narrow it down — what {fields}?'
    # 2. Read the candidate part numbers back with caller-safe descriptions
    if candidates:
        listed = []
        for c in candidates[:3]:
            desc = (getattr(c, 'description', '') or '').strip()
            listed.append(f'{c.sku} ({desc})' if desc else c.sku)
        return f'Did you mean one of these? {"; ".join(listed)}'
    return 'Could you give a bit more detail, or the part number?'
