#!/usr/bin/env python3
"""Agent evaluator-optimizer runner (Phase 2b) — the networked harness.

Drives the behavior catalog (voice_agent/scenarios.json) against a live ElevenLabs
agent via the `simulate-conversation` API, scores each run with the pure evaluator
(runtime/agent_eval), and prints a behavior x config matrix. This is how we tune
by MEASUREMENT instead of manual preview calls.

Method (one-factor-at-a-time, so behavior changes are attributable):
  - run the catalog at a BASELINE config, then
  - flip ONE variable and re-run the catalog; the diff is the attribution.
Don't sweep the Cartesian product (it explodes) — scale effort to complexity.

Key-gated; never runs in CI. Requirements:
  - ELEVENLABS_API_KEY (simulate-conversation)
  - an existing --agent-id whose resolve_part tool points at a reachable gateway
    (stand up /tmp/run_agent_server.py + a tunnel first; run that eval server
    WITHOUT AGENT_TOOL_SECRET so simulation can reach the tool)
  - ANTHROPIC_API_KEY for the judge oracles (optional: without it, judge oracles
    are skipped and only the deterministic oracles score)

Usage:
  python scripts/agent_grid.py --agent-id <id> --repeats 3
  python scripts/agent_grid.py --agent-id <id> --sweep llm \
      --levels gemini-2.5-flash,gemini-2.5-flash-lite,claude-haiku-4-5
"""
import argparse
import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

import certifi

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src'))

from runtime.agent_eval import (  # noqa: E402
    evaluate,
    format_results,
    load_scenarios,
    verify_frozen,
)

API = 'https://api.elevenlabs.io/v1/convai'
_CTX = ssl.create_default_context(cafile=certifi.where())


def _req(url, payload=None, method='GET', key=''):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        'xi-api-key': key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, context=_CTX, timeout=180) as resp:
        return json.loads(resp.read())


def simulate(agent_id, scenario, key, conv_id):
    """Run one scenario through simulate-conversation; return a normalized
    conversation [{role, message, tool_calls}]."""
    body = {
        'simulation_specification': {
            'simulated_user_config': {
                'persona': scenario.persona,
                'first_message': scenario.first_message,
            },
            'dynamic_variables': {'system__conversation_id': conv_id},
        },
        'new_turns_limit': 6,
    }
    data = _req(f'{API}/agents/{agent_id}/simulate-conversation',
                body, 'POST', key)
    raw = data.get('simulated_conversation') or data.get('conversation') or []
    conv = []
    for t in raw:
        conv.append({
            'role': t.get('role'),
            'message': t.get('message') or '',
            'tool_calls': [
                {'name': c.get('tool_name') or c.get('name'),
                 'params': c.get('params_as_json') or c.get('parameters') or {}}
                for c in (t.get('tool_calls') or [])],
        })
    return conv


def make_judge():
    """Return a judge_fn(prompt)->reply using Anthropic, or None if no key."""
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        print('# anthropic SDK not installed; judge oracles will be skipped',
              file=sys.stderr)
        return None
    client = anthropic.Anthropic(api_key=key)
    model = os.environ.get('JUDGE_MODEL', 'claude-opus-4-8')

    def judge(prompt: str) -> str:
        msg = client.messages.create(
            model=model, max_tokens=200,
            messages=[{'role': 'user', 'content': prompt}])
        return ''.join(b.text for b in msg.content if getattr(b, 'type', '') == 'text')
    return judge


def patch_llm(agent_id, llm, key):
    """OFAT: flip just the agent's LLM (the cheapest variable to sweep)."""
    _req(f'{API}/agents/{agent_id}',
         {'conversation_config': {'agent': {'prompt': {'llm': llm}}}}, 'PATCH', key)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--agent-id', required=True)
    ap.add_argument('--repeats', type=int, default=3,
                    help='runs per scenario per config (LLMs are stochastic)')
    ap.add_argument('--sweep', default='', help="variable to sweep, e.g. 'llm'")
    ap.add_argument('--levels', default='',
                    help='comma-separated levels for --sweep')
    ap.add_argument('--split', default='dev',
                    choices=['dev', 'frozen_visible', 'frozen_holdout'],
                    help="dev=tune; frozen_visible=gate; frozen_holdout=promotion only")
    ap.add_argument('--out', default='state/agent_grid_results.md')
    args = ap.parse_args()

    key = os.environ.get('ELEVENLABS_API_KEY')
    if not key:
        print('ELEVENLABS_API_KEY required', file=sys.stderr)
        return 1
    # The frozen sets must be intact before they can judge anything.
    if args.split.startswith('frozen'):
        bad = verify_frozen()
        if bad:
            print('FROZEN EVAL TAMPERED — refusing to run the gate:', file=sys.stderr)
            for m in bad:
                print(f'  {m}', file=sys.stderr)
            return 2
    # Hold-out discipline: aggregate-only, never per-case content.
    holdout = args.split == 'frozen_holdout'
    scenarios = load_scenarios(args.split)
    judge = make_judge()
    print(f'# {len(scenarios)} scenarios x {args.repeats} repeats | '
          f'judge={"on" if judge else "OFF (deterministic only)"}', file=sys.stderr)

    cells = [('baseline', None)]
    if args.sweep == 'llm' and args.levels:
        cells = [(f'llm={lv}', lv) for lv in args.levels.split(',')]

    rows = []
    for label, level in cells:
        if level is not None:
            patch_llm(args.agent_id, level, key)
            print(f'## config {label}', file=sys.stderr)
        for sc in scenarios:
            passes = 0
            last_reason = ''
            skipped = False
            for i in range(args.repeats):
                conv = simulate(args.agent_id, sc, key, f'{sc.id}-{label}-{i}')
                v = evaluate(sc, conv, judge_fn=judge)
                if v.method == 'skipped':           # judge oracle, no judge this pass
                    skipped = True
                    last_reason = v.reason
                    break
                passes += 1 if v.passed else 0
                last_reason = v.reason
            rate = passes / args.repeats
            # Hold-out: keep the per-case REASON out of results (it leaks case
            # content); record only the pass count. Visible/dev keep the reason.
            reason = (f'{passes}/{args.repeats}' if holdout
                      else f'{passes}/{args.repeats} | {last_reason}')
            rows.append({'scenario': sc.id if not holdout else f'holdout#{len(rows)}',
                         'config': label, 'passed': rate == 1.0,
                         'skipped': skipped, 'reason': reason})
            if not holdout:
                tag = 'skip(no judge)' if skipped else f'{passes}/{args.repeats}'
                print(f'  {sc.id:38s} {label:24s} {tag}', file=sys.stderr)

    if holdout:
        # aggregate pass-rate per config only — never per-case detail
        by_cfg = {}
        for r in rows:
            by_cfg.setdefault(r['config'], []).append(r['passed'])
        matrix = '\n'.join(f'{c}: {sum(v)}/{len(v)} holdout cases pass'
                           for c, v in by_cfg.items())
        detail = '(hold-out: per-case detail intentionally withheld)'
    else:
        matrix = format_results(rows)
        detail = '\n'.join(f'- {r["scenario"]} [{r["config"]}]: {r["reason"]}'
                           for r in rows if not r['passed'])
    out = REPO / args.out
    out.write_text(f'# Agent grid results\n\n{matrix}\n\n## failures / flakiness\n{detail}\n')
    print(f'\nwrote {out}', file=sys.stderr)
    print(matrix)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
