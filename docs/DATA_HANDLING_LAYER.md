# Data-Handling Layer: Reconciliation + Design

Precondition for pilot capture of real calls. A real parts call carries the
prospect's CUSTOMERS' PII — identity must have nowhere to persist before the
first call is captured.

---

## 1. Reconciliation with existing scrub_pii

The codebase already has **three scrubbing layers**, each at a different level of
aggression. The dispatch extends them rather than replacing or wrapping.

### What exists

| Layer | Location | Patterns | Used by |
|-------|----------|----------|---------|
| `scrub_pii` | `observability.telemetry` | EMAIL, PHONE, SSN, contextual ACCOUNT (keyword + 3-12 digits), bare ACCOUNT (9-12 digits), APIKEY, TOKEN | Trace attribute redaction, `pilot.capture.scrub_text` |
| `scrub` | `observability.service_improvement` | Any phone-shaped digit run, any 3-8 digit number | ImprovementLog records only |
| `scrub_text` | `pilot.capture` | `scrub_pii` + caller-name replacement (`[NAME]`) | `scrub_call` (RawCall -> ScrubbedCall) |

Plus `redact()` in telemetry: the REDACT-EVERYTHING default for trace attributes.
Structured attrs pass through; everything else is content (fail-closed), scrubbed
via `scrub_pii`, capped at 800 chars, or dropped entirely when content is disabled.

Plus `anon_key()` in service_improvement: one-way SHA-256 hash for tenant/account
identifiers — no identity recoverable.

### What the dispatch adds

The dispatch does NOT need a new regex engine. It needs:

1. **Company/org name scrubbing** — `scrub_text` handles caller names but not
   company/org names. In this domain, `Account.name` can be either a person name
   ("John Smith") or a company name ("Smith Plumbing"). Both are identity.
   Extension: `scrub_text` accepts a `companies` parameter alongside `names`, both
   applied as exact-match replacements with `[NAME]` / `[COMPANY]`.

2. **Formal field classification** — a type-level declaration that every field
   entering the ingestion pipeline is either IDENTITY (dropped) or CONTENT
   (retained). Today the guarantee is procedural (scrub_call is called correctly);
   the dispatch wants it structural (the content schema has no slot for identity).

3. **Demonstrate-the-catch** — a test suite proving the scrubber works before real
   calls go live.

### Reconciliation decision: EXTEND scrub_text, keep scrub_pii unchanged

- `scrub_pii` stays as-is — the regex engine for free-text PII patterns.
- `scrub_text` gains company/org handling (one new parameter).
- A new **field classification layer** sits ABOVE both, providing the structural
  guarantee that identity fields are dropped at ingestion, not stored-then-scrubbed.
- `service_improvement.scrub` stays as-is for its aggressive improvement-log role.
- `redact()` stays as-is for trace attributes.
- `anon_key()` stays as-is for tenant/account anonymization.

No parallel build. No wrapper. The existing scrubbing stack is correct; this adds
the structural guarantee and the missing company/org pattern.

---

## 2. Field Classification

Two classes. Classification is per-field, at ingestion, based on the field's role —
not its content.

### Identity (scrubbed always, never persisted)

| Field | Source | Existing coverage | Gap |
|-------|--------|-------------------|-----|
| Caller name | `Account.name`, spoken in call | `scrub_text(names=...)` -> `[NAME]` | None |
| Account number | Spoken/typed | `scrub_pii` contextual + bare patterns | None |
| Company/org name | `Account.name` (B2B), spoken | **Not covered** | New: `scrub_text(companies=...)` -> `[COMPANY]` |
| Phone number | Spoken/typed | `scrub_pii` `[PHONE]` | None |
| Email | Spoken/typed | `scrub_pii` `[EMAIL]` | None |
| SSN | Spoken/typed | `scrub_pii` `[SSN]` | None |

### Content (retained for training)

