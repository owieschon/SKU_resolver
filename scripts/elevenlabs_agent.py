#!/usr/bin/env python3
"""Build / inspect / deploy the ElevenLabs voice agent over the gateway tool.

The agent is a hosted speech shell (voice, turn-taking, small talk); every part
fact comes from ONE server tool, `resolve_part` = our deterministic gateway
(POST /agent/turn). See docs/VOICE_AGENT.md for the architecture and grounding.

Usage:
    # Print the exact create payload — no network, no key needed (CI-safe):
    python scripts/elevenlabs_agent.py --dry-run [--tool-base-url https://host]

    # Validate the system prompt keeps its non-negotiable guardrails:
    python scripts/elevenlabs_agent.py --validate

    # Create (or --agent-id <id> to update) the live agent. Needs
    # ELEVENLABS_API_KEY and a public AGENT_TOOL_BASE_URL the agent can reach:
    AGENT_TOOL_BASE_URL=https://your-tunnel.example \
        python scripts/elevenlabs_agent.py --apply

Persona (name / accent / voice / greeting) comes from the SAME env-configurable
VoicePersona the runtime uses (SKU_VOICE_NAME / _ACCENT / _GREETING / _ID), so
the hosted agent and the local stack share one source of truth.
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'src'))

from runtime.config import build_persona              # noqa: E402
from runtime.voice_agent import (                     # noqa: E402
    AgentSettings, build_agent_payload, create_or_update_agent,
    load_system_prompt, validate_system_prompt,
)


def _settings() -> AgentSettings:
    """Per-deploy overrides via env (LLM must be one ElevenLabs serves)."""
    defaults = AgentSettings()
    return AgentSettings(llm=os.environ.get('SKU_AGENT_LLM', defaults.llm))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true',
                   help='print the create payload as JSON; no network')
    g.add_argument('--validate', action='store_true',
                   help='check the system prompt keeps its guardrails')
    g.add_argument('--apply', action='store_true',
                   help='create/update the live agent (needs ELEVENLABS_API_KEY)')
    ap.add_argument('--tool-base-url',
                    default=os.environ.get('AGENT_TOOL_BASE_URL',
                                           'https://REPLACE-WITH-PUBLIC-HOST'),
                    help='public base URL the agent calls for /agent/turn')
    ap.add_argument('--agent-id', default=None,
                    help='update this existing agent instead of creating one')
    args = ap.parse_args()

    prompt = load_system_prompt()
    missing = validate_system_prompt(prompt)

    if args.validate:
        if missing:
            print('PROMPT INVALID — missing required guardrail clauses:')
            for m in missing:
                print(f'  - {m}')
            return 1
        print(f'prompt OK — all {len(prompt.splitlines())} lines, guardrails present')
        return 0

    persona = build_persona()
    payload = build_agent_payload(persona=persona,
                                  tool_base_url=args.tool_base_url,
                                  system_prompt=prompt, settings=_settings())

    if args.dry_run:
        if missing:
            print(f'# WARNING: prompt missing guardrails: {missing}', file=sys.stderr)
        if 'REPLACE-WITH-PUBLIC-HOST' in args.tool_base_url:
            print('# NOTE: set AGENT_TOOL_BASE_URL (or --tool-base-url) to the '
                  'public host before --apply', file=sys.stderr)
        print(json.dumps(payload, indent=2))
        return 0

    # --apply
    if missing:
        print(f'refusing to deploy: prompt missing guardrails {missing}',
              file=sys.stderr)
        return 1
    if 'REPLACE-WITH-PUBLIC-HOST' in args.tool_base_url:
        print('set AGENT_TOOL_BASE_URL (or --tool-base-url) to a public host the '
              'ElevenLabs agent can reach', file=sys.stderr)
        return 1
    result = create_or_update_agent(payload, agent_id=args.agent_id)
    print(json.dumps(result, indent=2))
    aid = result.get('agent_id') or result.get('id')
    if aid:
        print(f'\nagent {"updated" if args.agent_id else "created"}: {aid}',
              file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
