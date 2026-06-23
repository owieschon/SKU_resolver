"""BYOK key resolution. Keys come from the environment (or a secrets
manager that populates the environment) — never the repo, never a log. The
observability scrubber already redacts key patterns from any trace/journal;
this module additionally refuses to expose a key value, only its presence.
"""
from __future__ import annotations

import os

# Per-provider environment variable for the user's own key.
_ENV = {
    'anthropic': 'ANTHROPIC_API_KEY',
    'openai': 'OPENAI_API_KEY',
    'openrouter': 'OPENROUTER_API_KEY',
}


def key_for(provider: str) -> str | None:
    """Return the configured key for a provider, or None. Callers pass it
    straight to the SDK; they must never log it."""
    env = _ENV.get(provider)
    return os.environ.get(env) if env else None


def has_key(provider: str) -> bool:
    """Presence check without exposing the value — safe to log/trace."""
    return bool(key_for(provider))


def configured_providers() -> list[str]:
    """Which providers the operator has supplied a key for."""
    return [p for p in _ENV if has_key(p)]
