"""Scripted-harness driver — proves the contained ENDPOINT before exposure.

This is the deterministic baseline the thread's discipline demands: prove it, then
expose it. It drives the custom-LLM endpoint (`custom_llm.handle`) with FULLY
SCRIPTED user turns and the REAL gateway as the tool executor — no ElevenLabs, no
improvising simulator. The model is injected (an adversarial mock here to prove
containment deterministically; the real model swaps in to produce the live-model
adversarial results, which is the only thing that proves the mock->real transition
opened nothing).

run_turn() walks the OpenAI-style loop ourselves: user message -> handle() ->
if the model asks for a tool, execute the real gateway and feed the result back
-> handle() again (which SUBSTITUTES the gateway say) -> the spoken text. Every
handle() decision trace is collected, so an assertion of containment can read the
proof, not infer it.
"""
from __future__ import annotations

import json

from gateway import Channel
from gateway.provenance import surfaced
from gateway.say_guard import safe_voice_say
from runtime.custom_llm import handle


def _exec_tool(gw, sid, tok, tool_call) -> dict:
    """Run resolve_part on the REAL gateway -> the /agent/turn-shaped result the
    endpoint would receive as a tool message's content."""
    fn = tool_call.get('function') or {}
    args = fn.get('arguments') or '{}'
    try:
        text = json.loads(args).get('text', '')
    except (json.JSONDecodeError, TypeError):
        text = ''
    resp = gw.converse(sid, tok, text, channel=Channel.TYPED)   # orchestration backend
    skus, values = surfaced(resp)
    return {'say': safe_voice_say(resp.text), 'kind': resp.kind,
            'surfaced_skus': list(skus), 'surfaced_values': values}


def run_turn(messages: list[dict], *, gw, sid, tok, model_fn,
             max_tool_hops: int = 3) -> tuple[str, list[dict]]:
    """Advance one caller turn to the agent's spoken text, executing real gateway
    tool calls in between. Returns (spoken_text, [decision traces])."""
    traces = []
    for _ in range(max_tool_hops + 1):
        resp, trace = handle({'messages': messages}, model_fn=model_fn)
        traces.append(trace)
        msg = resp['choices'][0]['message']
        if msg.get('tool_calls'):
            # the model asked for a lookup -> execute it on the real gateway and
            # feed the result back; the next handle() will SUBSTITUTE the say.
            messages = messages + [
                {'role': 'assistant', 'content': None, 'tool_calls': msg['tool_calls']},
                {'role': 'tool', 'tool_call_id': msg['tool_calls'][0].get('id', 't'),
                 'content': json.dumps(_exec_tool(gw, sid, tok, msg['tool_calls'][0]))}]
            continue
        return msg.get('content') or '', traces
    return msg.get('content') or '', traces
