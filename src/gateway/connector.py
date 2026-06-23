"""G1/G7 — the integration surface: a tool-calling contract + a signed webhook.

`tools.json` is the function-calling manifest CS/voice platforms (SymphonyAI-
class CCaaS, agent runtimes) consume to call the gateway as a tool — the
"most common connection format" answer. It is generated from the same source
as the turn API, so the two never drift.

The webhook adapter verifies an HMAC-SHA256 signature (constant-time) with
replay defense (nonce + timestamp window), then dispatches to the orchestrator
exactly as a direct API turn would — byte-equivalent path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from gateway.models import Channel
from gateway.orchestrator import Gateway

REPLAY_WINDOW_SECONDS = 300


def tools_manifest() -> dict[str, Any]:
    """The function-calling projection of the gateway. One tool: submit a
    conversational turn. Platforms call this; their runtime owns ASR/TTS."""
    return {
        'name': 'sku_service_turn',
        'description': ('Answer a customer-service turn about part '
                        'availability, lead time, and (after account '
                        'verification) pricing.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'session_id': {'type': 'string'},
                'token': {'type': 'string'},
                'text': {'type': 'string',
                         'description': 'the customer utterance or message'},
                'channel': {'type': 'string', 'enum': ['typed', 'voice']},
            },
            'required': ['session_id', 'token', 'text', 'channel'],
        },
    }


@dataclass
class WebhookConnector:
    gateway: Gateway
    secret: bytes
    now_fn: Callable[[], float]
    _seen_nonces: set = field(default_factory=set)

    def _sign(self, body: bytes) -> str:
        return hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    def handle(self, raw_body: bytes, signature: str, *, nonce: str,
               ts: float) -> dict[str, Any]:
        """Verify signature + replay window, then dispatch. Returns a JSON-able
        TurnResponse dict, or a typed error dict (never raises to the caller)."""
        if not hmac.compare_digest(self._sign(raw_body), signature or ''):
            return {'error': 'signature_invalid'}
        if abs(self.now_fn() - ts) > REPLAY_WINDOW_SECONDS:
            return {'error': 'timestamp_outside_window'}
        if nonce in self._seen_nonces:
            return {'error': 'replay_detected'}     # idempotent: one effect only
        self._seen_nonces.add(nonce)
        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError):
            return {'error': 'malformed_body'}
        resp = self.gateway.turn(
            payload['session_id'], payload['token'], payload['text'],
            channel=Channel(payload.get('channel', 'typed')))
        return _response_to_dict(resp)


def _response_to_dict(resp) -> dict[str, Any]:
    out = {'kind': resp.kind, 'text': resp.text,
           'session_state': resp.session_state,
           'needs_confirmation': resp.needs_confirmation}
    if resp.candidates:
        out['candidates'] = [{'sku': c.sku, 'reason': c.reason}
                             for c in resp.candidates]
    if resp.availability:
        a = resp.availability
        out['availability'] = {'sku': a.sku, 'in_stock': a.in_stock,
                               'ship_by': a.ship_by_iso, 'basis': a.basis,
                               'catalog_version': a.catalog_version}
    if resp.price:
        out['price'] = {'sku': resp.price.sku, 'unit_price': resp.price.unit_price,
                        'account_id': resp.price.account_id,
                        'source': resp.price.source}
    if resp.escalation:
        out['escalation'] = {'reason': resp.escalation.reason,
                             'summary': resp.escalation.summary,
                             'action': resp.escalation.action}
    if resp.refused:
        out['refused'] = resp.refused
    return out
