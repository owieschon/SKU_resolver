"""ElevenLabs Agent definition — the hosted voice shell over the gateway tool.

Architecture (the "agent shell + gateway-as-a-tool" decision, see
docs/VOICE_AGENT.md): a hosted ElevenLabs Agent owns speech, turn-taking, small
talk, and the natural voice; for ANY part/availability/price question it calls
ONE server tool, `resolve_part`, which is our deterministic gateway (POST
/agent/turn). Never-invent and pricing-behind-verification stay enforced in
code — the agent cannot produce a SKU or a price except through the tool, and
the tool returns a verbatim `say` string for the agent to speak.

This module is split so the *build and validation* of the agent definition are
pure and testable with no network: `build_agent_payload()` assembles the exact
create/update request, and `validate_system_prompt()` is a fault-injection check
check that the non-negotiable guardrail clauses are present. Only the
`create_or_update_agent()` / `create_pronunciation_dictionary()` calls touch the
network, and they are key-gated.

Grounding (authoritative ElevenLabs docs, fetched 2026-06-07; live-verified
against the create API):
  - Six-block system prompt: Personality, Environment, Tone, Goal, Guardrails,
    Tools. /docs/eleven-agents/best-practices/prompting-guide
  - Guardrails 2.0 — discriminated on version "1": focus / prompt_injection /
    content / custom. /docs/eleven-agents/best-practices/guardrails
  - ASR keyword biasing (asr.keywords), patient turn-taking, soft-timeout filler,
    pre_tool_speech, alias pronunciation dictionaries, system tools (end_call,
    transfer_to_number). /docs/eleven-agents/* + changelog 2026-04-27/05-04.
  - POST https://api.elevenlabs.io/v1/convai/agents/create  (header xi-api-key)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

CREATE_URL = 'https://api.elevenlabs.io/v1/convai/agents/create'
UPDATE_URL = 'https://api.elevenlabs.io/v1/convai/agents/{agent_id}'
PD_RULES_URL = 'https://api.elevenlabs.io/v1/pronunciation-dictionaries/add-from-rules'

# Repo-relative path to the reviewable, version-controlled system prompt.
PROMPT_PATH = Path(__file__).resolve().parents[2] / 'voice_agent' / 'SYSTEM_PROMPT.md'


def load_system_prompt(path: Path | str | None = None) -> str:
    return Path(path or PROMPT_PATH).read_text(encoding='utf-8')


# -- the one server tool: the deterministic gateway --------------------------

def resolve_part_tool(tool_base_url: str, *, response_timeout_secs: int = 12,
                      pre_tool_speech: str = 'auto',
                      auth_secret_id: str = '') -> dict:
    """The `resolve_part` webhook tool: POST {base}/agent/turn.

    `text` is LLM-extracted (what the caller said). `caller_id` is bound to the
    conversation id (a stable per-call session key) via an ElevenLabs system
    dynamic variable so the gateway session — and account verification — persist
    across the call. `pre_tool_speech` lets the agent acknowledge ("let me check
    that") while the lookup runs, masking latency; `response_timeout_secs` caps a
    hung lookup."""
    base = tool_base_url.rstrip('/')
    return {
        'type': 'webhook',
        'name': 'resolve_part',
        'description': (
            'Resolve a customer-service turn about a part: identify the part, '
            'check availability and lead time, and (only after the account is '
            'verified) disclose pricing. Returns a JSON object whose `say` field '
            'is the exact sentence to read to the caller. Call this for ANY turn '
            'involving a part, part number, availability, stock, ship date, lead '
            'time, price, account number, or verification. Never answer those '
            'from memory.'),
        # Latency UX (documented on tool/MCP config): acknowledge before/while
        # the lookup runs, and don't wait forever.
        'pre_tool_speech': pre_tool_speech,
        'response_timeout_secs': response_timeout_secs,
        'api_schema': {
            'url': f'{base}/agent/turn',
            'method': 'POST',
            # Content-Type literal; the auth header value is a workspace-secret
            # reference ({secret_id: ...}) so the token never lives in the agent
            # config. The gateway validates it (X-Agent-Token) — containment.
            'request_headers': dict(
                {'Content-Type': 'application/json'},
                **({'X-Agent-Token': {'secret_id': auth_secret_id}}
                   if auth_secret_id else {})),
            'request_body_schema': {
                'type': 'object',
                'properties': {
                    'text': {
                        'type': 'string',
                        'description': (
                            'Exactly what the caller just said, in their own '
                            'words: the part number or description, an account '
                            'number, or their yes/no to a readback. Send it as '
                            'spoken — the tool handles imperfect transcription.'),
                    },
                    'caller_id': {
                        'type': 'string',
                        'dynamic_variable': 'system__conversation_id',
                    },
                },
                'required': ['text', 'caller_id'],
            },
        },
    }


def system_tools(transfer_number: str = '') -> list[dict]:
    """Built-in tools. API-created agents get NONE by default, so we add them
    explicitly: `end_call` (graceful hang-up) always, and a WARM (conference)
    `transfer_to_number` to a human when the gateway escalates — only if a
    destination number is configured (else escalation just speaks the hand-off
    line, as before)."""
    tools: list[dict] = [{
        'type': 'system',
        'name': 'end_call',
        'description': ("End the call when the caller's request is fully handled "
                        "and they've said goodbye — not before."),
    }]
    if transfer_number:
        tools.append({
            'type': 'system',
            'name': 'transfer_to_number',
            'description': (
                'Transfer the caller to a human at the parts counter when the '
                'resolve_part tool escalates, or when the caller explicitly asks '
                'for a person you cannot help through the tool.'),
            'params': {
                'transfers': [{
                    'transferDestination': {'type': 'phone',
                                            'phoneNumber': transfer_number},
                    'condition': ('The resolve_part tool returned an escalation, '
                                  'or the caller asked to speak to a person about '
                                  'something the tool cannot resolve.'),
                    'transferType': 'conference',   # warm: brief the human first
                }],
            },
        })
    return tools


# -- guardrails 2.0 (platform control layer, version "1") --------------------

def guardrails_config() -> dict:
    """The ElevenLabs Guardrails 2.0 platform layer (defense in depth behind the
    prompt's # Guardrails + the in-code tool enforcement). Discriminated union
    keyed on version "1" (the missing discriminator was the earlier 422). Note
    the casing: built-ins use `isEnabled` (camelCase), custom uses `is_enabled`.
    """
    return {
        'version': '1',
        'focus': {'isEnabled': True},            # keep replies on the prompt
        'prompt_injection': {'isEnabled': True},  # = "Manipulation": block jailbreaks
        'content': {'isEnabled': True},          # block inappropriate content
        'custom': {'config': {'configs': [{
            'is_enabled': True,
            'name': 'parts_only_from_tool',
            'prompt': (
                'Block any response that states a part number, description, '
                'availability, quantity, ship date, lead time, or price that was '
                'not returned by the resolve_part tool in this conversation, or '
                'that quotes a price when the tool did not return one after '
                'verification. The agent only conveys what the tool provided.'),
            'model': 'gemini-2.5-flash-lite',
            'execution_mode': 'blocking',         # don't start bad audio
            'trigger_action': {
                'type': 'retry',
                'feedback': ("That reply was blocked: it stated a part fact the "
                             "resolve_part tool did not provide. Re-answer using "
                             "only the tool's output, or offer to check/transfer."),
            },
        }]}},
    }


# -- ASR keyword biasing derived from the catalog ----------------------------

def asr_keywords_from_skus(skus, limit: int = 50) -> list[str]:
    """Seed the ASR keyword biaser with the catalog's real building blocks:
    leading family prefixes (K5, SBR6, L590, BH5) and trailing finish/body codes
    (SBC, EXC, SC). These are what the recognizer mis-hears on alphanumerics;
    biasing toward them pulls decoding back to real SKU morphology. Frequency-
    ordered, deduped, capped at `limit` (Scribe documents ~50)."""
    from collections import Counter
    prefixes: Counter = Counter()
    suffixes: Counter = Counter()
    for sku in skus:
        s = sku.upper()
        mp = re.match(r'^[A-Z]+\d+', s)          # leading alpha + first number, e.g. K5, SBR6
        if mp:
            prefixes[mp.group()] += 1
        ms = re.search(r'[A-Z]+$', s)            # trailing letters, e.g. SBC, EXC, SC
        if ms and len(ms.group()) >= 2:
            suffixes[ms.group()] += 1
    ranked = [t for t, _ in prefixes.most_common()] + [t for t, _ in suffixes.most_common()]
    seen, out = set(), []
    for t in ranked:
        if t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= limit:
            break
    return out


# -- the create/update payload (pure; no network) ----------------------------

@dataclass(frozen=True)
class AgentSettings:
    # The agent's reasoning model. For a VOICE agent latency dominates, and this
    # agent mostly relays the tool's `say` verbatim — so a fast model is correct;
    # the binding guarantees live in code at the tool, not in the agent's model.
    llm: str = 'gemini-2.5-flash'
    # English agents must use turbo or flash v2; flash v2 is the lowest-latency
    # option, which matters on a phone call.
    tts_model_id: str = 'eleven_flash_v2'
    language: str = 'en'
    # Voice tuning for a clear, warm, unhurried parts-counter read of codes.
    stability: float = 0.7
    similarity_boost: float = 0.8
    style: float = 0.0                 # >0 adds latency + destabilizes; keep 0
    speed: float = 0.95                # a touch under 1.0 reads codes clearer
    # ASR (telephony): Scribe realtime, Twilio mu-law, keyword-biased.
    asr_keywords: tuple[str, ...] = field(default_factory=tuple)
    user_input_audio_format: str = 'ulaw_8000'
    # Turn-taking: PATIENT so the agent doesn't cut a caller off mid part-number.
    turn_eagerness: str = 'patient'
    turn_timeout: float = 12.0
    soft_timeout_message: str = 'Let me check that for you.'
    # Real escalation target (warm transfer); empty -> escalation speaks only.
    transfer_number: str = ''
    # TTS pronunciation dictionaries (alias rules), as {pronunciation_dictionary_id, version_id}.
    pronunciation_locators: tuple[dict, ...] = field(default_factory=tuple)
    # Workspace-secret id whose value the gateway validates (X-Agent-Token) —
    # containment so only this agent can call the tool. Empty = no auth header.
    auth_secret_id: str = ''
    tool_ids: tuple[str, ...] = field(default_factory=tuple)


def build_agent_payload(*, persona, tool_base_url: str,
                        system_prompt: str | None = None,
                        settings: AgentSettings | None = None,
                        inline_tool: bool = True) -> dict:
    """Assemble the exact ElevenLabs create-agent request body.

    `persona` is the same operator-configurable VoicePersona the runtime uses
    (gateway.persona) — so the hosted agent's greeting and voice come from ONE
    source of truth (SKU_VOICE_NAME / _ACCENT / _GREETING / _ID).
    """
    s = settings or AgentSettings()
    prompt = system_prompt if system_prompt is not None else load_system_prompt()
    agent_prompt: dict = {'prompt': prompt, 'llm': s.llm}
    tools: list[dict] = []
    if inline_tool:
        tools.append(resolve_part_tool(tool_base_url,
                                       auth_secret_id=s.auth_secret_id))
    tools.extend(system_tools(s.transfer_number))
    if tools:
        agent_prompt['tools'] = tools
    if s.tool_ids:
        agent_prompt['tool_ids'] = list(s.tool_ids)

    tts: dict = {
        'model_id': s.tts_model_id,
        'voice_id': persona.resolved_voice_id(),
        'stability': s.stability,
        'speed': s.speed,
        'similarity_boost': s.similarity_boost,
        'style': s.style,
    }
    if s.pronunciation_locators:
        tts['pronunciation_dictionary_locators'] = list(s.pronunciation_locators)

    asr: dict = {
        'quality': 'high',
        'provider': 'scribe_realtime',
        'user_input_audio_format': s.user_input_audio_format,
    }
    if s.asr_keywords:
        asr['keywords'] = list(s.asr_keywords)

    return {
        'name': f'Parts line — {persona.name}',
        'conversation_config': {
            'agent': {
                'prompt': agent_prompt,
                'first_message': persona.opening(),
                'language': s.language,
            },
            'asr': asr,
            'turn': {
                'turn_eagerness': s.turn_eagerness,
                'turn_timeout': s.turn_timeout,
                'soft_timeout_config': {
                    'timeout_seconds': 3.0,
                    'message': s.soft_timeout_message,
                    'use_llm_generated_message': False,
                },
            },
            'tts': tts,
        },
        'platform_settings': {'guardrails': guardrails_config()},
    }


# -- fault-injection check: the prompt must keep its guardrails ---------------

# Each check is (label, predicate over the lowercased prompt). These are the
# clauses that make the agent safe to put in front of a customer; a prompt that
# loses one must FAIL validation, not ship quietly.
def _has_all(text: str, *needles: str) -> bool:
    return all(n in text for n in needles)


REQUIRED_PROMPT_CLAUSES: tuple[tuple[str, object], ...] = (
    ('six-block: Personality', lambda t: '# personality' in t),
    ('six-block: Environment', lambda t: '# environment' in t),
    ('six-block: Tone', lambda t: '# tone' in t),
    ('six-block: Goal', lambda t: '# goal' in t),
    ('six-block: Guardrails heading', lambda t: '# guardrails' in t),
    ('six-block: Tools', lambda t: '# tools' in t),
    ('never-invent part facts',
     lambda t: _has_all(t, 'never invent', 'tool')),
    ('read the tool say verbatim',
     lambda t: 'verbatim' in t and 'say' in t),
    ('pricing gated behind verification',
     lambda t: _has_all(t, 'pricing is gated', 'verif')),
    ('no fitment/compatibility speculation',
     lambda t: 'speculate' in t and ('fitment' in t or 'compatib' in t)),
    ('resists instruction override',
     lambda t: 'ignore these rules' in t or 'hold your instructions' in t),
    ('names the resolve_part tool', lambda t: 'resolve_part' in t),
)


def validate_system_prompt(prompt: str) -> list[str]:
    """Return the labels of any required guardrail clause MISSING from the
    prompt. Empty list == safe to deploy."""
    t = prompt.lower()
    return [label for label, ok in REQUIRED_PROMPT_CLAUSES if not ok(t)]


# -- networked entry points (key-gated) --------------------------------------

def _post(url: str, payload: dict, *, api_key: str, method: str = 'POST') -> dict:
    import urllib.request
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        'xi-api-key': api_key, 'Content-Type': 'application/json'})
    try:
        import ssl

        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = None
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read())


def create_pronunciation_dictionary(name: str, rules: list[dict], *,
                                    api_key: str | None = None) -> dict:
    """Create an alias pronunciation dictionary (segment -> spelled-out form, e.g.
    'SBC' -> 'S B C') and return {pronunciation_dictionary_id, version_id} for
    use as a TTS locator. Alias (not phoneme): phoneme rules are silently ignored
    on flash/turbo v2.5 and are English-only."""
    key = api_key or os.environ.get('ELEVENLABS_API_KEY')
    if not key:
        raise RuntimeError('ELEVENLABS_API_KEY not set — cannot reach the API')
    body = {'name': name, 'rules': rules}
    r = _post(PD_RULES_URL, body, api_key=key)
    return {'pronunciation_dictionary_id': r.get('id'),
            'version_id': r.get('version_id')}


def create_or_update_agent(payload: dict, *, api_key: str | None = None,
                           agent_id: str | None = None) -> dict:
    """Create (or update, if agent_id given) the agent via the ElevenLabs API.
    Key-gated; refuses to deploy a prompt that lost a guardrail."""
    key = api_key or os.environ.get('ELEVENLABS_API_KEY')
    if not key:
        raise RuntimeError('ELEVENLABS_API_KEY not set — cannot reach the API')
    missing = validate_system_prompt(payload['conversation_config']['agent']
                                     ['prompt']['prompt'])
    if missing:
        raise ValueError(f'refusing to deploy: prompt missing guardrails {missing}')
    url = UPDATE_URL.format(agent_id=agent_id) if agent_id else CREATE_URL
    return _post(url, payload, api_key=key,
                 method='PATCH' if agent_id else 'POST')
