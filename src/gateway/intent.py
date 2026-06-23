"""Intent classification seam (P2). The gateway's turn router was a pile of
regexes — brittle to real phrasing. This makes classification a swappable
seam: `RuleBasedIntentRouter` (CI default, reproduces the regex logic exactly)
and `LLMIntentRouter` (production, real language understanding). Either way the
GATES still bind — the router only decides which handler to dispatch; pricing
verification, never-invent, and escalation enforcement are unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from gateway import escalation
from gateway.models import Escalation

# Regex set lifted verbatim from the orchestrator so RuleBased == old behavior.
_PRICE_RE = re.compile(r'\b(price|cost|how much|quote|pricing)\b', re.I)
_AVAIL_RE = re.compile(r'\b(in stock|available|availability|lead time|'
                       r'when can|ship|how many|on hand)\b', re.I)
_VERIFY_RE = re.compile(r'\b(account|acct)\b', re.I)
_ACCT_NO_RE = re.compile(r'\b(?:account|acct)\s*(?:no\.?|number|#)?\s*[:#]?\s*'
                         r'(\d{3,12})\b', re.I)


class Intent(Enum):
    HANDOFF = 'handoff'            # escalate (explicit request or out-of-scope)
    VERIFY = 'verify'
    PRICING = 'pricing'
    AVAILABILITY = 'availability'  # default: identify/availability flow


@dataclass(frozen=True)
class IntentDecision:
    intent: Intent
    escalation: Escalation | None = None   # set when intent is HANDOFF


def _wants_verify(text: str) -> bool:
    return bool(re.search(r'\b(verify|my account|account (number|no|#|name)|'
                          r'here\'?s my account)\b', text, re.I)) \
        or bool(_ACCT_NO_RE.search(text))


def _has_sku_shape(text: str) -> bool:
    return bool(re.search(r'\b[A-Za-z]{1,4}\d', text))


class IntentRouter(Protocol):
    def classify(self, text: str) -> IntentDecision: ...


class RuleBasedIntentRouter:
    """Deterministic classifier — same priority order as the original inline
    routing: explicit handoff / explicit out-of-scope -> verify -> pricing ->
    no-signal out-of-scope -> availability."""

    name = 'rule_based'

    def classify(self, text: str) -> IntentDecision:
        esc = (escalation.explicit_handoff(text)
               or escalation.explicit_out_of_scope(text))
        if esc is not None:
            return IntentDecision(Intent.HANDOFF, esc)
        if _VERIFY_RE.search(text) and _wants_verify(text):
            return IntentDecision(Intent.VERIFY)
        if _PRICE_RE.search(text):
            return IntentDecision(Intent.PRICING)
        esc = escalation.no_signal_out_of_scope(
            text, has_sku_shape=_has_sku_shape(text))
        if esc is not None:
            return IntentDecision(Intent.HANDOFF, esc)
        return IntentDecision(Intent.AVAILABILITY)


_INTENT_SCHEMA = {
    'type': 'object',
    'properties': {
        'intent': {'type': 'string',
                   'enum': ['handoff', 'verify', 'pricing', 'availability']},
        'reason': {'type': 'string'},
    },
    'required': ['intent', 'reason'],
    'additionalProperties': False,
}


class LLMIntentRouter:
    """Production classifier. The model reads the turn and returns a structured
    intent; this handles the messy phrasing regexes miss (informed
    disambiguation and out-of-scope detection both improve). Falls back to the
    rule-based router if the model is unavailable — the system degrades, never
    breaks. The classification only picks a handler; every gate still binds."""

    name = 'llm'

    def __init__(self, llm, fallback: IntentRouter | None = None) -> None:
        self._llm = llm
        self._fallback = fallback or RuleBasedIntentRouter()

    def classify(self, text: str) -> IntentDecision:
        from model_provider import ModelUnavailable
        try:
            resp = self._llm.propose(
                task='intent',
                system=('Classify a customer-service turn for an industrial '
                        'parts desk into exactly one intent: "pricing" (asking '
                        'a price), "verify" (giving an account number/name), '
                        '"availability" (asking about a part, stock, or lead '
                        'time, including free-text part descriptions), or '
                        '"handoff" (anything else — billing, orders, returns, '
                        'wanting a human).'),
                user=text, json_schema=_INTENT_SCHEMA, max_tokens=128)
        except ModelUnavailable:
            return self._fallback.classify(text)
        data = resp.data or {}
        try:
            intent = Intent(data.get('intent', 'availability'))
        except ValueError:
            return self._fallback.classify(text)
        esc = None
        if intent is Intent.HANDOFF:
            esc = Escalation(reason='out_of_scope',
                             summary=data.get('reason', 'model: out of scope'))
        return IntentDecision(intent, esc)
