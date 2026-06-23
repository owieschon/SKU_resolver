"""Custom-LLM endpoint mapper — the seam between ElevenLabs and the brain.

ElevenLabs hands us an OpenAI-compatible chat-completions request; we own the
"LLM." This module maps that request into the NORMALIZED turns the brain
(`agent_brain.decide_turn`) was proven against, runs the brain, and maps the
result back. Everything the brain proves is DOWNSTREAM of this mapping being
faithful, so the mapping is the new critical surface and it is fail-closed:

  * ROLE FIDELITY: the brain's role-typed allowlist (tool->tier1, user->tier2,
    assistant->nothing) is only as accurate as the role this mapper assigns. A
    mis-assigned role either drops real parts (service failure) or breaches the
    self-laundering boundary at the mapping layer.
  * PROVENANCE ROUND-TRIP: a `tool` message's content is the /agent/turn result
    serialized by ElevenLabs in transit; we re-parse it. surfaced_values as the
    brain receives it MUST equal what the gateway emitted, or the router routes a
    fact turn to the free path off a degraded copy.
  * MAPPING FAILURE = FAIL-CLOSED: an unrecognized/partial/unparseable payload
    returns the deterministic fallback (never best-effort partial mapping, which
    is how a tool result gets dropped). Its own trace is the early warning that
    ElevenLabs changed the format under us.
  * LATENCY BUDGET B: buffer-don't-stream adds first-token latency on fact turns;
    over-B converts a correct fact turn into a fallback, so the over-B RATE is
    watched as its own number.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time

from runtime.agent_brain import (
    FALLBACK, SERVICE_FALLBACK, apply_model_output, decide_turn,
    is_substitution_turn, substitution,
)


class MappingError(Exception):
    """The request couldn't be faithfully mapped — handled as fail-closed."""


# Keys the gateway's /agent/turn result must carry to be a usable tool message.
_REQUIRED_TOOL_KEYS = ('say', 'surfaced_skus', 'surfaced_values')


def parse_tool_content(content) -> dict:
    """Re-parse a tool message's content (a serialized /agent/turn result) and
    verify provenance survived the round trip. Strict: a degraded/partial result
    must raise, not be silently accepted."""
    if isinstance(content, dict):
        obj = content
    elif isinstance(content, str):
        try:
            obj = json.loads(content)
        except (json.JSONDecodeError, TypeError) as e:
            raise MappingError(f'tool content not JSON: {e}')
    else:
        raise MappingError(f'tool content has unexpected type {type(content).__name__}')
    missing = [k for k in _REQUIRED_TOOL_KEYS if k not in obj]
    if missing:
        raise MappingError(f'tool result missing keys {missing}')
    if not isinstance(obj.get('surfaced_values'), dict) or \
       not isinstance(obj.get('surfaced_skus'), list):
        raise MappingError('tool result provenance has wrong type')
    return obj


def map_request(body: dict) -> list[dict]:
    """OpenAI chat-completions body -> normalized brain turns. Role-typed; raises
    MappingError on anything it can't faithfully represent."""
    if not isinstance(body, dict):
        raise MappingError('request body is not an object')
    messages = body.get('messages')
    if not isinstance(messages, list) or not messages:
        raise MappingError('messages missing or not a non-empty list')
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict) or 'role' not in m:
            raise MappingError('message missing role')
        role = m['role']
        if role == 'system':
            continue                                  # not part of routing/allowlist
        if role == 'user':
            out.append({'role': 'user', 'content': m.get('content') or ''})
        elif role == 'assistant':
            # assistant content contributes NOTHING to the allowlist (by type);
            # we still carry it so the model has context. tool_calls pass opaque.
            out.append({'role': 'assistant', 'content': m.get('content') or ''})
        elif role == 'tool':
            out.append({'role': 'tool', 'content': m.get('content') or '',
                        'result': parse_tool_content(m.get('content'))})
        else:
            raise MappingError(f'unknown message role {role!r}')
    if not out:
        raise MappingError('no mappable (non-system) messages')
    return out


def map_response(decided) -> dict:
    """Brain output -> OpenAI chat-completions response."""
    if isinstance(decided, dict) and 'tool_call' in decided:
        msg = {'role': 'assistant', 'content': None, 'tool_calls': decided['tool_call']}
    else:
        msg = {'role': 'assistant', 'content': str(decided)}
    return {'object': 'chat.completion',
            'choices': [{'index': 0, 'message': msg, 'finish_reason': 'stop'}]}


