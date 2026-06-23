"""Deterministic (scripted) provider for CI and tests — no network, no keys, no cost.

Two modes:
- scripted: a dict mapping task -> response object (or callable(req)->obj),
  for golden tests of the seams.
- echo: returns a trivial deterministic structure, for smoke paths.

This is what keeps CI fast, free, and reproducible while the seams still run
their full LLM-shaped code path (request building, response parsing, the
deterministic verification that binds the proposal).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from model_provider.base import ModelRequest, ModelResponse, ModelUnavailable


@dataclass
class ScriptedProvider:
    name: str = 'scripted'
    # task -> object | callable(req)->object. Returned as ModelResponse.data.
    scripted: dict[str, Any] = field(default_factory=dict)
    fail_tasks: set = field(default_factory=set)   # simulate provider outage
    calls: list = field(default_factory=list)

    def complete(self, req: ModelRequest) -> ModelResponse:
        self.calls.append(req)
        if req.task in self.fail_tasks:
            raise ModelUnavailable(f'scripted outage for task {req.task!r}')
        val = self.scripted.get(req.task)
        if callable(val):
            val = val(req)
        return ModelResponse(text=str(val), data=val, model=req.model,
                             provider=self.name, in_tokens=10, out_tokens=5,
                             cost_usd=0.0)
