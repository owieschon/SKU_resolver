# Voice agent: hosted speech shell over the deterministic gateway

## The decision

Two ways to run a phone agent over this gateway:

1. **Self-hosted media pipeline** — Twilio Media Streams → streaming STT →
   gateway → TTS, all wired in `runtime/app.py` (`/voice-stream`). We built and
   live-tested this. It works, but it makes us responsible for turn-taking,
   barge-in, small talk, greeting cadence, and voice quality — the parts that a
   hosted voice-agent platform already solves well, and that a live test call
   showed are hard to get right by hand (robotic cadence, jumping the caller).

2. **Hosted speech shell + gateway-as-a-tool** (this document) — an ElevenLabs
   Agent owns speech, turn-taking, small talk, and the natural voice; for **any**
   part/availability/price question it calls **one** server tool, `resolve_part`,
   which is our deterministic gateway (`POST /agent/turn`). The agent never
   produces a SKU or a price itself — it can only relay what the tool returns.

We chose (2) for the conversational surface and kept (1) in the tree as the
self-hosted fallback. The division of labor:

| Concern | Owner |
|---|---|
| Natural voice, turn-taking, barge-in, small talk, greeting | ElevenLabs Agent |
| Speech-to-text | ElevenLabs Agent (hosted STT) |
| **Resolving the part** (grammar + retrieval + never-invent) | **gateway tool** |
| **Availability / lead time** | **gateway tool** |
| **Pricing behind account verification** | **gateway tool** |
| **Escalation to a human** | **gateway tool** |
| Self-improvement data capture | gateway (`observe_agent_turn`) |

The thesis of the whole repo holds unchanged: **rules own every binding fact;
the model only handles speech and phrasing.** Moving speech to ElevenLabs does
not move the guarantee — it stays in code, behind the tool.

## What the agent can and cannot do

The agent's *only* freedom is to be a friendly human voice. Every part fact is
gated by the tool, enforced two ways:

- **In code (primary).** `/agent/turn` runs the same `gateway.turn` as every
  other channel: never-invent (every RESOLVED answer references a real catalog
  row), pricing refused until the session is verified, escalation on repeated
  failure. The agent cannot reach a SKU, a price, stock, or a ship date except
  as the `say` string the tool hands back. No prompt can change that.
- **In the prompt + platform guardrails (defense in depth).** The system prompt
  (`voice_agent/SYSTEM_PROMPT.md`) and the ElevenLabs Guardrails control layer
  tell the model the same thing, so it does not *try* to free-lance and then get
  blocked. Belt and suspenders.

## Grounding (authoritative ElevenLabs sources)

The system prompt and guardrail config follow ElevenLabs' own guidance
(fetched 2026-06-07):

- **Six-block prompt** — Personality, Environment, Tone, Goal, Guardrails,
  Tools. Models pay special attention to the `# Guardrails` heading; the most
  important rules are stated twice and tagged "This step is important."
  <https://elevenlabs.io/docs/eleven-agents/best-practices/prompting-guide>
- **Guardrails** — four types (Focus, Manipulation, Content, Custom) across
  three layers (system-prompt hardening, user-input validation, independent
  agent-response validation). The binding guarantee lives in the prompt's
  `# Guardrails` section (always sent) and in code at `/agent/turn`; the
  ElevenLabs *platform* Guardrails 2.0 layer is a versioned object we enable in
  the dashboard (our intended policy — Focus + Manipulation + Content + a custom
  `parts_only_from_tool` rule — is version-controlled in
  `runtime/voice_agent.py:guardrail_config()`). Voice runs in streaming mode; on
  a hard violation the call ends rather than risk an unmandated value.
  <https://elevenlabs.io/docs/eleven-agents/best-practices/guardrails>
- **Hallucination prevention** — ground the agent in tool/knowledge data and
  instruct "never guess or make up information"; we go further and give it *no*
  part knowledge of its own — the tool is the only source.
  <https://elevenlabs.io/docs/eleven-agents/best-practices/prompting-guide>
- **TTS-friendly phrasing** — the gateway already returns speech-shaped text
  (`gateway/spoken.py`: "5 by 24 inch", not `5"X24`), so the prompt tells the
  agent to read `say` verbatim and not re-render the numbers.
