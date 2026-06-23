"""Anthropic (direct) provider — provider-pure, anthropic SDK only.

Uses the Messages API with output_config json_schema for structured proposals
(the claude-api-reference-recommended path). The SDK is imported lazily so
this package stays network-free at import; a missing SDK or key surfaces as
ModelUnavailable, which callers fall back from.

Pricing table is per-1M tokens (claude-api reference, 2026-06) so the cost
ledger is populated for cross-model comparison.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from model_provider.base import (
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
)
from model_provider.keyring import key_for

# $/1M (input, output)
_PRICES = {
    'claude-haiku-4-5': (1.0, 5.0),
    'claude-sonnet-4-6': (3.0, 15.0),
    'claude-opus-4-8': (5.0, 25.0),
}


def _cost(model: str, in_tok: int, out_tok: int) -> float:
    pin, pout = _PRICES.get(model, (0.0, 0.0))
    return round(in_tok / 1e6 * pin + out_tok / 1e6 * pout, 6)


@dataclass
class AnthropicProvider:
    name: str = 'anthropic'
    clock: object = None        # injectable for latency timing/tests

    def complete(self, req: ModelRequest) -> ModelResponse:
        if not key_for('anthropic'):
            raise ModelUnavailable('no ANTHROPIC_API_KEY configured')
        try:
            import anthropic
        except ImportError as e:
            raise ModelUnavailable('anthropic SDK not installed') from e

        client = anthropic.Anthropic()   # key from env (never logged)
        kwargs = dict(model=req.model, max_tokens=req.max_tokens,
                      system=req.system,
                      messages=[{'role': 'user', 'content': req.user}])
        if req.json_schema is not None:
            kwargs['output_config'] = {
                'format': {'type': 'json_schema', 'schema': req.json_schema}}
        try:
            msg = client.messages.create(**kwargs)
        except Exception as e:                 # network, rate-limit, 4xx
            raise ModelUnavailable(f'anthropic call failed: {e}') from e

        return parse_anthropic_response(msg, req, provider=self.name)


def parse_anthropic_response(msg, req: ModelRequest, *,
                             provider: str = 'anthropic') -> ModelResponse:
    """Map an Anthropic Messages response object to a ModelResponse. Pure (no
    network) so the parsing — text-block extraction, structured-output JSON
    decode, token/cost mapping — is unit-tested against synthetic payloads, not
    only run live. Tolerates missing/empty content and non-text blocks."""
    text = next((b.text for b in (msg.content or [])
                 if getattr(b, 'type', None) == 'text'), '')
    data = None
    if req.json_schema is not None:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
    u = getattr(msg, 'usage', None)
    in_tok = getattr(u, 'input_tokens', 0) or 0
    out_tok = getattr(u, 'output_tokens', 0) or 0
    return ModelResponse(
        text=text, data=data, model=req.model, provider=provider,
        in_tokens=in_tok, out_tokens=out_tok,
        cost_usd=_cost(req.model, in_tok, out_tok))
