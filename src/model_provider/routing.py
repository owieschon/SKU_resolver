"""Model routing policy — the system's OPINION about which model fits each task.

Enforced-with-override (decision, your call 2026-06-07): the system picks the
model per task by default; a caller may override with an explicit model id,
and the choice is recorded (task_policy vs override) for the cost ledger.

The opinion is grounded in the locked experiment record, not guesses:
- Extraction/selection on clean structured inputs is a MEDIUM-tier job — the
  call-capture arc hit 88.2% LLM-conditional selection with a fast/cheap
  model (Gemini 2.5 Flash) over a K=25 candidate window; the Claude-tier
  equivalent is Sonnet, not Opus. (Paying Opus here is waste.)
- Intent/scope classification is a CHEAP-tier job (Haiku) — short, bounded,
  latency-sensitive; the gateway is conversational.
- ERP-schema mapping reasoning is MEDIUM (Sonnet) — structural, not deep.
- Nothing in this system needs HIGH (Opus) by default; HIGH is reserved for
  an explicit override when correctness must beat cost on a hard call.

Per-provider tier→model tables let the same policy run against direct
frontier labs OR OpenRouter (which can serve the exact validated non-Claude
models, e.g. Gemini 2.5 Flash, if the operator prefers).
"""
from __future__ import annotations

from dataclasses import dataclass

CHEAP, MEDIUM, HIGH = 'cheap', 'medium', 'high'

# Per-provider model id for each tier. Claude ids verified against the
# claude-api reference (2026-06); OpenRouter ids are the slugs that route to
# the validated models.
TIER_MODELS: dict[str, dict[str, str]] = {
    'anthropic': {
        CHEAP: 'claude-haiku-4-5',
        MEDIUM: 'claude-sonnet-4-6',
        HIGH: 'claude-opus-4-8',
    },
    'openai': {
        CHEAP: 'gpt-5-mini',
        MEDIUM: 'gpt-5',
        HIGH: 'gpt-5',
    },
    'openrouter': {
        # OpenRouter can serve the exact call-capture-validated chooser model.
        CHEAP: 'google/gemini-2.5-flash',
        MEDIUM: 'anthropic/claude-sonnet-4-6',
        HIGH: 'anthropic/claude-opus-4-8',
    },
    # Test/CI provider — routing resolves to a placeholder so the ScriptedProvider
    # exercises the full route->call->log path without a real model id.
    'scripted': {CHEAP: 'scripted-cheap', MEDIUM: 'scripted-medium', HIGH: 'scripted-high'},
}


@dataclass(frozen=True)
class TaskPolicy:
    tier: str
    rationale: str          # WHY this tier — cited, auditable


# The opinion, per task. Each entry is self-documenting.
TASK_POLICY: dict[str, TaskPolicy] = {
    'intent': TaskPolicy(
        CHEAP, 'Short bounded scope/intent classification on a conversational '
        'turn — latency-sensitive, cheap-tier is sufficient.'),
    'retrieval_select': TaskPolicy(
        MEDIUM, 'Pick one SKU from a K=25 candidate window. Locked arc: 88.2% '
        'LLM-conditional with a fast model; medium-tier, not Opus.'),
    'onboarding_map': TaskPolicy(
        MEDIUM, 'Propose ERP field->contract mappings from schema + samples — '
        'structural reasoning, medium-tier; every proposal is probe-verified '
        'so model error is caught, not binding.'),
    'catalog_decode_role': TaskPolicy(
        MEDIUM, 'Label SKU segments the deterministic correlation pass left '
        'unknown, from family examples + sample descriptions — pattern '
        'reasoning over messy strings, medium-tier; the label is an unverified '
        'proposal a human SME confirms, never binding.'),
    'cover_prose': TaskPolicy(
        MEDIUM, 'Customer-facing prose around a deterministic table — quality '
        'matters, but it is not a reasoning-hard task.'),
}

DEFAULT_TIER = MEDIUM


class UnknownProvider(Exception):
    pass


@dataclass(frozen=True)
class ModelChoice:
    model: str
    tier: str
    provider: str
    source: str             # 'task_policy' | 'override'
    rationale: str


def resolve_model(task: str, provider: str, *, override: str | None = None
                  ) -> ModelChoice:
    """The enforced-with-override decision. Without an override, the task's
    policy tier picks the model for the active provider. With an override, the
    caller's model id wins and the source is recorded as 'override'."""
    if provider not in TIER_MODELS:
        raise UnknownProvider(
            f'no model table for provider {provider!r}; known: '
            f'{sorted(TIER_MODELS)}')
    if override is not None:
        return ModelChoice(model=override, tier='(override)', provider=provider,
                           source='override',
                           rationale='caller-supplied model override')
    policy = TASK_POLICY.get(task) or TaskPolicy(
        DEFAULT_TIER, 'no task policy; defaulted to medium tier')
    return ModelChoice(model=TIER_MODELS[provider][policy.tier],
                       tier=policy.tier, provider=provider,
                       source='task_policy', rationale=policy.rationale)


def policy_table() -> list[dict]:
    """Render the opinion for docs/observability — the system explaining
    itself: which tier each task uses and why."""
    return [{'task': t, 'tier': p.tier, 'rationale': p.rationale}
            for t, p in TASK_POLICY.items()]
