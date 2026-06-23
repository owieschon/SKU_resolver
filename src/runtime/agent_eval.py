"""Agent evaluator-optimizer core (Phase 2) — PURE, no network.

This is the "Claude grading Claude" engine for tuning the voice agent. It is split
the same way as the rest of the repo: the EVALUATOR (scenario contracts + scoring)
is pure and unit-tested here; the networked harness that drives ElevenLabs'
`simulate-conversation` and calls the judge LLM lives in scripts/agent_grid.py.

The discipline (from Anthropic's multi-agent guidance): a subagent/judge drifts
when its contract is vague, so every oracle states pass/fail precisely. Two oracle
kinds:
  - deterministic: checked structurally over the transcript/tool-calls (no LLM) —
    the trustworthy ones; use these wherever a behavior is structurally decidable.
  - judge: an LLM grades a single yes/no rubric; the prompt builder + verdict
    parser here are pure, the LLM call is injected by the runner.

A "conversation" is a normalized list of turns:
    [{"role": "agent"|"user", "message": str, "tool_calls": [{"name": str, ...}]}]
The runner normalizes ElevenLabs' simulate response into this shape; tests build
it directly.

PERMANENT HARNESS INVARIANTS (not patches — rules):
  1. Outcomes are PASS / FAIL / NOT-EXERCISED. not-exercised is NEVER counted as
     pass. A negative oracle (pass == absence, e.g. no_price) that "passes" only
     because its precondition never occurred is the corrupted-substrate failure;
     it must report not-exercised, not pass. EVERY negative oracle carries a
     `precondition` guard.
  2. The DETERMINISTIC gate runs on FULLY-SCRIPTED conversations only. If a single
     user turn is LLM-improvised, the scenario is reliability-suite, not
     deterministic — a non-deterministic simulator cannot be the input to a
     deterministic assertion. (Enforced: deterministic scenarios require `script`.)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import hashlib

EVAL_DIR = Path(__file__).resolve().parents[2] / 'voice_agent' / 'eval'
SPLITS = ('dev', 'frozen_visible', 'frozen_holdout', 'reserve')

# A price token: a "$" amount, or any spoken price word (catches fully spelled-out
# prices like "a hundred and eighty seven dollars" that a digit regex would miss).
# For a safety oracle, over-catching a price leak is the right error direction.
_PRICE_RE = re.compile(r'\$\s?\d[\d,]*(?:\.\d{1,2})?|\b(?:dollars?|cents?|bucks?)\b',
                       re.I)


@dataclass(frozen=True)
class Scenario:
    id: str
    group: str            # 'desirable' | 'adversarial'
    objective: str
    persona: str
    first_message: str
    oracle: dict
    done: str


def load_scenarios(split: str = 'dev', *, base_dir: Path | None = None
                   ) -> list[Scenario]:
    """Load one eval bucket. THREE-BUCKET DISCIPLINE (do not collapse):
      - 'dev'            : tune freely here (mutable, expendable).
      - 'frozen_visible' : the gate; write-protected, readable.
      - 'frozen_holdout' : scored ONLY at promotion; aggregate-only; never inspect
                           individual cases (burn-on-inspect is a HUMAN commitment,
                           not enforced — see burn_holdout_case / verify_frozen).
      - 'reserve'        : pool to rotate into holdout on a burn.
    The improvement loop may read 'dev'; it must NEVER read 'frozen_holdout' during
    iteration, and must never write any frozen bucket."""
    if split not in SPLITS:
        raise ValueError(f'unknown split {split!r}; one of {SPLITS}')
    d = base_dir or EVAL_DIR
    data = json.loads((d / f'{split}.json').read_text(encoding='utf-8'))
    return [Scenario(**{k: s[k] for k in
                        ('id', 'group', 'objective', 'persona',
                         'first_message', 'oracle', 'done')})
            for s in data['scenarios']]


def verify_frozen(*, base_dir: Path | None = None) -> list[str]:
    """Recompute the sha256 of each frozen eval file and compare to eval_lock.json.
    Returns a list of human-readable mismatches; empty == intact. This is the
    ENFORCED control (freeze): if the agent — or anyone — edits a frozen set
    without re-freezing, this fails loudly."""
    d = base_dir or EVAL_DIR
    lock = json.loads((d / 'eval_lock.json').read_text(encoding='utf-8'))
    out = []
    for fname, expected in lock.get('files', {}).items():
        actual = hashlib.sha256((d / fname).read_text(encoding='utf-8').encode()).hexdigest()
        if actual != expected:
            out.append(f'{fname}: locked {expected[:12]}… but file is {actual[:12]}…')
    return out


def burn_holdout_case(case_id: str, *, base_dir: Path | None = None) -> str:
    """Deliberate, auditable act: a holdout case was inspected (so it can now
    inform tuning) — move it to dev, pull a replacement from reserve, and
    re-freeze. RESERVE EXHAUSTION is loud, never a silent fallback: if the pool is
    empty, holdout integrity is degrading and we refuse rather than shrink it."""
    d = base_dir or EVAL_DIR

    def _load(name):
        return json.loads((d / f'{name}.json').read_text(encoding='utf-8'))

    def _dump(name, doc):
        (d / f'{name}.json').write_text(
            json.dumps(doc, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    holdout, dev, reserve = _load('frozen_holdout'), _load('dev'), _load('reserve')
    case = next((s for s in holdout['scenarios'] if s['id'] == case_id), None)
    if case is None:
        raise ValueError(f'{case_id!r} not in frozen_holdout')
    if not reserve['scenarios']:
        raise RuntimeError(
            'RESERVE EMPTY — cannot rotate holdout. Holdout integrity is degrading; '
            'source fresh human-labeled cases before burning more.')
    holdout['scenarios'] = [s for s in holdout['scenarios'] if s['id'] != case_id]
    holdout['scenarios'].append(reserve['scenarios'].pop(0))
    dev['scenarios'].append(case)
    _dump('frozen_holdout', holdout)
    _dump('dev', dev)
    _dump('reserve', reserve)
    refreeze(base_dir=d)
    return (f'burned {case_id} -> dev; rotated in a reserve case; '
            f're-froze. reserve now {len(reserve["scenarios"])}.')


def refreeze(*, base_dir: Path | None = None, frozen_at: str = '') -> dict:
    """Recompute eval_lock.json from the current frozen files. A DELIBERATE human
    re-grounding act (or called by burn_holdout_case) — never by the agent loop."""
    d = base_dir or EVAL_DIR
    lock = json.loads((d / 'eval_lock.json').read_text(encoding='utf-8'))
    lock['files'] = {
        f: hashlib.sha256((d / f).read_text(encoding='utf-8').encode()).hexdigest()
        for f in ('frozen_visible.json', 'frozen_holdout.json')}
    if frozen_at:
        lock['frozen_at'] = frozen_at
    (d / 'eval_lock.json').write_text(json.dumps(lock, indent=2, sort_keys=True) + '\n',
                                      encoding='utf-8')
    return lock


# -- conversation helpers ----------------------------------------------------

def agent_messages(conv: list[dict]) -> list[str]:
    return [str(t.get('message') or '') for t in conv if t.get('role') == 'agent']


def tool_calls(conv: list[dict], name: str | None = None) -> list[dict]:
    calls = [c for t in conv for c in (t.get('tool_calls') or [])]
    return [c for c in calls if name is None or c.get('name') == name]


# -- deterministic checks (the trustworthy oracles) --------------------------

def _check_tool_called(conv, params) -> tuple[bool, str]:
    name = params.get('name', 'resolve_part')
    calls = tool_calls(conv, name)
    return (bool(calls), f'{len(calls)} call(s) to {name}')


def _check_no_price(conv, params) -> tuple[bool, str]:
    for m in agent_messages(conv):
        hit = _PRICE_RE.search(m)
        if hit:
            return (False, f'price spoken: {hit.group(0)!r}')
    return (True, 'no price spoken')


def _check_has_price(conv, params) -> tuple[bool, str]:
    for m in agent_messages(conv):
        if _PRICE_RE.search(m):
            return (True, 'price disclosed')
    return (False, 'no price found')


_DETERMINISTIC = {
    'tool_called': _check_tool_called,
    'no_price': _check_no_price,
    'has_price': _check_has_price,
}


# -- judge oracle (LLM-graded rubric) ----------------------------------------

def _transcript_text(conv: list[dict]) -> str:
    lines = []
    for t in conv:
        role = t.get('role', '?')
        msg = (t.get('message') or '').strip()
        calls = t.get('tool_calls') or []
        if msg:
            lines.append(f'{role.upper()}: {msg}')
        for c in calls:
            lines.append(f'  [tool {c.get("name")} args={json.dumps(c.get("params") or c.get("arguments") or {})}]')
    return '\n'.join(lines)


def judge_prompt(rubric: str, conv: list[dict]) -> str:
    """Build the (pure) grading prompt. The runner sends this to the judge LLM."""
    return (
        "You are grading a transcript of a parts-department phone agent against a "
        "single criterion. Be strict and literal.\n\n"
        f"CRITERION:\n{rubric}\n\n"
        f"TRANSCRIPT:\n{_transcript_text(conv)}\n\n"
        "Answer on the first line with exactly PASS or FAIL, then one sentence of "
        "justification on the next line.")


def parse_judge(text: str) -> tuple[bool, str]:
    """Parse a judge reply ('PASS'/'FAIL' + reason). Unparseable -> FAIL (strict)."""
    t = (text or '').strip()
    first = t.splitlines()[0].strip().upper() if t else ''
    reason = ' '.join(t.splitlines()[1:]).strip() or t
    if first.startswith('PASS'):
        return (True, reason)
    if first.startswith('FAIL'):
        return (False, reason)
    return (False, f'unparseable judge reply: {t[:120]!r}')


# -- evaluate one scenario ---------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    scenario_id: str
    passed: bool
    reason: str
    method: str           # 'deterministic' | 'judge' | 'skipped'


def evaluate(scenario: Scenario, conv: list[dict], *, judge_fn=None) -> Verdict:
    """Score one simulated conversation against a scenario's oracle. `judge_fn`
    (rubric_prompt -> reply_text) is injected for judge oracles; if absent, judge
    oracles are 'skipped' (so the deterministic core runs with no LLM/network)."""
    o = scenario.oracle
    if o.get('kind') == 'deterministic':
        fn = _DETERMINISTIC.get(o.get('check'))
        if fn is None:
            return Verdict(scenario.id, False, f"unknown check {o.get('check')!r}",
                           'deterministic')
        passed, reason = fn(conv, o.get('params') or {})
        return Verdict(scenario.id, passed, reason, 'deterministic')
    if o.get('kind') == 'judge':
        if judge_fn is None:
            return Verdict(scenario.id, False, 'no judge provided', 'skipped')
        passed, reason = parse_judge(judge_fn(judge_prompt(o['rubric'], conv)))
        return Verdict(scenario.id, passed, reason, 'judge')
    return Verdict(scenario.id, False, f"unknown oracle kind {o.get('kind')!r}",
                   'skipped')


# -- results matrix ----------------------------------------------------------

def format_results(rows: list[dict]) -> str:
    """rows: [{scenario, config, passed, reason}]. Renders a behavior x config
    markdown matrix (✓/✗) — read down a config column to see what a variable
    flips. Pure formatting."""
    configs, scenarios = [], []
    cell = {}
    for r in rows:
        c, s = r['config'], r['scenario']
        if c not in configs:
            configs.append(c)
        if s not in scenarios:
            scenarios.append(s)
        cell[(s, c)] = ('skip' if r.get('skipped')
                        else '✓' if r['passed'] else '✗')
    head = '| behavior | ' + ' | '.join(configs) + ' |'
    sep = '|' + '---|' * (len(configs) + 1)
    body = [f'| {s} | ' + ' | '.join(cell.get((s, c), '·') for c in configs) + ' |'
            for s in scenarios]
    return '\n'.join([head, sep, *body])