| Field | Description | PII risk |
|-------|-------------|----------|
| Part descriptions | "4-inch chrome stack", "aluminized elbow" | Low (product terms) |
| Call phrasing | How the customer phrases requests (scrubbed of identity) | Residual — scrub_pii catches known patterns |
| Call flow / sequence | Branch sequence: clarify -> establish_account -> disclose_price | None (structural, no free text) |
| STT confidence | Per-turn ASR confidence score (float) | None (numeric) |
| Resolution outcome | identified / ambiguous / unknown / not_a_part | None (enum) |
| Decision points | Structured: move, branch, resolved_sku, candidates | None (catalog SKUs, not PII) |
| Candidate SKUs | Catalog SKU strings | None (product identifiers) |

### Classification registry

A `FieldClass` enum (`IDENTITY`, `CONTENT`) and a frozen mapping from field name to
class. The ingestion pipeline checks every field against this registry. An
unregistered field is **rejected** (fail-closed), not silently classified.

---

## 3. Structural Content Schema

The keystone: identity has nowhere to persist.

### Current state

`ScrubbedCall` holds `(scrubbed_caller, scrubbed_rep)` string tuples. The strings
have been scrubbed, but the type doesn't prevent someone from passing raw text.
The guarantee is procedural (you must call `scrub_call` first).

### New: `RetainedContent`

A frozen dataclass that **structurally** has no identity fields. Every field is
either a content field or an opaque identifier (call_id). There is no `name`,
`account_number`, `phone`, `company`, or `email` field. Identity is dropped at
ingestion, not stored then scrubbed.

```python
@dataclass(frozen=True)
class RetainedContent:
    """What persists for training. Structurally has no identity fields —
    there is no field here that COULD hold a name, account number, phone,
    or company. Identity is dropped at ingestion, not stored-then-scrubbed."""
    call_id: str                        # opaque correlation key
    scrubbed_turns: tuple               # tuple[(scrubbed_caller, scrubbed_rep)]
    decision_points: tuple              # tuple[DecisionPoint] (catalog SKUs, not PII)
    flow_sequence: tuple                # tuple[str] — branch per turn
    resolution_outcomes: tuple          # tuple[str] — outcome per turn
    stt_confidence: tuple               # tuple[float | None] — per-turn ASR confidence
    stt_error_flags: tuple              # tuple[bool] — per-turn STT error flag
```

### Relationship to existing types

- `ScrubbedCall` stays — it's the transcript-only view used by the shadow replay.
  `RetainedContent` wraps `ScrubbedCall.turns` plus the structured decision trace.
- `ShadowIngest` stays — it's the full shadow-replay artifact (scrubbed transcript +
  divergence marker + tagged decisions). `RetainedContent` is the subset that
  persists for training purposes.
- The ingestion pipeline produces a `RetainedContent` from a `RawCall` + decision
  points. `RetainedContent` is what the training store accepts. The store's write
  method type-checks on `RetainedContent` — passing a `RawCall` is a type error.

### Construction invariant

`RetainedContent` is built ONLY by `build_retained_content()`, which:
1. Takes a `RawCall`, names, companies, and decision points
2. Runs checks on the raw text (STT-error detection, resolution check)
3. Scrubs via `scrub_text` (which calls `scrub_pii` + name/company replacement)
4. Extracts structural content (flow sequence, outcomes, confidence)
5. Returns a `RetainedContent` — the raw call is not returned alongside it

---

## 4. Demonstrate-the-Catch Plan

Two phases. Both must pass before any real call is captured.

### Phase 1: Known PII injection (hard pass/fail)

Inject a record containing every known identity pattern. Scrub. Assert every
identity marker is replaced; assert content survives.

```
Input:  "Hi, this is John Smith from Smith Plumbing, account 12345678,
         my number is 440-221-8112, email john@smithplumbing.com,
         I need a 4-inch chrome stack for a K5 assembly"

Names:     ["John Smith"]
Companies: ["Smith Plumbing"]

Assert GONE:  "John Smith", "Smith Plumbing", "12345678",
              "440-221-8112", "john@smithplumbing.com"
Assert PRESENT: "4-inch", "chrome stack", "K5 assembly"
```

