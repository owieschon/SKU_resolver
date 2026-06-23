"""Shared construction helpers for the harness test matrix."""
from __future__ import annotations

from pathlib import Path

from erp_harness import (
    ERPClass, ERPDescriptor, ManualClock, HeuristicExplorer, SafetyEnforcer,
    run_onboarding,
)
from erp_twin import STANDARD_GRANTS, seeded_twin

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / 'data' / 'catalog.csv'
BC = ERPDescriptor(ERPClass.BC_SAAS, 'BC 24', 'saas')


def make_rig(*, granted: set[str] | None = None,
             throttle_per_minute: int | None = None,
             rate_per_minute: int = 300, total_call_budget: int = 500,
             item_limit: int = 500):
    """One stop: (clock, twin, enforcer). Deterministic by construction."""
    clock = ManualClock()
    twin = seeded_twin(CATALOG, clock=clock, granted=granted,
                       throttle_per_minute=throttle_per_minute,
                       item_limit=item_limit)
    enforcer = SafetyEnforcer(twin, clock, rate_per_minute=rate_per_minute,
                              total_call_budget=total_call_budget)
    return clock, twin, enforcer


def onboard(*, item_limit: int = 200, **rig_kw):
    clock, twin, enforcer = make_rig(item_limit=item_limit, **rig_kw)
    result = run_onboarding(BC, enforcer, HeuristicExplorer(), clock)
    return clock, twin, enforcer, result


def all_grants() -> set[str]:
    return set(STANDARD_GRANTS)
