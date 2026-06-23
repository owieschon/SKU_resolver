"""LLMClient — the one entry point the seams call.

Ties together: routing (which model for this task), the active provider, the
cost ledger (every call logged with model/provider/tokens/cost for cross-model
comparison — this is what makes observability model-adaptable), and a span.
Returns a typed proposal; raises ModelUnavailable so the seam can fall back.

The model only proposes here. The seam's deterministic verifier still binds.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from model_provider.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from model_provider.routing import ModelChoice, resolve_model

try:                                  # observability is optional at this layer
    from observability import set_attr, tracer
    from observability.cost import CostEvent, CostLedger
    _OBS = True
except Exception:                     # pragma: no cover
    _OBS = False


@dataclass
class LLMClient:
    provider: ModelProvider
    cost_ledger: 'CostLedger | None' = None
    now_iso: Callable[[], str] | None = None
    session_id: str = ''

    def propose(self, *, task: str, system: str, user: str,
                json_schema: dict | None = None, max_tokens: int = 1024,
                override_model: str | None = None) -> ModelResponse:
        """Route -> call -> log. Raises ModelUnavailable on any failure; the
        caller is expected to fall back to its deterministic path."""
        choice: ModelChoice = resolve_model(task, self.provider.name,
                                            override=override_model)
        req = ModelRequest(task=task, system=system, user=user,
                           model=choice.model, max_tokens=max_tokens,
                           json_schema=json_schema)
        span = (tracer.start_as_current_span(f'llm.{task}')
                if _OBS else _NullCtx())
        with span as sp:
            if _OBS:
                set_attr(sp, 'svc.task', task)
                set_attr(sp, 'llm.model_name', choice.model)
                set_attr(sp, 'llm.provider', self.provider.name)
                set_attr(sp, 'svc.source', choice.source)
            resp = self.provider.complete(req)   # may raise ModelUnavailable
            if _OBS:
                set_attr(sp, 'llm.cost.total', resp.cost_usd)
            self._log(task, resp)
            return resp

    def _log(self, task: str, resp: ModelResponse) -> None:
        if self.cost_ledger is None or self.now_iso is None:
            return
        self.cost_ledger.record(CostEvent(
            ts=self.now_iso(), task=task, model=resp.model,
            cost_usd=resp.cost_usd, in_tokens=resp.in_tokens,
            out_tokens=resp.out_tokens, session_id=self.session_id))


class _NullCtx:
    def __enter__(self): return None
    def __exit__(self, *a): return False