Test cases:
- Caller name in mid-sentence
- Account number with "account" keyword prefix
- Account number bare (9+ digits)
- Phone number in various formats (+1, parens, dashes, dots)
- Email address
- Company name as possessive ("Smith Plumbing's order")
- SSN format
- Multiple identity items in one turn
- Identity item spanning a turn boundary (caller says name, rep repeats it)

### Phase 2: Novel PII patterns (coverage report — reviewed before go-live)

Inject records with PII patterns that stress the current regex boundaries. The test
does NOT hard-fail on a miss — it **reports the coverage map**. The go-live gate
is: every miss has been reviewed and either (a) the regex is extended to catch it, or
(b) the miss is documented as accepted residual risk with mitigation.

Novel patterns to test:

| Pattern | Example | Expected | Rationale |
|---------|---------|----------|-----------|
| International phone | "+44 20 7946 0958" | Likely MISS — regex is US-focused | Low risk in US B2B parts domain |
| Short account without keyword | "my number is 4837" | MISS by scrub_pii (3-8 digits without "account" keyword); CAUGHT by service_improvement.scrub | Covered at the improvement-log layer |
| Alphanumeric account | "ACC-78912-B" | MISS — regex expects pure digits | Evaluate prevalence in real accounts |
| Name embedded in description | "I need the Smith bracket" | MISS — name replacement is exact-match on known names | Residual; mitigated by the known-names list from CustomerDB |
| Company as part of address | "ship it to Smith Plumbing at 123 Main" | Company: CAUGHT (exact match). Address "123 Main": MISS | Address scrubbing is a new pattern — evaluate need |
| Multi-word company partial | "this is for Smith" (company is "Smith Plumbing") | MISS — exact match won't catch partial | Evaluate: add substring matching for companies? |