- **Server (webhook) tool + create-agent API** — the tool and payload shapes in
  `runtime/voice_agent.py`.
  <https://elevenlabs.io/docs/eleven-agents/customization/tools/server-tools> ·
  `POST https://api.elevenlabs.io/v1/convai/agents/create` (header `xi-api-key`)

## Data-capture parity

The self-hosted path feeds the always-on self-improvement loop (flagging
uncertain moments for periodic human review, see `gateway/shadow.py`). The
hosted path keeps that parity: `/agent/turn` calls
`ContinuousImprovement.observe_agent_turn(text)` on every turn (active only when
`SKU_IMPROVEMENT` is configured), so moving speech off-box does not lose the
"improve the service" signal.

## Persona is one source of truth

The hosted agent's greeting and voice come from the **same** operator-configurable
`VoicePersona` the local runtime uses — `build_agent_payload(persona=…)` reads
`first_message` from `persona.opening()` and `voice_id` from
`persona.resolved_voice_id()`. So `SKU_VOICE_NAME`, `SKU_VOICE_ACCENT`,
`SKU_VOICE_GREETING`, and `SKU_VOICE_ID[_<ACCENT>]` configure the ElevenLabs
agent exactly as they configure the phone stack. No second place to edit.

## Deploy

The build and validation are pure (no network, no key) — CI exercises them in
`tests/test_voice_agent_config.py`. Only `--apply` touches the API.

```bash
# 1. Confirm the prompt still carries every non-negotiable guardrail:
python scripts/elevenlabs_agent.py --validate

# 2. Review the exact create payload (no network):
AGENT_TOOL_BASE_URL=https://<public-host> \
    python scripts/elevenlabs_agent.py --dry-run | less

# 3. Stand up a public URL the ElevenLabs agent can reach for /agent/turn
#    (cloudflared quick tunnel or your ingress), then create the agent:
AGENT_TOOL_BASE_URL=https://<public-host> \
ELEVENLABS_API_KEY=... \
    python scripts/elevenlabs_agent.py --apply
# update later with: --apply --agent-id <id>
```

`create_or_update_agent()` re-runs `validate_system_prompt()` and **refuses to
deploy** a prompt that has lost a guardrail — the same check the tests assert.

Schema notes (live-verified against the create API, 2026-06-07): a POST webhook
tool uses `api_schema.request_body_schema` (not `body_params_schema`);
`caller_id` binds to the `system__conversation_id` system dynamic variable;
English agents must use `eleven_turbo_v2` or `eleven_flash_v2` TTS (we use flash
v2 for the lowest phone latency). A successful `--apply` returns an `agent_id`;
verify the tool fires end-to-end without a phone via
`POST /v1/convai/agents/{id}/simulate-conversation` (supply
`simulation_specification.dynamic_variables.system__conversation_id`, which real
calls inject automatically).

### First-deploy checks (do these in the ElevenLabs dashboard)

- **`caller_id` binding.** The tool sends `caller_id` so the gateway session —
  and thus account verification — persists across the call. We bind it to the
  system dynamic variable `{{system__conversation_id}}`; confirm the agent is
  actually injecting it (not letting the model fill it), so concurrent callers
  do not share a session.
- **LLM id.** `SKU_AGENT_LLM` (default `claude-opus-4-7`) must match a model
  ElevenLabs currently serves (`GET /v1/convai/llm`). EU data-residency
  workspaces restrict some models.
- **Attach a phone number** to the agent (or point your Twilio number at the
  ElevenLabs SIP/inbound integration) and place a test call.

## What we give up vs. the self-hosted path

- **STT vendor choice / keyterm biasing.** ElevenLabs uses its own hosted STT;
  we lose the AssemblyAI keyterm-prompt tuning. Mitigation: the gateway's
  deterministic grammar already absorbs garbled alphanumerics, and the readback
  states the decoded part back for confirmation.
- **Full transcript control.** The hosted agent owns the media; our per-turn
  `assistant` events on `/voice-stream` are richer. Mitigation: `/agent/turn`
  still journals every turn through the gateway and feeds the improvement loop.
- **A second dependency** (ElevenLabs Agents, not just TTS). Accepted: it buys
  the conversational quality a live call showed we should not hand-roll.