def handle(body: dict, *, model_fn, fallback: str = FALLBACK,
           budget_secs: float = 6.0) -> tuple[dict, dict]:
    """Map -> decide -> map back. Returns (openai_response, trace). Fail-closed on
    mapping error, model error, filter error, and over-budget."""
    # Wall-clock from request-received, so EVERY route is timed (incl. mapping
    # errors and substitution turns, which never invoke the model but still have
    # latency on the hot path). Tag by route -> per-route tails watched separately.
    t0 = time.monotonic()
    try:
        normalized = map_request(body)
    except MappingError as e:
        # The seam ElevenLabs can break under us: never best-effort partial map.
        # No model output -> topic unknown -> service fallback (not part-number).
        return (map_response(SERVICE_FALLBACK),
                {'route': 'mapping_error', 'decision': 'BLOCK', 'reason': str(e),
                 'fallback_used': True, 'latency_secs': round(time.monotonic() - t0, 4)})
    # The free-turn model must run WITH its system prompt. We drop `system` from
    # the brain's ROUTING/allowlist turns (not allowlist-relevant), but the model
    # call gets the ORIGINAL messages (system included) — "dropped from routing",
    # never "model runs blind".
    def _model_with_full_context(_routing_turns):
        return model_fn(body['messages'])

    decided, trace = decide_turn(normalized, model_fn=_model_with_full_context,
                                 fallback=fallback)
    elapsed = time.monotonic() - t0
    trace = dict(trace)
    trace['latency_secs'] = round(elapsed, 4)
    # over-B (SYNC path is a post-hoc check, CI only). The LIVE path uses
    # handle_async with a REAL deadline that fires while the model is still
    # running — a post-hoc check can't fire on a HUNG model (you're blocked
    # awaiting it), so it would be fail-closed in name only.
    if trace.get('model_invoked') and elapsed > budget_secs:
        trace['over_budget'] = True
        if trace.get('decision') != 'BLOCK':
            trace['decision'] = 'BLOCK'
            trace['fallback_used'] = True
            # too slow, topic unknown -> service fallback, not the part-number line
            return map_response(SERVICE_FALLBACK), trace
    return map_response(decided), trace


async def handle_async(body: dict, *, model_fn, fallback: str = FALLBACK,
                       budget_secs: float = 6.0) -> tuple[dict, dict]:
    """LIVE endpoint path: B is a REAL deadline. The model call is raced against
    budget_secs and the endpoint returns the fallback AT B even if the model
    hangs — the post-hoc elapsed-check (sync handle) can't, because a hung model
    never lets you reach the check. Substitution turns never touch the model."""
    t0 = time.monotonic()
    try:
        normalized = map_request(body)
    except MappingError as e:
        return (map_response(SERVICE_FALLBACK),
                {'route': 'mapping_error', 'decision': 'BLOCK', 'reason': str(e),
                 'fallback_used': True, 'latency_secs': round(time.monotonic() - t0, 4)})
    # INVARIANT (structural, not a tuning assumption): the substitution route is
    # exempt from the deadline B and that exemption is SAFE because this branch
    # does ZERO I/O — the gateway result is already in `normalized` (its latency
    # was spent on the prior /agent/turn hop, bounded there by ElevenLabs'
    # response_timeout_secs). A substitution turn has nothing to wait on, so a hung
    # model cannot make it hang. Do NOT add I/O to this branch: it would convert a
    # structural property into a measurement that can drift, and a substitution turn
    # could then hang while exempt from B. Proven:
    # test_correlated_load_substitution_route_stays_bounded_without_the_model.
    if is_substitution_turn(normalized):
        text, trace = substitution(normalized)
    else:
        # An ASYNC model_fn is awaited directly so wait_for's cancellation
        # propagates into the in-flight request and ABORTS it server-side (a real
        # abort). A SYNC model_fn must go through a thread, which CANNOT be
        # cancelled — fine for CI/tests, but the LIVE endpoint passes an async
        # model_fn precisely so the deadline is honored end-to-end (see
        # openrouter_model.make_async_model_fn).
        if inspect.iscoroutinefunction(model_fn):
            call = model_fn(body['messages'])
        else:
            call = asyncio.to_thread(model_fn, body['messages'])
        try:
            out = await asyncio.wait_for(call, timeout=budget_secs)
        except asyncio.TimeoutError:               # REAL deadline fired mid-flight
            # too slow, no usable output, topic unknown -> service fallback
            return (map_response(SERVICE_FALLBACK),
                    {'route': 'over_budget', 'decision': 'BLOCK', 'over_budget': True,
                     'model_invoked': True, 'fallback_used': True,
                     'latency_secs': round(time.monotonic() - t0, 4)})
        except Exception as e:                     # fail-closed
            return (map_response(SERVICE_FALLBACK),
                    {'route': 'model_error', 'decision': 'BLOCK', 'reason': str(e)[:120],
                     'model_invoked': True, 'fallback_used': True,
                     'latency_secs': round(time.monotonic() - t0, 4)})
        text, trace = apply_model_output(normalized, out, fallback)
    trace = dict(trace)
    trace['latency_secs'] = round(time.monotonic() - t0, 4)
    return map_response(text), trace
