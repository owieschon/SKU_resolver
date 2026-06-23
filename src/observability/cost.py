"""Cost ledger + per-session budget.

Adapted from a prior agent stack's LLM module cost-logging portions, stripped of the
task-tier specifics. The voice gateway needs the per-session budget pattern:
a long adversarial call must not rack up unbounded model spend.

Append-only JSONL; B0-style id threading (run_id / session_id / tenant_id) on
every row so cost is joinable to spans and to a specific conversation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CostEvent:
    ts: str                 # caller-supplied ISO stamp (no Date.now in lib)
    task: str
    model: str
    cost_usd: float
    in_tokens: int = 0
    out_tokens: int = 0
    latency_s: float = 0.0
    ok: bool = True
    session_id: str = ''
    run_id: str = ''
    tenant_id: str = ''

    def as_row(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v != ''}


class BudgetExceeded(Exception):
    def __init__(self, scope: str, spent: float, limit: float):
        self.scope, self.spent, self.limit = scope, spent, limit
        super().__init__(
            f'{scope} cost budget exceeded: ${spent:.4f} >= ${limit:.2f}')


@dataclass
class CostLedger:
    """JSONL-backed cost ledger with budget queries. Path is injected so the
    harness, gateway, and tests each own their own ledger file."""
    path: Path
    rows: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.path = Path(self.path)
        if self.path.exists():
            self.rows = [json.loads(l) for l in
                         self.path.read_text().splitlines() if l.strip()]

    def record(self, event: CostEvent) -> None:
        row = event.as_row()
        self.rows.append(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a') as f:
            f.write(json.dumps(row) + '\n')

    def spent_for_session(self, session_id: str) -> float:
        return sum(r.get('cost_usd', 0.0) for r in self.rows
                   if r.get('session_id') == session_id)

    def spent_for_task(self, task: str) -> float:
        return sum(r.get('cost_usd', 0.0) for r in self.rows
                   if r.get('task') == task)

    def enforce_session_budget(self, session_id: str, limit: float) -> None:
        """Raise BudgetExceeded if this session is already at/over budget.
        Call BEFORE an expensive operation; the cap is hard, not advisory."""
        spent = self.spent_for_session(session_id)
        if spent >= limit:
            raise BudgetExceeded(f'session:{session_id}', spent, limit)


def anomaly_flags(event: CostEvent, thresholds: dict[str, float]) -> dict[str, dict]:
    """Return {field: {value, threshold}} for each threshold the event
    exceeds — a cheap injection/runaway tripwire (e.g. cost_usd, out_tokens)."""
    out = {}
    for fld, limit in thresholds.items():
        val = getattr(event, fld, None)
        if isinstance(val, (int, float)) and val > limit:
            out[fld] = {'value': val, 'threshold': limit}
    return out
