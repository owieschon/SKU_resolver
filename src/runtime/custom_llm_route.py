"""The custom-LLM seam, mounted: `POST /v1/chat/completions`.

ElevenLabs points its Agent's LLM at this OpenAI-compatible endpoint (via
`custom_llm{url, model_id, api_key}`); we own the seam and run the containment
brain (`custom_llm.handle_async`) before any audio. The brain SUBSTITUTES the
gateway say on tool-result turns (model not invoked) and filters free turns; this
route is only transport + instrumentation + the live model wiring.

Three properties this module is responsible for, all of which the thread's
discipline demands BEFORE the live agent is pointed here:

  * INSTRUMENT-THEN-EXPOSE: per-route latency AND the decision trace are emitted
    on EVERY call from the very first one — a telemetry span plus an append-only
    JSONL ledger (CUSTOM_LLM_TRACE_LOG) the B-probing reads to characterize the
    two latency populations (substitution = fast; free/model = fat-tailed).
  * REAL DEADLINE, REAL ABORT: the live model_fn is ASYNC (make_async_model_fn),
    so when budget B fires the in-flight request is cancelled at the source, not
    left running in the background. B is the MODEL-route budget by design:
    handle_async checks substitution FIRST and never subjects it to the deadline,
    so the gateway route is not held hostage to the model's jitter.
  * FAIL-CLOSED EVERYWHERE: missing key / model error / over-budget all degrade
    to the deterministic fallback; substitution turns keep working without a key.

B (CUSTOM_LLM_BUDGET_SECS) is PROVISIONAL here — a first-probe value above the
observed model tail. It is tightened only after B-probing characterizes both the
model's tail under load and ElevenLabs' own degradation threshold.
"""
# NB: no `from __future__ import annotations` — FastAPI reads real types.
import json
import logging
import os
import time

from runtime.custom_llm import handle_async
from observability import telemetry

_log = logging.getLogger(__name__)

# Provisional first-probe budget: above the 6.28s model tail observed in the
# live adversarial re-run. Tightened post-probe against both distributions.
_DEFAULT_BUDGET_SECS = 8.0

telemetry.register_structured(
    'clm.route', 'clm.decision', 'clm.latency_s', 'clm.wall_s',
    'clm.model_invoked', 'clm.over_budget', 'clm.stream', 'clm.fallback_used')


def _budget_secs() -> float:
    try:
        return float(os.environ.get('CUSTOM_LLM_BUDGET_SECS', _DEFAULT_BUDGET_SECS))
    except (TypeError, ValueError):
        return _DEFAULT_BUDGET_SECS


def _trace_log_path():
    return os.environ.get('CUSTOM_LLM_TRACE_LOG') or None


def _append_ledger(path, record: dict) -> None:
    """Append one decision/latency record as JSONL. Never raises into the hot
    path — instrumentation must not be able to fail a call."""
    if not path:
        return
    try:
        with open(path, 'a') as fh:
            fh.write(json.dumps(record) + '\n')
    except Exception:                              # pragma: no cover - best effort
        _log.warning('custom-llm trace ledger write failed', exc_info=True)


def _emit(trace: dict, *, stream: bool, wall_s: float) -> dict:
    """Per-route latency + decision trace -> telemetry span + JSONL ledger. The
    record is intentionally scalar-only (no transcript text) so it is safe to keep
    forever and feed straight into the B-probing distribution analysis."""
    record = {
        'route': trace.get('route'),
        'decision': trace.get('decision'),
        'latency_secs': trace.get('latency_secs'),   # endpoint processing time
        'wall_secs': round(wall_s, 4),               # incl. transport/auth/parse
        'model_invoked': bool(trace.get('model_invoked')),
        'over_budget': bool(trace.get('over_budget')),
        'fallback_used': bool(trace.get('fallback_used')),
        'stream': stream,
    }
    span = telemetry.tracer.start_span('custom_llm.turn')
    try:
        telemetry.set_attr(span, 'clm.route', record['route'])
        telemetry.set_attr(span, 'clm.decision', record['decision'])
        telemetry.set_attr(span, 'clm.latency_s', record['latency_secs'])
        telemetry.set_attr(span, 'clm.wall_s', record['wall_secs'])
        telemetry.set_attr(span, 'clm.model_invoked', record['model_invoked'])
        telemetry.set_attr(span, 'clm.over_budget', record['over_budget'])
        telemetry.set_attr(span, 'clm.fallback_used', record['fallback_used'])
        telemetry.set_attr(span, 'clm.stream', record['stream'])
    finally:
        span.end()
    _append_ledger(_trace_log_path(), record)
    return record


