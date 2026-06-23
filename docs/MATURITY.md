# Maturity Map

A straight answer to "what's actually real here?" — written so a reviewer can
trust the rest of the repo. Every capability is labelled:

- **PROD** — production-grade, runs as-is.
- **PROD (sim default)** — production logic behind a protocol; CI runs a
  deterministic/simulated implementation, a real adapter drops in by config.
- **GATED** — real implementation exists but only runs with credentials; CI
  skips it. The decision/parsing logic is extracted and tested; only the live
  I/O is unexercised in CI.
- **STUB** — intentionally not built; raises or is documented as deferred.

The governing rule everywhere: **rules own canonical output and all binding
fields; LLMs/agents propose, extract, and draft only.** Never-invent holds at
every layer — including voice and onboarding.

---

## Capability map

| Capability | Status | What's real / what's not |
|---|---|---|
| SKU resolution (deterministic grammar, 9,487-SKU catalog) | **PROD** | Identity gate 9,487/9,487; round-trip 96.96% (floor 95%); 0 pinned construct truncations. |
| Ship-date / fulfillment engine | **PROD** | Pure, total function; golden + property tests; calendar horizon raises, never silently wrong. |
| Conversational gateway (chat) | **PROD** | All gates (never-invent, pricing-behind-verification, discriminating readback, escalation) tested incl. adversarially. |
| Retrieval chooser / intent / ERP explorer (LLM) | **PROD (sim default)** | Deterministic impls in CI; LLM impls behind protocols, bind-guarded so a hallucination is caught, never trusted. |
| Catalog grammar induction (unknown tenant) | **PROD** | Learns grammar from strings; assumptions are proposals w/ evidence+confidence; SME questions; iterates to diminishing returns. Validated on a real unseen vendor PDF. |
| Catalog ingestion: PDF / Excel / web | **PROD (sim default)** | Pure row-extractors tested in CI; PDF reader (pypdf) is the only file-gated piece. |
| Customer DB / price book | **PROD (sim default)** | In-memory/synthetic in CI; **SQLite adapters fully CI-tested** (stdlib); config selects by env. |
| ERP adapter harness (recon→discover→verify→profile→drift) | **PROD (sim default)** | Runs against the in-process BC twin in CI with the full fault-injection check matrix; real ERP via the HTTP backend below. |
| ERP HTTPS transport + OAuth (BC SaaS) | **GATED** | `erp_transport.HttpBackend` + `OAuthClientCredentials`. Request building, bearer header, JSON parse, 429/403 pass-through, timeout→TransportTimeout, OAuth caching/refresh all unit-tested with a mocked transport. Live-tenant runs gated behind the twin matrix (spec §6). |
| Voice — Twilio `<Gather>` bot | **PROD** | Callable end-to-end; Twilio does ASR, the gateway does every decision; signature-validated. |
| Voice — Streaming STT + TTS (Twilio Media Streams → AssemblyAI v3 → TTS) | **PROD (sim default)** | Full duplex: frame parsing, mu-law codec, Turn parsing, and the TTS reply leg run in CI with `SimulatedTTS`/scripted ASR; live AssemblyAI + ElevenLabs (`ulaw_8000`) drop in by key. |
| ERP NAV on-prem (SQL) | **GATED** | `erp_transport.SqlBackend` — OData-shaped drop-in (EDMX from INFORMATION_SCHEMA, OData paging); the SAME `discover()` runs over it. Logic unit-tested with an injected runner; live `from_pyodbc` gated (`[erp]`). |
| Web catalog ingestion (incl. JS-rendered) | **PROD (sim default)** | Static + injected-fetcher paths CI-tested; `playwright_fetcher` for JS grids is gated (`[web]` + `playwright install`). |
| LLM providers (Anthropic / OpenAI / OpenRouter) | **GATED** | Response parsing + token/cost mapping unit-tested against synthetic payloads; live calls run only with a key (cheap-tier, spend-capped smoke). |
| `ERPCatalogIndex` (Supabase-backed) | **STUB** | Raises `NotImplementedError` by design — superseded by the harness adapter path. |
| P21 / Eclipse ERP classes | **STUB** | Documented `unsupported` (no instance access); named, not silently missing. |

---

## Test posture

- **585 passing / 10 skipped** on a clean `pip install -e ".[dev]"` clone (the
  skips are the credential-gated live-API smokes), green in CI on Python 3.12.
