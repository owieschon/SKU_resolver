# Voice Runbook — calling the gateway on a real phone

How to dial a Twilio number and talk to the parts agent. This uses the TwiML
`<Gather>` request/response flow: Twilio does speech-to-text and POSTs the
transcript to `/voice`; the gateway runs a turn on the VOICE channel (same
gates, same never-invent guarantee as chat) and speaks the reply. AssemblyAI
streaming with catalog keyterms is the later fidelity upgrade (see end).

## What you need
- A Twilio account + a voice-capable number. Set `TWILIO_ACCOUNT_SID`,
  `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` in the environment (from an
  out-of-repo `.env` / secret store — never committed).
- `ngrok` (or any HTTPS tunnel) for local testing — Twilio must reach a
  public HTTPS URL.

## Steps (local, ~5 minutes)

1. **Run the server** (local defaults, no external deps needed for the demo):
   ```sh
   pip install -e ".[serve]"
   uvicorn runtime.app:create_app --factory --port 8000
   ```
   Optional, for the LLM-backed intent/chooser: also set
   `SKU_LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=...` (or `openrouter`).
   For a real session secret: `SKU_SESSION_SECRET=...`.

2. **Expose it**:
   ```sh
   ngrok http 8000        # note the https URL, e.g. https://ab12.ngrok.app
   ```

3. **Point the Twilio number at it.** In the Twilio console (Phone Numbers →
   your number → Voice Configuration → "A call comes in"):
   - Webhook: `https://<your-ngrok>.ngrok.app/voice`
   - Method: `HTTP POST`
   Save.

4. **Call the number** and talk:
   - "Is K5-24SBC in stock?" → it reads the part back ("I have K5-24SBC,
     chrome curved stack — what diameter or finish?"), you confirm, it gives
     availability + lead time.
   - "How much for that one?" before verifying → it declines and offers to
     verify your account.
   - "My account number is 1001" → verified; then pricing is shared.
   - "I need to talk to someone" or "where's my order" → it escalates (and
     transfers to `VOICE_TRANSFER_NUMBER` if you set one).

## Notes
- **Voice always reads a part back before acting** — by design (the readback
  is the human-in-the-loop that makes attended voice shippable; see
  CONVERSATIONAL_GATEWAY_SPEC §2.5 #11). A bare "yes" is a weak confirm and
  won't satisfy pricing.
- **No price is ever spoken before account verification**, and no fabricated
  SKU can be spoken — the gateway's gates apply identically over the phone.
- **Cost**: a Twilio number is ~$1/mo; inbound calls ~$0.0085/min. A few test
  calls cost pennies.
- **Twilio signature validation** is enforced on `/voice` whenever
  `TWILIO_AUTH_TOKEN` is set: the `X-Twilio-Signature` header is verified
  (HMAC-SHA1 over the URL + sorted POST params, `runtime/twilio_sig.py`) and a
  bad/missing signature gets a `403` before any session opens. In local/ngrok
  dev with no token configured, validation fails open (skipped) so you can test
  without it — set the token to exercise the production path. No `twilio` SDK
  needed; it's pure stdlib.

## Voice persona (operator-configurable)
The agent's name, speaking style, accent, and voice are config — no hardcoded
character. Set via env (all optional):
- `SKU_VOICE_NAME` — what it calls itself in the greeting (e.g. "Sam at the parts desk").
- `SKU_VOICE_ACCENT` — one of `standard | northeast | midwest | southern |
  west_coast | british | australian`. Accent selects the ElevenLabs voice (TTS
  is the speech-OUT provider; AssemblyAI is speech-IN only and has no voices).
  **Reality:** ElevenLabs stock voices distinguish `american / british /
  australian`. `british`/`australian` are genuinely accented; the US-regional
  slots default to real, distinct *American* voices — a TRUE regional accent
  (e.g. southern) needs a curated or cloned ElevenLabs voice supplied via
  `SKU_VOICE_ID_SOUTHERN` (etc.).
- `SKU_VOICE_ID` — explicit provider voice id; overrides the accent mapping.
- `SKU_VOICE_STYLE` — tone descriptor (e.g. "friendly, concise, professional").
- `SKU_VOICE_GREETING` — override the generated opening line entirely.

`gateway/persona.py` holds the `VoicePersona` (pure) + accent→voice presets;
`build_persona()` reads the env; the persona's resolved voice id is passed to
`ElevenLabsTTS`, and its greeting is spoken on both the `<Gather>` and
Media-Streams paths. The accent voice ids are placeholders to replace with your
provider's actual voices.

## Fidelity upgrade — Streaming STT (built; the durable path)
The `<Gather>` flow uses Twilio's built-in speech-to-text. The higher-accuracy
path (the H1 call-capture finding) is **Twilio Media Streams → AssemblyAI
Universal-Streaming v3** with catalog-derived keyterms. This is built:

- **Why STT-only, not the AssemblyAI Voice Agent API:** the Voice Agent API
  runs a hosted LLM that *speaks the reply* — that would put an uncontrolled
  model in the binding seat and break never-invent + the pricing gate. We use
  Streaming STT (transcribe-only); the deterministic gateway stays the brain.
  (DECISION_LOG, 2026-06-07.)
- **Pieces:** `gateway/voice_stream.py` parses the Twilio Media Streams frame
  envelope and decodes G.711 mu-law → PCM16 (hand-rolled; `audioop` was removed
  in Python 3.14). `gateway/asr_streaming.py` has the `StreamingASR` seam:
  `SimulatedStreamingASR` (CI) and `AssemblyAIStreamingASR` (live v3 client:
  `wss://streaming.assemblyai.com/v3/ws`, `Authorization: <key>`, binary audio,
  `Turn`/`end_of_turn` messages). Forward mu-law @ 8 kHz directly
  (`encoding=pcm_mulaw`) — no resample.
- **Wiring:** point the number at the WebSocket with `<Connect><Stream>`
  (`runtime.twiml.connect_stream`). The `/voice-stream` endpoint
  (`runtime/app.py`) ingests frames, transcribes, and runs a gateway turn per
  finalized utterance — same gates as chat. It boots with a no-op simulated ASR
  and switches to AssemblyAI automatically when `ASSEMBLYAI_API_KEY` is set.
- **Credentials:** `ASSEMBLYAI_API_KEY` (kept in an out-of-repo `.env`, never
  copied into the repo, loaded at runtime). Install the extra:
  `pip install '.[voice]'`.
- **Tested:** mu-law decode, frame parsing, and the stream→ASR→gateway bridge
  run in CI with the simulated ASR; `tests/test_live_voice_smoke.py` exercises
  the real AssemblyAI socket (credential-gated, skipped in CI).
- **Full-duplex reply leg (built):** `/voice-stream` speaks each gated reply
  back over the same WebSocket — TTS → mu-law → Twilio `media` frames + a `mark`
  — *and* emits a JSON `assistant` event for transcript/supervisor use. The
  `TTS` seam (`gateway.tts`) runs `SimulatedTTS` in CI; set `ELEVENLABS_API_KEY`
  to use `runtime.tts_adapters.ElevenLabsTTS` (requests `ulaw_8000`, Twilio-
  native, no resample). The gateway is still the brain — every gate runs before
  a single audio frame is synthesized, so TTS only ever voices words the
  deterministic gateway already chose.
