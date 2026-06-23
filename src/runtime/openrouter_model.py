"""Real `model_fn` for the contained endpoint — OpenRouter, OpenAI-compatible.

This is the live model the mock stood in for. It exists for exactly one purpose:
to prove the mock->real transition opens nothing. The endpoint harness drives the
SAME `custom_llm.handle` loop with this swapped in for the adversarial mock; the
mock proved the plumbing deterministically, this proves the real model — TRYING to
fabricate under adversarial callers — is contained by the same environment.

Provider-pure (the `openai` SDK against OpenRouter's base_url); no Anthropic SDK
here. We route to `google/gemini-2.5-flash` by default because that is the model
the live ElevenLabs agent runs — so the containment is tested against the actual
production brain, not a proxy. The conformant return shape the brain proves
against is `str` (a free-turn reply) or `{'tool_call': [<openai tool_call dicts>]}`.

The model is given the PRODUCTION system prompt and the PRODUCTION resolve_part
tool schema (text-only; `caller_id` is an ElevenLabs dynamic variable the model
never authors — exposing it would invite the model to invent a session key). The
faithful test is production config + adversarial callers: if the model's own
guardrails hold, good; if a caller jailbreaks them, the endpoint still contains
the output. We are proving the floor, not the model.
"""
from __future__ import annotations

from pathlib import Path

from model_provider.keyring import key_for

_OPENROUTER_BASE = 'https://openrouter.ai/api/v1'
_PROD_MODEL = 'google/gemini-2.5-flash'

# The production system prompt the live agent runs (single source of truth).
_PROMPT_PATH = Path(__file__).resolve().parents[2] / 'voice_agent' / 'SYSTEM_PROMPT.md'


def production_system_prompt() -> str:
    return _PROMPT_PATH.read_text()


# The resolve_part tool in OpenAI function-calling shape. Mirrors the webhook
# tool in voice_agent.resolve_part_tool, minus the transport (url/headers) and
# minus caller_id (a dynamic variable, not a model-authored argument).
RESOLVE_PART_TOOL = {
    'type': 'function',
    'function': {
        'name': 'resolve_part',
        'description': (
            'Resolve a customer-service turn about a part: identify the part, '
            'check availability and lead time, and (only after the account is '
            'verified) disclose pricing. Returns a JSON object whose `say` field '
            'is the exact sentence to read to the caller. Call this for ANY turn '
            'involving a part, part number, availability, stock, ship date, lead '
            'time, price, account number, or verification. Never answer those '
            'from memory.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'text': {
                    'type': 'string',
                    'description': (
                        'Exactly what the caller just said, in their own words: '
                        'the part number or description, an account number, or '
                        'their yes/no to a readback. Send it as spoken — the tool '
                        'handles imperfect transcription.'),
                },
            },
            'required': ['text'],
        },
    },
}


def _to_conformant(msg) -> str | dict:
    """An OpenAI SDK message -> the brain's conformant model_fn return shape."""
    tcs = getattr(msg, 'tool_calls', None)
    if tcs:
        return {'tool_call': [
            {'id': tc.id, 'type': 'function',
             'function': {'name': tc.function.name,
                          'arguments': tc.function.arguments or '{}'}}
            for tc in tcs]}
    return msg.content or ''


def _prepend_system(messages, system_prompt):
    msgs = list(messages)
    if system_prompt and not any(
            isinstance(m, dict) and m.get('role') == 'system' for m in msgs):
        msgs = [{'role': 'system', 'content': system_prompt}] + msgs
    return msgs


def make_model_fn(*, model: str = _PROD_MODEL, inject_system: bool = True,
                  client=None):
    """Build a SYNC `model_fn(messages) -> str | {'tool_call': [...]}` backed by
    the real model. Used by the deterministic harness/CI. `inject_system` prepends
    the production prompt when the caller's message list carries none (the harness
    starts from a bare user turn, as ElevenLabs supplies the system prompt). Raises
    if no key — a missing key at build time is an operator error worth surfacing
    loudly, not silently fallback-ing.

    NOTE: the LIVE endpoint must use `make_async_model_fn` instead — a sync model_fn
    under `handle_async` runs in a thread that CANNOT be cancelled at the deadline,
    so the HTTP request keeps running (and billing) in the background after we've
    already returned the fallback. The async variant is genuinely cancellable."""
    if client is None:
        key = key_for('openrouter')
        if not key:
            raise RuntimeError('OPENROUTER_API_KEY not set')
        import openai
        client = openai.OpenAI(api_key=key, base_url=_OPENROUTER_BASE)
    system_prompt = production_system_prompt() if inject_system else None

    def model_fn(messages):
        resp = client.chat.completions.create(
            model=model, messages=_prepend_system(messages, system_prompt),
            tools=[RESOLVE_PART_TOOL], temperature=0.0)
        return _to_conformant(resp.choices[0].message)

    return model_fn


def make_async_model_fn(*, model: str = _PROD_MODEL, inject_system: bool = True,
                        request_timeout: float | None = None, client=None):
    """Build an ASYNC `model_fn(messages)` for the LIVE endpoint. Awaited directly
    under `handle_async`'s `asyncio.wait_for`, so when the deadline B fires the
    cancellation propagates into the in-flight httpx request and ABORTS the
    connection server-side — a real abort, not "return fallback while the model
    keeps running." `request_timeout` additionally caps the HTTP call at the SDK
    layer as a backstop (set it to ~B). Cancellable + bounded: the fail-closed
    deadline is honored end-to-end against the live provider, not just at our seam."""
    if client is None:
        key = key_for('openrouter')
        if not key:
            raise RuntimeError('OPENROUTER_API_KEY not set')
        import openai
        client = openai.AsyncOpenAI(api_key=key, base_url=_OPENROUTER_BASE)
    system_prompt = production_system_prompt() if inject_system else None

    async def model_fn(messages):
        kwargs = dict(model=model,
                      messages=_prepend_system(messages, system_prompt),
                      tools=[RESOLVE_PART_TOOL], temperature=0.0)
        if request_timeout is not None:
            kwargs['timeout'] = request_timeout
        resp = await client.chat.completions.create(**kwargs)
        return _to_conformant(resp.choices[0].message)

    return model_fn


__all__ = ['make_model_fn', 'make_async_model_fn', 'production_system_prompt',
           'RESOLVE_PART_TOOL']