- **85% line coverage** (`pytest --cov=src`). The remainder is concentrated in
  the **GATED** modules' live I/O — the real WebSocket/SDK/socket calls that
  cannot run in CI. For each, the decision/parsing logic is extracted into a
  pure function that **is** covered (`parse_turn_message`,
  `parse_anthropic_response`, `parse_openai_response`, `HttpBackend` against a
  mocked `urlopen`). So "59% on `asr_streaming`" means the socket plumbing is
  live-only, not that the logic is untested.
- **Fault-injection check**: detectors are validated against *planted* faults,
  not just clean runs — schema mutations, write attempts, drift, a wrong ERP
  mapping, a planted unknown SKU grammar, spoofed Twilio signatures, malformed
  audio frames, degenerate catalogs.
- **Gated suites** (never in CI, opt-in): `test_live_smoke.py` (real LLM,
  spend-capped < $0.05), `test_live_voice_smoke.py` (real AssemblyAI), the
  live PDF case in `test_catalog_source.py`.

---

## Live-verified (2026-06-07) — checked against real services, not just stand-ins

Self-reported from development (run logs not in this repo; reproduce with your
own keys — see `docs/LIVE_VERIFICATION.md`). The credential-gated smokes were
run against real backends. All pass; the run
caught and fixed four real integration bugs (missing provider SDK extra;
TLS `CERTIFICATE_VERIFY_FAILED` on the streaming ASR and all urllib adapters,
fixed with a certifi CA bundle; AssemblyAI `keyterms_prompt` needing a JSON
array). Proven: **LLM** (OpenRouter→gemini-2.5-flash: intent, chooser, cost cap,
spend-capped), **AssemblyAI Streaming STT** (pcm16 @16k and telephony mu-law
@8k with catalog keyterms), **ElevenLabs TTS** (real ulaw_8000 audio), and
**Twilio** credentials (account active, read-only check). Still requires a human
to place an actual phone call (and a public URL) for a full voice round-trip.

## Known gaps (named, not hidden)

1. **Live-tenant ERP** runs are deliberately gated behind a green twin
   fault-injection check matrix; no live run has occurred. The transports
   (BC `HttpBackend`, NAV `SqlBackend`) and the one-command launcher
   (`build_live_enforcer`, `docs/ERP_LIVE_RUNBOOK.md`) are built and
   unit-tested; what's unproven is a real tenant's quirks.
2. **Real voice quality** is unmeasured: the full-duplex loop, framing, codec,
   and seams are tested, but actual STT accuracy / TTS naturalness need the live
   AssemblyAI + ElevenLabs keys and a real call. The catalog-keyterms accuracy
   lever is wired but not field-measured.
3. **Catalog classifier category axis.** On the WA PDF the engine-line segment
   auto-decodes from fitment; the *category* segment correctly stays an SME
   question (its evidence — section taxonomy — is noisier). Cleaner section
   parsing would lift it further; today it degrades to a question, not a wrong
   answer.
4. **Web JS rendering** needs `[web]` + `playwright install` (a browser); the
   static + injected-fetcher paths are CI-tested, the headless render is gated.
5. **Normalizer vocabulary** has a few `TODO(sme)` family-word gaps that need
   SME input — exactly the kind of question the induction layer now generates.

---

## "Go to production" per capability (config, not code)

| To enable | Set |
|---|---|
| Real LLM seams | `SKU_LLM_PROVIDER=anthropic\|openai\|openrouter` + the matching `*_API_KEY` |
| Persistent customer/price data | `SKU_CUSTOMER_DB=...db` / `SKU_PRICEBOOK_DB=...db` |
| Real catalog file | `SKU_CATALOG_PATH=...csv` (+ `pip install '.[pdf]'` for PDF) |
| Live voice (Gather) | `TWILIO_AUTH_TOKEN` (enables signature enforcement) + point the number at `/voice` |
| Live voice (Streaming STT + spoken replies) | `ASSEMBLYAI_API_KEY` (+ `ELEVENLABS_API_KEY` for TTS) + `pip install '.[voice]'` + point at `/voice-stream` |
| Real BC SaaS ERP | `SKU_ERP_KIND=bc` + base-URL/OAuth env, then `build_live_enforcer()` (see `docs/ERP_LIVE_RUNBOOK.md`) |
| Real NAV on-prem ERP | `SKU_ERP_KIND=nav` + `SKU_ERP_SQL_DSN` + `pip install '.[erp]'` |
| JS-rendered web catalogs | `pip install '.[web]' && playwright install chromium`, use `playwright_fetcher` |
| Session/webhook security | `SKU_SESSION_SECRET`, `SKU_WEBHOOK_SECRET` |
