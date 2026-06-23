# Documentation

Start with the top-level [`README.md`](../README.md) for what the system is and
how to run it. These documents go deeper, grouped by area.

## Architecture & decisions
- [DECISION_LOG.md](DECISION_LOG.md) — the consequential design decisions, the
  alternatives weighed, and why (rules-own-output, the resolution paths, the
  fulfillment policies).
- [MATURITY.md](MATURITY.md) — the accurate per-capability map: what is fully
  tested vs. credential-gated vs. stub.
- [PRODUCTION_PLAN.md](PRODUCTION_PLAN.md) — what a path to production would look
  like, and what was deliberately left out of this work sample.
- [MIGRATION_NOTES.md](MIGRATION_NOTES.md) — notes from consolidating the
  prototype into this repo.

## Resolution core & catalog
- [RESOLUTION_LEARNING_LOOP.md](RESOLUTION_LEARNING_LOOP.md) — how confirmed
  corrections feed the live resolver through a gated eval battery.
- [VOCABULARY_SPEC.md](VOCABULARY_SPEC.md) — the family-word vocabulary the
  normalizer maps from rep phrasing to SKU families.

The never-invent guarantee is attacked directly in the test suite —
`tests/test_resolution_adversarial.py` (mutation fuzz, injection/unicode corpus,
tenant isolation) and `tests/test_gateway_adversarial.py`.

## Onboarding & ERP integration
- [ERP_ADAPTER_HARNESS_SPEC.md](ERP_ADAPTER_HARNESS_SPEC.md) — the least-privilege
  tenant-onboarding harness: discovery → human-gated profile → adapter + drift guard.
- [ERP_LIVE_RUNBOOK.md](ERP_LIVE_RUNBOOK.md) — connecting the harness to a real ERP.

## Conversational gateway & voice
- [CONVERSATIONAL_GATEWAY_SPEC.md](CONVERSATIONAL_GATEWAY_SPEC.md) — the
  customer-facing surface and its gates.
- [CONVERSATION_STATE_SPEC.md](CONVERSATION_STATE_SPEC.md) — session/turn state model.
- [VOICE_AGENT.md](VOICE_AGENT.md) · [VOICE_EXPERIENCE_DESIGN.md](VOICE_EXPERIENCE_DESIGN.md)
  · [VOICE_RUNBOOK.md](VOICE_RUNBOOK.md) — the voice front-end: design, persona,
  and how to run it against Twilio.

## Operations
- [OBSERVABILITY.md](OBSERVABILITY.md) — tracing (OTel → Phoenix), error tracking
  (Sentry), structured logs, the transcript journal, and the cost ledger; all
  off-by-default, fail-open, PII-scrubbed.

## Verification
- [NOISE_RESILIENCE_AUDIT.md](NOISE_RESILIENCE_AUDIT.md) — how the engine behaves
  on noisy input (typo / OCR / partial specs): it never invents and degrades
  gracefully. The complement to the round-trip audit's "high by construction".
- [PRODUCTION_VALIDATION_GATE.md](PRODUCTION_VALIDATION_GATE.md) — the boundary
  between what synthetic tests can prove and what only a real ERP / real audio /
  real customer DB can confirm.
- [LIVE_VERIFICATION.md](LIVE_VERIFICATION.md) — what the credential-gated smoke
  tests check against real providers.