The coverage report is a committed artifact reviewed as part of the go-live gate.
Misses that cannot be fixed by regex extension are documented with their residual
risk level and mitigation (e.g., "international phone: low risk in US B2B context,
mitigated by service_improvement.scrub catching digit runs in the improvement log").

### Phase 3: Structural guarantee test

Verify that `RetainedContent` **cannot** be constructed with identity fields:
- Assert `RetainedContent` has no field named `name`, `account_number`, `phone`,
  `company`, `email`, or `caller_name` (introspection test).
- Assert the training store rejects anything that is not a `RetainedContent`.
- Assert `build_retained_content()` is the only public constructor (no direct
  instantiation with raw text possible without going through scrub).

---

## 5. STT-Error Flag Criteria + Audio Retention Design

### STT-error flag criteria

A turn is flagged as a probable STT error when ANY of:

1. **Low ASR confidence**: confidence < 0.60 (the threshold where transcription
   unreliability causes resolution failures — calibrate on initial pilot data,
   starting conservative)
2. **Resolution failure on a part-like utterance**: `looks_part_like(text)` is True
   AND resolution outcome is `no_match` AND the utterance is short (< 8 words) —
   a short part-like utterance that fails resolution is more likely an STT garble
   than a genuinely unknown part
3. **Re-ask detected**: the agent/rep asks the caller to repeat (detected by the
   orchestration's `move == 'clarify'` on consecutive turns for the same topic)

Flags are per-turn, stored in `RetainedContent.stt_error_flags`.

### Audio retention design

**Default**: audio dropped. No audio field in `RetainedContent`.

**Exception**: turns flagged as probable STT errors.

```
AudioRetentionStore
  ├── Separate from content store (different directory / table)
  ├── Keyed by (call_id, turn_index)
  ├── Stores: raw audio bytes (mu-law or PCM), call_id, turn_index, flag_reason, ingested_at
  ├── NOT readable from the training pipeline (no import path)
  ├── Access: diagnostic only (debugging STT failures)
  └── Deletion:
      ├── Retention window: 72 hours (configurable via SKU_AUDIO_RETENTION_HOURS)
      ├── Mechanism: reaper function called at ingestion time (check-and-delete-expired
      │   before writing new entries — no background daemon needed)
      └── Hard delete: file removed from disk, not soft-deleted
```

The retention window starts conservative (72 hours). This is enough to investigate
an STT failure noticed in the same-day review cycle but short enough that audio
doesn't accumulate. The window is a config knob, not hardcoded.

### Why not a background daemon

The reaper runs synchronously at ingestion (before writing new audio). In the pilot
phase, call volume is low (handful of calls/day), so the reaper adds negligible
latency. A background daemon adds operational complexity (crash recovery, orphaned
files) that isn't justified at pilot scale. If call volume grows, the reaper can be
extracted to a periodic task.

---

## 6. Invariant Review

All six invariants are sound. None challenged.

| # | Invariant | Status | Notes |
|---|-----------|--------|-------|
| 1 | Content store structurally has no identity fields | **Agree** | `RetainedContent` dataclass enforces this. No name/account/phone/company/email field exists in the type. |
| 2 | Scrubber demonstrate-the-catch proven before real calls | **Agree** | Phase 1 (hard gate) + Phase 2 (reviewed coverage report) + Phase 3 (structural test). |
| 3 | filter-on-raw-store-scrubbed ordering | **Agree** | Already implemented in `pilot.shadow.ingest`. `build_retained_content` follows the same discipline: checks run on raw, then scrub, then return only scrubbed. |
| 4 | Audio dropped by default, retained only for flagged STT errors | **Agree** | `RetainedContent` has no audio field. `AudioRetentionStore` is separate, access-controlled, time-bounded. |
| 5 | Training on de-identified content, identity link severed | **Agree** | `anon_key()` already severs the tenant/account link. `RetainedContent` carries no identity. The call_id is opaque (no identity derivable from it). |
| 6 | No global scrubber-off toggle | **Agree** | Existing `SKU_OBS_TRACE_CONTENT=0` is a content KILL switch (drops content entirely), not a scrub bypass. The scrubber itself has no off switch and the new field classification layer has none either. Per-field-class, defaults to scrub. Moving a field from IDENTITY to CONTENT would require a code change to the frozen classification registry — logged in version control, not a runtime toggle. |

---

## 7. Implementation Plan (post-approval)

### Files to create

| File | Purpose |
|------|---------|
| `src/pilot/field_class.py` | `FieldClass` enum, classification registry, registry validation |
| `src/pilot/content_schema.py` | `RetainedContent` dataclass, `build_retained_content()` constructor |
| `src/pilot/audio_retention.py` | `AudioRetentionStore` — separate store, reaper, time-bounded |
| `src/pilot/ingestion.py` | Ingestion pipeline: raw -> checks -> classify -> scrub -> RetainedContent |
| `tests/test_pii_demonstrate_catch.py` | Phase 1 + Phase 2 + Phase 3 of demonstrate-the-catch |
| `tests/test_field_classification.py` | Field classification registry tests |
| `tests/test_audio_retention.py` | Audio retention store + reaper tests |
| `tests/test_ingestion_pipeline.py` | End-to-end ingestion pipeline tests |

### Files to modify

| File | Change |
|------|--------|
| `src/pilot/capture.py` | Add `companies` parameter to `scrub_text` and `scrub_call` |
| `src/observability/__init__.py` | Re-export new symbols if needed |

### Ordering

1. `field_class.py` + tests (the classification is the foundation)
2. Extend `scrub_text` with company/org handling + tests
3. `content_schema.py` + `build_retained_content()` + tests
4. `audio_retention.py` + tests
5. `ingestion.py` (wires everything together) + tests
6. `test_pii_demonstrate_catch.py` (the go-live gate)
7. Commit + tag as the "capture-precondition-met" milestone

Each step is independently committable and testable.
