"""Shared test fixtures.

Live-smoke preflight: any `test_live*` module is a credential-gated live
verification. Before such a test runs, the deploy-guard stale/dirty-code check
(`verification_preflight`) must pass — you should never verify live behavior
against uncommitted or stale code (the guard's original purpose). This wires
that guard to the smokes it protects; it's inert for normal (non-live) tests and
for CI runs where the live smokes skip earlier for lack of credentials.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _live_preflight(request):
    if not request.module.__name__.startswith('test_live'):
        return
    from observability import verification_preflight
    pf = verification_preflight(_REPO, _REPO / 'state' / 'startup.json')
    if pf.should_block:
        pytest.skip(f'live preflight blocked (commit your code first): {pf.message}')
