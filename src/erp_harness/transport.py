"""Transport boundary: the protocols the enforcer wraps.

The harness never talks to an ERP directly — exploration code holds a
SafetyEnforcer (enforcer.py), the enforcer holds a Backend, and the Backend
is the ONLY thing that knows how bytes move. In CI the Backend is the
in-process twin; in production it is an HTTPS client adapter. Nothing in
erp_harness imports a network stack — tests/test_harness_purity.py proves it.

Clock is injected for the same reason DST got golden tests in fulfillment:
time-dependent behavior (token buckets, backoff, consistency probes) must be
deterministic under test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class TransportRequest:
    method: str
    path: str                                  # e.g. 'items', '$metadata'
    params: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    json: Any | None = None
    text: str | None = None


class TransportTimeout(Exception):
    """A backend exceeded its deadline (R2 #8). A production HTTPS backend
    sets a socket timeout and raises this; the enforcer journals it and
    treats it as a retryable transient, never a hang. The in-process twin
    never raises it — the contract exists for real backends."""


class Backend(Protocol):
    """Where requests go after the enforcer approves them. Implementations
    MUST raise TransportTimeout rather than block indefinitely (R2 #8)."""
    def handle(self, req: TransportRequest) -> TransportResponse: ...


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class ManualClock:
    """Deterministic clock for tests and the twin: sleep() advances time."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError('cannot sleep negative seconds')
        self._t += seconds

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)