def _wrap_completion(resp: dict, *, model_id: str, created: int, cid: str) -> dict:
    """Stamp the OpenAI envelope fields map_response leaves off."""
    out = dict(resp)
    out['id'] = cid
    out['created'] = created
    out['model'] = model_id
    return out


def _sse(resp: dict, *, model_id: str, created: int, cid: str):
    """Buffer-don't-stream, in OpenAI streaming clothes: we have already filtered
    the complete reply, so we emit it as role + one content/tool_calls delta + a
    terminal finish chunk + [DONE]. No partial fact content is ever streamed."""
    msg = resp['choices'][0]['message']
    base = {'id': cid, 'object': 'chat.completion.chunk', 'created': created,
            'model': model_id}

    def chunk(delta, finish):
        c = dict(base)
        c['choices'] = [{'index': 0, 'delta': delta, 'finish_reason': finish}]
        return f'data: {json.dumps(c)}\n\n'

    yield chunk({'role': 'assistant'}, None)
    if msg.get('tool_calls'):
        tcs = [dict(tc, index=tc.get('index', i))
               for i, tc in enumerate(msg['tool_calls'])]
        yield chunk({'tool_calls': tcs}, None)
        yield chunk({}, 'tool_calls')
    else:
        if msg.get('content'):
            yield chunk({'content': msg['content']}, None)
        yield chunk({}, resp['choices'][0].get('finish_reason', 'stop'))
    yield 'data: [DONE]\n\n'


def _resolve_model_fn(state):
    """Build the live ASYNC model_fn once, lazily (so app boot needs no key and
    tests inject their own). On build failure we install a model_fn that raises —
    handle_async fail-closes free turns to fallback while substitution keeps
    working — and log it ONCE, loudly: a missing key is an operator error."""
    if 'model_fn' in state:
        return state['model_fn']
    injected = state.get('injected')
    if injected is not None:
        state['model_fn'] = injected
        return injected
    try:
        from runtime.openrouter_model import make_async_model_fn
        state['model_fn'] = make_async_model_fn(request_timeout=_budget_secs())
    except Exception as e:
        _log.error('custom-llm live model_fn unavailable (%s) — free turns will '
                   'fail-closed to fallback; substitution still works', e)

        async def _unavailable(_messages):
            raise RuntimeError('custom-llm model_fn unavailable')
        state['model_fn'] = _unavailable
    return state['model_fn']


def register_custom_llm(app, *, model_fn=None, budget_secs=None):
    """Mount `POST /v1/chat/completions`. `model_fn` injects a model for tests;
    in production it is built lazily from the OpenRouter key. `budget_secs`
    overrides the env/provisional B (the model-route deadline)."""
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse

    state = {'injected': model_fn}
    _counter = {'n': 0}

    @app.post('/v1/chat/completions')
    async def chat_completions(request: Request):
        t0 = time.monotonic()
        # Auth: ElevenLabs sends the configured custom_llm.api_key as a bearer.
        # Fails open only when CUSTOM_LLM_API_KEY is unset (dev), mirroring the
        # /agent/turn and twilio_sig posture.
        expected = os.environ.get('CUSTOM_LLM_API_KEY')
        if expected:
            import hmac
            auth = request.headers.get('Authorization', '')
            token = auth[7:] if auth.startswith('Bearer ') else ''
            if not hmac.compare_digest(token, expected):
                return JSONResponse({'error': 'forbidden'}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({'error': 'invalid JSON'}, status_code=400)

        stream = bool(body.get('stream'))
        model_id = str(body.get('model') or 'sku-contained')
        b = budget_secs if budget_secs is not None else _budget_secs()
        mf = _resolve_model_fn(state)

        resp, trace = await handle_async(body, model_fn=mf, budget_secs=b)
        wall_s = time.monotonic() - t0
        _emit(trace, stream=stream, wall_s=wall_s)

        _counter['n'] += 1
        cid = f'chatcmpl-clm-{int(time.time() * 1000)}-{_counter["n"]}'
        created = int(time.time())
        if stream:
            return StreamingResponse(
                _sse(resp, model_id=model_id, created=created, cid=cid),
                media_type='text/event-stream')
        return JSONResponse(
            _wrap_completion(resp, model_id=model_id, created=created, cid=cid))

    return chat_completions
