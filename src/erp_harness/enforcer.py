"""C2 — Rate-Budget & Safety Enforcer. Pure code; the agent's only transport.

Guarantees (each one has a planted-fault test in test_harness_c2_enforcer.py):

  READ-ONLY BY CONSTRUCTION — the public surface is `get()`. The internal
  `request()` refuses any non-GET method with a typed WriteRefused that is
  journaled BEFORE the backend is consulted: a refused write never reaches
  the wire, provable from the destination's audit log.

  BUDGETS ARE CODE — a token-bucket rate ceiling and a total-call budget.
  The explorer cannot exceed either by being clever; exhaustion raises
  BudgetExhausted for a clean partial-results halt, never a crash.

  EVERYTHING IS JOURNALED — every attempt (sent, refused, throttled,
  retried) lands in an append-only journal. C5's probes are journal
  analyses: probes measure by introspecting what the enforcer observed,
  never by bypassing it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from erp_harness.models import AuthExpiredError
from erp_harness.transport import (
    Backend, Clock, TransportRequest, TransportResponse, TransportTimeout,
)


class WriteRefused(Exception):
    def __init__(self, method: str, path: str):
        self.method, self.path = method, path
        super().__init__(
            f'{method} {path!r} refused: exploration transport is read-only '
            f'by construction (spec C2). This attempt was journaled.'
        )


class BudgetExhausted(Exception):
    def __init__(self, kind: str, limit: int):
        self.kind, self.limit = kind, limit
        super().__init__(f'{kind} budget exhausted (limit {limit}); '
                         f'halting cleanly with partial results')


@dataclass(frozen=True)
class JournalEntry:
    ts: float
    event: str            # 'sent' | 'refused_write' | 'throttled' | 'retry' | 'budget_halt'
    method: str
    path: str
    status: int | None
    attempt: int


@dataclass
class Journal:
    entries: list[JournalEntry] = field(default_factory=list)

    def record(self, **kw) -> None:
        self.entries.append(JournalEntry(**kw))

    def events(self, event: str) -> list[JournalEntry]:
        return [e for e in self.entries if e.event == event]


_ALLOWED_METHODS = frozenset({'GET'})
_RETRYABLE = frozenset({429, 503, 504})
_MAX_RETRIES = 5


def _parse_retry_after(raw: str | None, now: float, *, fallback: float) -> float:
    """Retry-After is either delta-seconds OR an HTTP-date (RFC 9110). The
    original code assumed seconds and threw on the date form, which real
    services send (R0 #5). Parse both; never return negative or raise.

    `now` is facility-relative seconds (the injected clock's timeline), so an
    absolute HTTP-date can't be diffed against it directly; when a date form
    is seen we honor it as the fallback delay rather than guessing a wall-clock
    offset. The seconds form — the common case — is parsed exactly.
    """
    if raw is None:
        return max(0.0, float(fallback))
    raw = raw.strip()
    try:
        return max(0.0, float(raw))            # delta-seconds form
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime
    try:
        parsedate_to_datetime(raw)             # validate it IS an HTTP-date
        return max(0.0, float(fallback))       # honor as fallback (see docstring)
    except (TypeError, ValueError):
        return max(0.0, float(fallback))       # unparseable -> fallback


class SafetyEnforcer:
    def __init__(self, backend: Backend, clock: Clock, *,
                 rate_per_minute: int, total_call_budget: int,
                 journal: Journal | None = None,
                 auth_refresh: 'Callable[[], bool] | None' = None) -> None:
        """auth_refresh (R2 #9): an optional zero-arg callback invoked once on
        a 401 to re-acquire credentials; returns True if it succeeded. Without
        it, a 401 raises AuthExpiredError immediately — distinct from the 403
        permission-gap path."""
        if rate_per_minute < 1 or total_call_budget < 1:
            raise ValueError('budgets must be >= 1')
        self._backend = backend
        self._clock = clock
        self._rate = rate_per_minute
        self._total_budget = total_call_budget
        self._calls_made = 0
        self._window_start = clock.now()
        self._window_count = 0
        self.journal = journal or Journal()
        self._auth_refresh = auth_refresh

    # -- public surface (the ONLY verb) ---------------------------------------

    def get(self, path: str, params: Mapping[str, str] | None = None
            ) -> TransportResponse:
        return self._request('GET', path, params or {})

    @property
    def calls_remaining(self) -> int:
        return self._total_budget - self._calls_made

    # -- internals -------------------------------------------------------------

    def _request(self, method: str, path: str,
                 params: Mapping[str, str]) -> TransportResponse:
        if method not in _ALLOWED_METHODS:
            self.journal.record(ts=self._clock.now(), event='refused_write',
                                method=method, path=path, status=None, attempt=0)
            raise WriteRefused(method, path)

        refreshed = False
        for attempt in range(_MAX_RETRIES + 1):
            self._take_rate_token()
            self._take_call_budget()
            try:
                resp = self._backend.handle(
                    TransportRequest(method=method, path=path, params=params))
            except TransportTimeout:        # R2 #8: a hang becomes a transient
                self.journal.record(ts=self._clock.now(), event='timeout',
                                    method=method, path=path, status=None,
                                    attempt=attempt)
                self._clock.sleep(2 ** attempt)
                continue
            self.journal.record(ts=self._clock.now(), event='sent',
                                method=method, path=path,
                                status=resp.status, attempt=attempt)
            if resp.status == 401:          # R2 #9: auth expired, not a gap
                if self._auth_refresh is not None and not refreshed:
                    self.journal.record(ts=self._clock.now(),
                                        event='auth_refresh', method=method,
                                        path=path, status=401, attempt=attempt)
                    refreshed = True
                    if self._auth_refresh():
                        continue            # retry once with fresh credentials
                raise AuthExpiredError(
                    f'401 on {path!r}: credentials expired/revoked mid-run '
                    f'(refresh {"failed" if refreshed else "unavailable"})')
            if resp.status not in _RETRYABLE:
                return resp
            self.journal.record(ts=self._clock.now(), event='throttled',
                                method=method, path=path,
                                status=resp.status, attempt=attempt)
            retry_after = _parse_retry_after(
                resp.headers.get('Retry-After'), self._clock.now(),
                fallback=2 ** attempt)
            self._clock.sleep(retry_after)
        raise BudgetExhausted('retry', _MAX_RETRIES)

    def _take_rate_token(self) -> None:
        now = self._clock.now()
        if now - self._window_start >= 60.0:
            self._window_start, self._window_count = now, 0
        if self._window_count >= self._rate:
            # Politeness ceiling: wait out the window rather than exceed it.
            wait = 60.0 - (now - self._window_start)
            self._clock.sleep(max(wait, 0.0))
            self._window_start, self._window_count = self._clock.now(), 0
        self._window_count += 1

    def _take_call_budget(self) -> None:
        if self._calls_made >= self._total_budget:
            self.journal.record(ts=self._clock.now(), event='budget_halt',
                                method='GET', path='', status=None,
                                attempt=0)
            raise BudgetExhausted('total_call', self._total_budget)
        self._calls_made += 1
