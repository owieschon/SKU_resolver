"""OpenAI and OpenRouter providers — OpenAI-compatible Chat Completions.

Both use the `openai` SDK (provider-pure: no Anthropic SDK here). OpenRouter
is the same wire protocol at a different base_url, so one implementation
serves both — OpenRouter is the "any model" escape hatch (it can route to the
exact call-capture-validated chooser model, e.g. google/gemini-2.5-flash).

Lazy SDK import keeps the package network-free at import; missing SDK or key
surfaces as ModelUnavailable for graceful fallback. Structured output via
response_format json_schema.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from model_provider.base import ModelRequest, ModelResponse, ModelUnavailable
from model_provider.keyring import key_for

_OPENROUTER_BASE = 'https://openrouter.ai/api/v1'

# $/1M (input, output) — best-effort for the routed tier models so the cost
# ledger / live-smoke spend cap isn't silently zero. Unknown models -> (0,0)
# (logged at zero cost; tokens are still recorded).
_PRICES = {
    'gpt-5': (1.25, 10.0),
    'gpt-5-mini': (0.25, 2.0),
    'google/gemini-2.5-flash': (0.30, 2.5),
    'anthropic/claude-sonnet-4-6': (3.0, 15.0),
    'anthropic/claude-opus-4-8': (5.0, 25.0),
}


def _cost(model: str, in_tok: int, out_tok: int) -> float:
    pin, pout = _PRICES.get(model, (0.0, 0.0))
    return round(in_tok / 1e6 * pin + out_tok / 1e6 * pout, 6)


@dataclass
class _OpenAICompatProvider:
    name: str
    base_url: str | None
    key_provider: str

    def complete(self, req: ModelRequest) -> ModelResponse:
        api_key = key_for(self.key_provider)
        if not api_key:
            raise ModelUnavailable(f'no key for {self.key_provider}')
        try:
            import openai
        except ImportError as e:
            raise ModelUnavailable('openai SDK not installed') from e

        client = openai.OpenAI(api_key=api_key, base_url=self.base_url)
        messages = [{'role': 'system', 'content': req.system},
                    {'role': 'user', 'content': req.user}]
        kwargs = dict(model=req.model, max_tokens=req.max_tokens,
                      messages=messages)
        if req.json_schema is not None:
            kwargs['response_format'] = {
                'type': 'json_schema',
                'json_schema': {'name': req.task, 'schema': req.json_schema}}
        try:
            resp = client.chat.completions.create(**kwargs)  # type: ignore[call-overload]  # kwargs built dynamically
        except Exception as e:
            raise ModelUnavailable(f'{self.name} call failed: {e}') from e

        return parse_openai_response(resp, req, provider=self.name)


def parse_openai_response(resp, req: ModelRequest, *,
                          provider: str = 'openai') -> ModelResponse:
    """Map an OpenAI-compatible Chat Completions response to a ModelResponse.
    Pure (no network) so the parsing — choice/content extraction, structured
    JSON decode, token/cost mapping — is unit-tested against synthetic payloads.
    Tolerates missing choices/usage and null content."""
    choices = getattr(resp, 'choices', None) or []
    text = (getattr(choices[0].message, 'content', '') if choices else '') or ''
    data = None
    if req.json_schema is not None:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
    usage = getattr(resp, 'usage', None)
    in_tok = getattr(usage, 'prompt_tokens', 0) or 0
    out_tok = getattr(usage, 'completion_tokens', 0) or 0
    return ModelResponse(text=text, data=data, model=req.model,
                         provider=provider, in_tokens=in_tok, out_tokens=out_tok,
                         cost_usd=_cost(req.model, in_tok, out_tok))


def OpenAIProvider() -> _OpenAICompatProvider:
    return _OpenAICompatProvider(name='openai', base_url=None,
                                 key_provider='openai')


def OpenRouterProvider() -> _OpenAICompatProvider:
    return _OpenAICompatProvider(name='openrouter', base_url=_OPENROUTER_BASE,
                                 key_provider='openrouter')
