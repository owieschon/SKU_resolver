# Observability

Three signals — traces, errors, logs — plus a PII-scrubbed transcript journal
and a cost ledger. Everything is **off by default, fail-open, and PII-scrubbed**:
nothing phones home unless you set an env var, any backend error degrades to a
no-op (a misconfigured deploy still boots), and every attribute/message passes a
redaction chokepoint before it can leave the box.

## Tracing (OpenTelemetry → Phoenix / any OTLP backend)

Spans nest per request, so a whole turn is one trace:

```
gateway.turn                      (src/gateway/orchestrator.py)
 └─ resolve.turn                  (src/resolution/service.py)
     └─ llm.<task>                (src/model_provider/client.py)   # only if the
                                    proposer is invoked — never in the hot path
```

Span attributes are scalar labels (`svc.outcome`, `svc.source`, `llm.model_name`,
`llm.cost.total`, `svc.tenant_id`, …); any free-text attribute (`input.value`,
`output.value`) goes through `redact()` → `scrub_pii()` and is dropped entirely
unless content capture is explicitly enabled.

Enable it:

```bash
pip install -e ".[trace]"
export SKU_OBS_TRACING=1
export SKU_OBS_OTLP_ENDPOINT=http://localhost:6006/v1/traces   # Arize Phoenix
python -m phoenix.server.main serve   # or any OTLP collector
```

Phoenix ingests OTLP directly, so the `llm.<task>` spans (model, tokens, cost,
latency) show up as an LLM trace with no extra instrumentation. There is **no
LangChain/LangGraph** here — the engine is deterministic and an LLM is only a
*proposer* — so OTel (framework-agnostic) is the right tracer rather than a
framework-specific tracer.

Implementation: `src/observability/telemetry.py` (`tracer`, `init_tracing`,
`set_attr`, the redaction chokepoint).

## Error tracking (Sentry)

Off unless `SENTRY_DSN` is set and `sentry-sdk` is installed (`.[serve]`):

```bash
pip install -e ".[serve]"
export SENTRY_DSN=https://...@oXXXX.ingest.sentry.io/XXXX
```

`init_error_tracking()` runs in `create_app()` with `send_default_pii=False` and
a `before_send` hook that runs `scrub_pii` over the message + exception values —
an error report must not carry a customer's account number off-box.
Implementation: `src/observability/errors.py`.

## Structured logs

One logger tree under `sku`, emitting greppable `event key=value` records; level
from `SKU_LOG_LEVEL` (default `WARNING`). The seam that matters most is the
gateway fault path: an internal dependency fault is turned into a coherent
customer escalation (never a 500), but it is **still surfaced** as a
`gateway.internal_fault` WARNING (and journaled) so it is never silently
swallowed. Implementation: `src/observability/logs.py`.

## Transcript journal

`src/gateway/journal.py` persists every turn to `state/conversation.jsonl` with
all free-text fields scrubbed via `observability.scrub_pii` before write —
auditable conversation history that never stores a raw account number or contact.

## PII & data handling

PII never has to leave the box in the clear. Three scrubbing layers, each at a
different level of aggression, sit in front of every outward path:

- `observability.scrub_pii` (`telemetry.py`) — the redaction chokepoint every
  span attribute, log field, and journal line passes through (emails, phones,
  SSNs, spoken/typed account numbers, API keys, long tokens → placeholders).
- the gateway journal (`gateway/journal.py`) scrubs free-text fields before they
  touch disk.
- `observability.scrub` (`service_improvement.py`) — anonymizes captured
  improvement examples.

The Sentry hook (`errors.py`) reuses `scrub_pii` in `before_send`, and the
catalog/inventory shipped here are synthetic (no real customer, vendor, or
financial data — see the top-level README), so even the fixtures carry nothing
sensitive.

## Cost ledger

`src/observability/cost.py` records per-call model cost with a per-session budget
and anomaly flags, fed from the `llm.<task>` seam.

## Diagnosing a failure after the fact

1. The `gateway.turn` trace shows the path taken (intent → resolve → maybe LLM),
   the outcome, and any `svc.refused` reason.
2. A swallowed internal fault appears as a `gateway.internal_fault` log line and
   an `ESCALATED / internal_fault` journal entry with the exception type.
3. Sentry (if enabled) carries the stack trace, PII-scrubbed.
