"""Provider-agnostic LLM layer (P2).

The model proposes; deterministic code binds. BYOK keys from the environment;
direct frontier labs (Anthropic, OpenAI) + OpenRouter as the any-model escape
hatch. An opinionated, self-aware routing policy picks the model per task
(enforced-with-override) and the cost ledger logs every call for cross-model
comparison. CI runs ScriptedProvider — network-free, deterministic.
"""
from model_provider.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
)
from model_provider.client import LLMClient
from model_provider.keyring import configured_providers, has_key, key_for
from model_provider.routing import (
    TASK_POLICY,
    TIER_MODELS,
    ModelChoice,
    UnknownProvider,
    policy_table,
    resolve_model,
)
from model_provider.scripted import ScriptedProvider


def make_provider(name: str):
    """Construct a real provider adapter by name. Lazy so SDKs load only when
    a provider is actually selected."""
    if name == 'anthropic':
        from model_provider.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    if name == 'openai':
        from model_provider.openai_compat import OpenAIProvider
        return OpenAIProvider()
    if name == 'openrouter':
        from model_provider.openai_compat import OpenRouterProvider
        return OpenRouterProvider()
    raise UnknownProvider(f'unknown provider {name!r}')


__all__ = [
    'ModelProvider', 'ModelRequest', 'ModelResponse', 'ModelUnavailable',
    'LLMClient', 'ScriptedProvider', 'configured_providers', 'has_key', 'key_for',
    'ModelChoice', 'TASK_POLICY', 'TIER_MODELS', 'UnknownProvider',
    'policy_table', 'resolve_model', 'make_provider',
]
