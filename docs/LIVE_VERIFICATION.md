# Live Verification — integrations checked against real services

The credential-gated smokes (never part of CI) were run against the real
backends on 2026-06-07. This is the record of what ran, what passed, and the
real bugs the run caught — evidence the integration code isn't just "tested
against my own assumption of the API."

> **Reproducibility note.** This is a self-report from development: the raw run
> logs are not committed to this repo. The smokes themselves *are* here
> (`tests/test_live_*.py`, skipped by default) — set the provider credentials
> and run them to reproduce these results with your own keys.

## What was run (all passed)

| Smoke | Real service | Proves |
|---|---|---|
| `tests/test_live_smoke.py` (3) | OpenRouter → `google/gemini-2.5-flash` | provider seam, retrieval chooser binds to candidates, cost ledger, hard spend cap (< $0.05) |
| `tests/test_live_voice_smoke.py::*connects*` | AssemblyAI Universal-Streaming v3 | wss connect + `Begin` + clean terminate, PCM16 @ 16 kHz |
| `tests/test_live_voice_smoke.py::*mulaw*` | AssemblyAI v3 | telephony **mu-law @ 8 kHz** + catalog **keyterms** accepted |
| `tests/test_live_voice_smoke.py::*tts*` | ElevenLabs | real `ulaw_8000` (Twilio-native) audio synthesized |
| `tests/test_live_twilio_smoke.py` | Twilio REST | account credentials live (read-only, no call placed) |

Run them yourself:

```sh
pip install -e ".[dev,voice,llm]"
# LLM (cheapest): OpenRouter → gemini-2.5-flash, spend-capped
OPENROUTER_API_KEY=… SKU_LIVE_PROVIDER=openrouter pytest tests/test_live_smoke.py -v
# Streaming STT + TTS
ASSEMBLYAI_API_KEY=… ELEVENLABS_API_KEY=… pytest tests/test_live_voice_smoke.py -v
# Twilio creds (read-only)
TWILIO_ACCOUNT_SID=… TWILIO_AUTH_TOKEN=… pytest tests/test_live_twilio_smoke.py -v
```

The live smokes are guarded by `verification_preflight` (conftest autouse): they
**skip** if the working tree has uncommitted code — you never verify live
behavior against code that isn't committed.

## Real bugs the live run caught (and fixed)

1. **Provider SDK not declared.** The OpenRouter/Anthropic paths need the
   `openai`/`anthropic` SDKs, which weren't in any extra → added `[llm]`.
2. **TLS `CERTIFICATE_VERIFY_FAILED`.** The streaming ASR and every urllib
   adapter (TTS, ERP HTTP/OAuth, web fetch) didn't configure a CA bundle, so the
   handshake failed on interpreters without a system trust store → a shared
   `certifi` CA bundle is now the default opener.
3. **AssemblyAI `keyterms_prompt` format.** Sent as a comma-joined string;
   v3 returned `error 3006: Invalid JSON array`. Diagnosed from the server's own
   message; fixed to a JSON array.

These are exactly the failures unit tests against synthetic payloads cannot
catch — "runs clean" ≠ "correct against the real wire."

## Still requires a human
A full Twilio **phone round-trip** (dial the number, hear the bot answer,
resolve, speak back) needs someone to place the call and a public URL — see
`docs/VOICE_RUNBOOK.md`. Everything up to that (creds, TwiML, signature, ASR,
TTS) is verified.
