"""Offline half of B-probing: characterize the MODEL-route latency distribution.

The live probe needs ElevenLabs pointed at the endpoint (that yields the OTHER
unknown — ElevenLabs' tolerance). But the model route's tail — the fat,
non-stationary one that lives INSIDE B (1.0s then 6.28s on the same input in the
adversarial re-run) — we can characterize now, by firing a scripted load straight
at /v1/chat/completions with the real async model and reading the per-route ledger
the route already writes. This produces one of the two distributions the cutover
needs, de-risking B before exposure; only ElevenLabs' tolerance then remains.

Run:  OPENROUTER_API_KEY=... .venv/bin/python scripts/probe_model_route.py [N]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'src')]

from fastapi.testclient import TestClient  # noqa: E402

from runtime.app import create_app  # noqa: E402

# Varied free-turn prompts: small talk (-> route=free) and part questions the
# model answers by proposing a tool_call (-> route=tool_call). Both are MODEL
# latency. A couple of tool-result turns exercise the substitution route (the
# narrow tail) for contrast.
FREE_PROMPTS = [
    'hi there', 'how are you today?', 'are you a real person?',
    'thanks, that helps', 'what can you do for me?',
    'I need a chrome stack for a Pete', 'is K5-24SBC in stock?',
    'how long to ship a curved stack?', 'do you have any bullhorn stacks?',
    "what's the lead time on a clamp?",
]
_TOOL_MSG = {'role': 'tool', 'tool_call_id': 't', 'content': json.dumps(
    {'say': 'Yep, in stock — 58 on hand.', 'surfaced_skus': ['K5-24SBC'],
     'surfaced_values': {'qty': 58}})}


def _percentiles(xs, ps=(50, 90, 95, 99)):
    if not xs:
        return {}
    s = sorted(xs)
    out = {}
    for p in ps:
        k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
        out[f'p{p}'] = round(s[k], 3)
    out['max'] = round(s[-1], 3)
    out['min'] = round(s[0], 3)
    out['n'] = len(s)
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    budget = float(os.environ.get('CUSTOM_LLM_BUDGET_SECS', 8.0))
    ledger = Path(tempfile.gettempdir()) / 'probe_model_route.jsonl'
    if ledger.exists():
        ledger.unlink()
    os.environ['CUSTOM_LLM_TRACE_LOG'] = str(ledger)
    os.environ.setdefault('CUSTOM_LLM_BUDGET_SECS', str(budget))

    c = TestClient(create_app())
    print(f'firing {n} calls (budget B={budget}s) ...')
    for i in range(n):
        prompt = FREE_PROMPTS[i % len(FREE_PROMPTS)]
        body = {'messages': [{'role': 'user', 'content': prompt}]}
        if i % 7 == 6:                                 # sprinkle substitution turns
            body['messages'].append(_TOOL_MSG)
        t0 = time.monotonic()
        c.post('/v1/chat/completions', json=body)
        if (i + 1) % 10 == 0:
            print(f'  {i + 1}/{n}  (last {round(time.monotonic() - t0, 2)}s)')

    rows = [json.loads(ln) for ln in ledger.read_text().splitlines()]
    by_route = {}
    for r in rows:
        by_route.setdefault(r['route'], []).append(r)

    print(f'\n=== per-route latency (endpoint processing, secs) over {len(rows)} calls ===')
    summary = {}
    for route, rs in sorted(by_route.items()):
        lat = [r['latency_secs'] for r in rs if r.get('latency_secs') is not None]
        over = sum(1 for r in rs if r.get('over_budget'))
        pct = _percentiles(lat)
        over_rate = round(over / len(rs), 3) if rs else 0.0
        summary[route] = {**pct, 'over_budget_rate': over_rate}
        print(f'  {route:18s} n={pct.get("n",0):3d}  p50={pct.get("p50")}  '
              f'p90={pct.get("p90")}  p95={pct.get("p95")}  p99={pct.get("p99")}  '
              f'max={pct.get("max")}  over_B={over_rate}')

    model_routes = [r for r in by_route if r in ('free', 'tool_call', 'over_budget')]
    model_lat = [r['latency_secs'] for rt in model_routes for r in by_route[rt]
                 if r.get('latency_secs') is not None]
    mp = _percentiles(model_lat)
    print('\n=== MODEL route (free+tool_call+over_budget) — the population B gates ===')
    print(f'  {mp}')
    print(f'  -> set B at a high percentile of THIS (p95={mp.get("p95")}, '
          f'p99={mp.get("p99")}, max={mp.get("max")}), bounded above by '
          f"ElevenLabs' tolerance (the live unknown).")

    out = REPO / 'docs' / 'B_PROBE_MODEL_ROUTE.json'
    out.write_text(json.dumps(
        {'n_calls': len(rows), 'budget_secs': budget, 'per_route': summary,
         'model_route_combined': mp}, indent=2))
    print(f'\nartifact -> {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
