"""Live-tenant ERP wiring — assemble a SafetyEnforcer over a real backend.

This is the one-command bridge from "twin-tested harness" to "live tenant": it
reads connection config from the environment and returns a `SafetyEnforcer`
wrapping the right transport (HttpBackend for BC SaaS, SqlBackend for NAV
on-prem). The harness, discovery, and onboarding code are unchanged — only the
backend behind the enforcer differs.

Discipline (harness spec §6): a live-tenant run stays GATED behind a green twin
fault-injection check matrix. This builder makes the live run trivial to
launch; it does not lower that bar.
"""
from __future__ import annotations

import os
import time

from erp_harness.enforcer import SafetyEnforcer
from erp_transport.http_backend import HttpBackend, OAuthClientCredentials


class WallClock:
    """Production Clock: real monotonic time + real sleep (backoff/budgets).
    ManualClock is the test/twin counterpart."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


def _require(env, key: str) -> str:
    val = env.get(key)
    if not val:
        raise RuntimeError(f'live ERP config: missing env var {key!r}')
    return val


def build_live_backend(env=None, *, clock=None):
    """Build the wire backend from env. BC SaaS (OAuth+HTTPS) or NAV (SQL)."""
    env = env if env is not None else os.environ
    clock = clock or WallClock()
    kind = (env.get('SKU_ERP_KIND') or '').lower()
    if kind == 'bc':
        oauth = OAuthClientCredentials(
            _require(env, 'SKU_ERP_TOKEN_URL'),
            _require(env, 'SKU_ERP_CLIENT_ID'),
            _require(env, 'SKU_ERP_CLIENT_SECRET'),
            env.get('SKU_ERP_SCOPE', ''), clock)
        return HttpBackend(_require(env, 'SKU_ERP_BASE_URL'),
                           token_provider=oauth)
    if kind == 'nav':
        from erp_transport.sql_backend import SqlBackend  # pyodbc ([erp] extra)
        return SqlBackend.from_pyodbc(_require(env, 'SKU_ERP_SQL_DSN'),
                                      schema=env.get('SKU_ERP_SQL_SCHEMA', 'dbo'))
    raise RuntimeError("set SKU_ERP_KIND=bc|nav and the matching connection env "
                       "(see docs/ERP_LIVE_RUNBOOK.md)")


def build_live_enforcer(env=None, *, clock=None) -> SafetyEnforcer:
    """The full live enforcer: a real backend under the same code-enforced rate
    budget / method allowlist / journal the twin runs under."""
    env = env if env is not None else os.environ
    clock = clock or WallClock()
    backend = build_live_backend(env, clock=clock)
    return SafetyEnforcer(
        backend, clock,
        rate_per_minute=int(env.get('SKU_ERP_RATE_PER_MIN', '120')),
        total_call_budget=int(env.get('SKU_ERP_CALL_BUDGET', '2000')))
