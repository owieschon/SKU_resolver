"""Live smoke — REAL model calls, cheaply. The real (non-stubbed) validation path.

How this is cheap:
  - Skipped entirely unless a real key is present (never runs in CI).
  - Routes to the CHEAP tier (Haiku-4.5 / gemini-2.5-flash) — the smallest,
    fastest models — via tier override, regardless of the task's normal tier.
  - Tiny prompts + small max_tokens.
  - Asserts the run's TOTAL spend stayed under a hard cent-level cap, read
    from the cost ledger. A full live run is fractions of a cent.

Run it on demand:
    ANTHROPIC_API_KEY=sk-ant-... pytest tests/test_live_smoke.py -v
  or against the literally-call-capture-validated model, near-free:
    OPENROUTER_API_KEY=sk-or-... SKU_LIVE_PROVIDER=openrouter pytest tests/test_live_smoke.py

This proves the adapters parse real responses and the seams bind real model
output — the thing ScriptedProvider cannot prove. It is NOT part of CI green.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from model_provider import LLMClient, configured_providers, make_provider
from model_provider.routing import CHEAP, TIER_MODELS

REPO = Path(__file__).resolve().parent.parent

# Pick the provider from env; skip the whole module if none is configured.
_PROVIDER = os.environ.get('SKU_LIVE_PROVIDER') or (
    configured_providers()[0] if configured_providers() else None)
pytestmark = pytest.mark.skipif(
    _PROVIDER is None,
    reason='no LLM key configured — live smoke is opt-in (set ANTHROPIC_API_KEY '
           'or OPENROUTER_API_KEY). Not part of CI.')

# Hard spend cap for the entire module run. Cheap-tier + tiny prompts keep a
# full pass well under this; the assertion fails loudly if a change blows it.
MAX_SPEND_USD = 0.05


@pytest.fixture(scope='module')
def live():
    from observability.cost import CostLedger
    ledger = CostLedger(REPO / 'state' / 'live_smoke_cost.jsonl')
    ledger.rows.clear()
    provider = make_provider(_PROVIDER)
    client = LLMClient(provider=provider, cost_ledger=ledger,
                       now_iso=lambda: 'live', session_id='smoke')
    yield client, ledger
    spent = sum(r.get('cost_usd', 0.0) for r in ledger.rows)
    assert spent <= MAX_SPEND_USD, f'live smoke spent ${spent:.4f} > cap'


def _cheap(client, **kw):
    # Force the cheapest model for the smoke regardless of task policy.
    return client.propose(override_model=TIER_MODELS[_PROVIDER][CHEAP], **kw)


def test_live_intent_classification(live):
    client, _ = live
    resp = _cheap(client, task='intent',
                  system='Reply with JSON {"intent": one of '
                         'pricing|verify|availability|handoff}.',
                  user='is the K5-24SBC in stock?',
                  json_schema={'type': 'object',
                               'properties': {'intent': {'type': 'string'}},
                               'required': ['intent'],
                               'additionalProperties': False},
                  max_tokens=64)
    assert resp.data and resp.data.get('intent') == 'availability'
    assert resp.in_tokens > 0          # a real call happened


def test_live_chooser_picks_from_candidates(live):
    client, _ = live
    cands = ['VB-5C: CLAMP V-BAND 5in SS', 'R5-4C: COUPLER 5x8',
             'K5-24SBC: STACK CURVED 5x24 chrome']
    resp = _cheap(client, task='retrieval_select',
                  system='Pick the single best SKU, copied exactly from the '
                         'candidates. JSON {"sku": ...}.',
                  user='I need the v-band clamp\n' + '\n'.join(cands),
                  json_schema={'type': 'object',
                               'properties': {'sku': {'type': 'string'}},
                               'required': ['sku'],
                               'additionalProperties': False},
                  max_tokens=64)
    # The real model should pick the v-band clamp; bind-guard would catch any
    # non-candidate, but here we just confirm it returns a real candidate.
    assert resp.data and resp.data.get('sku') in {c.split(':')[0] for c in cands}


def test_live_run_stayed_cheap(live):
    # Sanity within the run (the fixture teardown also enforces the cap).
    client, ledger = live
    # one more trivial call, then check cumulative spend is tiny
    _cheap(client, task='intent', system='Reply {"ok": true}.', user='ping',
           json_schema={'type': 'object',
                        'properties': {'ok': {'type': 'boolean'}},
                        'required': ['ok'], 'additionalProperties': False},
           max_tokens=16)
    spent = sum(r.get('cost_usd', 0.0) for r in ledger.rows)
    assert spent < MAX_SPEND_USD
