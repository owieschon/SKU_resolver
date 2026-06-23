"""P2 model-provider layer: routing policy (enforced-with-override), BYOK key
resolution, ScriptedProvider determinism, cost-ledger logging, graceful fallback.
No network — ScriptedProvider only.
"""
from __future__ import annotations

import pytest

from model_provider import (
    ScriptedProvider, LLMClient, ModelUnavailable, TIER_MODELS, UnknownProvider,
    configured_providers, has_key, policy_table, resolve_model,
)


# ── routing policy (the system's opinion, enforced-with-override) ────────────

def test_task_policy_picks_documented_tier():
    c = resolve_model('retrieval_select', 'anthropic')
    assert c.tier == 'medium'                       # locked-evidence tier
    assert c.model == 'claude-sonnet-4-6'
    assert c.source == 'task_policy'
    assert '88.2%' in c.rationale                   # cites the evidence


def test_intent_is_cheap_tier():
    c = resolve_model('intent', 'anthropic')
    assert c.tier == 'cheap' and c.model == 'claude-haiku-4-5'


def test_override_wins_and_is_recorded():
    c = resolve_model('intent', 'anthropic', override='claude-opus-4-8')
    assert c.model == 'claude-opus-4-8' and c.source == 'override'


def test_policy_is_provider_agnostic():
    # Same task, different provider -> that provider's tier model.
    a = resolve_model('retrieval_select', 'anthropic')
    o = resolve_model('retrieval_select', 'openrouter')
    assert a.tier == o.tier == 'medium'
    assert o.model == 'anthropic/claude-sonnet-4-6'   # routed via OpenRouter


def test_unknown_provider_raises():
    with pytest.raises(UnknownProvider):
        resolve_model('intent', 'nope')


def test_unknown_task_defaults_to_medium():
    c = resolve_model('some_new_task', 'anthropic')
    assert c.tier == 'medium'


def test_policy_table_is_self_documenting():
    table = policy_table()
    assert {r['task'] for r in table} >= {'intent', 'retrieval_select',
                                          'onboarding_map'}
    assert all(r['rationale'] for r in table)


# ── BYOK key resolution (presence only, never exposes value) ─────────────────

def test_key_presence_without_exposure(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    assert not has_key('anthropic')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-secret')
    assert has_key('anthropic')
    assert 'anthropic' in configured_providers()


# ── client: route -> call -> log; graceful fallback ─────────────────────────

def test_client_logs_cost_per_call(tmp_path):
    from observability.cost import CostLedger
    ledger = CostLedger(tmp_path / 'cost.jsonl')
    provider = ScriptedProvider(scripted={'intent': {'intent': 'pricing'}})
    client = LLMClient(provider=provider, cost_ledger=ledger,
                       now_iso=lambda: 't', session_id='S1')
    resp = client.propose(task='intent', system='s', user='u',
                          json_schema={'type': 'object'})
    assert resp.data == {'intent': 'pricing'}
    assert ledger.rows and ledger.rows[0]['task'] == 'intent'
    assert ledger.rows[0]['session_id'] == 'S1'


def test_client_propagates_model_unavailable():
    provider = ScriptedProvider(fail_tasks={'intent'})
    client = LLMClient(provider=provider)
    with pytest.raises(ModelUnavailable):
        client.propose(task='intent', system='s', user='u')


def test_scripted_provider_is_deterministic():
    p = ScriptedProvider(scripted={'intent': lambda req: {'echo': req.user}})
    r1 = p.complete(_req('hello'))
    r2 = p.complete(_req('hello'))
    assert r1.data == r2.data == {'echo': 'hello'}


def _req(user):
    from model_provider import ModelRequest
    return ModelRequest(task='intent', system='s', user=user,
                        model='scripted-model')
