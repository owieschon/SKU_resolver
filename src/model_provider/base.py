"""Provider-agnostic LLM seam. The model only ever PROPOSES; deterministic
code still binds — so a provider being down degrades to the rule-based
fallback rather than breaking the system, and never weakens never-invent.

`ModelProvider` is the one interface every adapter implements. CI uses
`ScriptedProvider`; production uses Anthropic / OpenAI / OpenRouter adapters
(each provider-pure, in its own module, imported lazily so this package
pulls no SDK at import).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelRequest:
    task: str                       # routing key (intent, retrieval_select, ...)
    system: str
    user: str
    model: str                      # resolved model id (routing decides this)
    max_tokens: int = 1024
    json_schema: dict | None = None  # when set, response must validate to it


@dataclass(frozen=True)
class ModelResponse:
    text: str
    data: Any | None                # parsed object when json_schema was given
    model: str
    provider: str
    in_tokens: int = 0
    out_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


class ModelUnavailable(Exception):
    """Raised when a provider call fails (no key, network, rate limit after
    retries). Callers catch this and fall back to the deterministic path —
    the system keeps working, degraded, never broken."""


class ModelProvider(Protocol):
    name: str
    def complete(self, req: ModelRequest) -> ModelResponse: ...
