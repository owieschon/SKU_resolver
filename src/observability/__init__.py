"""Observability layer — tracing, cost ledger, deploy guard, alert routing.

Domain-neutral, adapted from a prior agent operational stack and
generalized. Two hard rules this package keeps:
  - OFF / no-op by default; tracing is opt-in and fail-open.
  - Network-free at import time — OTel and webhook clients are imported lazily
    inside functions, so anything that imports this stays purity-test clean.
"""
from observability.alerts import AlertRouter
from observability.cost import (
    BudgetExceeded, CostEvent, CostLedger, anomaly_flags,
)
from observability.deploy_guard import (
    Preflight, StaleCheck, StartupSnapshot, check_for_stale_code,
    record_startup_commit, verification_preflight,
)
from observability.telemetry import (
    init_tracing, redact, register_structured, reset_for_test, scrub_pii,
    set_attr, tracer,
)
from observability.service_improvement import ImprovementLog, anon_key, scrub

__all__ = [
    'AlertRouter', 'BudgetExceeded', 'CostEvent', 'CostLedger', 'anomaly_flags',
    'Preflight', 'StaleCheck', 'StartupSnapshot', 'check_for_stale_code',
    'record_startup_commit', 'verification_preflight', 'init_tracing',
    'redact', 'register_structured', 'reset_for_test', 'scrub_pii', 'set_attr',
    'tracer', 'ImprovementLog', 'anon_key', 'scrub',
]
