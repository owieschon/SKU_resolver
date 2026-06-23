# Decision Log

Format: Decision / Why / Alternatives rejected / Outcome / Status.
Entries are locked BEFORE the code that depends on them is written.

Entries from 2026-06-06 onward are this repo's. The section below backfills the
**foundational decisions made before this repo existed** (in the originating
SKU-translator / call-capture projects), so the log is self-contained. These
are recorded as historical context — the evidence (locked, pre-registered
experiment records) lives in prior private work (under NDA) and is quoted, not
re-derived, here.

---

## Pre-repo foundations (2026-05-01 → 2026-05-11) — historical context

**F1 — Deterministic grammar over an LLM for canonical resolution.** The core
decoder (315-pattern regex grammar + bucket-scoped Levenshtein +
extract/construct invertibility) was built deterministic from the start. *Why:*
wrong-but-plausible SKUs are the worst failure class (they pass human review);
a model that "usually" gets it right is the wrong tool where confident error is
costlier than an honest "needs review." This is the thesis the whole repo
inherits. **Status:** locked; productized as `src/sku_translator/`.

**F2 — The voice few-shot falsification.** LLM extraction of SKUs from degraded
voice transcripts scored **0.000 recall** zero-shot AND few-shot (experiment
SS10.5, conjunction gates pre-registered before the run); failures were
hallucinated phonetically-plausible SKUs absent from the catalog. The
deterministic engine on the same corpus: 0.44 direct / 0.64 rep-solvable Heavy
recall at zero LLM calls, failing as PENDING/UNRESOLVABLE not fabrication.
*Outcome:* LLM excluded from the binding path; reserved for proposal/extraction.

**F3 — LLM as chooser, not author (retrieval arc).** Given the catalog as a
K=25 candidate window, an LLM chose correctly **88.2%** (71.4% corrected for the
full active-SKU base) — good chooser, dangerous author. *Outcome:* the
retrieval chooser seam (`resolution/chooser.py`) binds the choice to the
retrieved set, so a hallucination is caught, never resolved. Hybrid BM25+MiniLM
dense retrieval at K=20–30 + LLM-mediated selection was validated as the
production retrieval architecture (call-capture experiments, closed 2026-05-02).

**F4 — The catalog-decoding methodology.** The grammar-decode approach
(segmentation, family-by-prefix, per-family regex, positional roles) was
developed and applied twice (the example catalog and second example catalogs) as a
hand-run artifact before being productized. *Outcome:* `erp_harness/
grammar_induction.py` is its agent-executed, tenant-agnostic form (C4); the
multi-field classifier mode (2026-06-07) extends it past what the hand artifact
did.

**F5 — Observability harvested, not invented.** Tracing / cost-ledger /
deploy-guard / safe-state patterns were lifted from a prior agent stack and
made domain-neutral and off-by-default, rather than rebuilt. *Outcome:*
`src/observability/`.

---

## 2026-06-06 — D1: Receipt normalization and the 5 PM rule

**Decision.** The fulfillment rule "in-stock orders ship by 5:00 PM the
following business day" is implemented as: normalize the receipt timestamp
first — an order received after the 17:00 cutoff, or on a weekend/holiday,
is treated as received at the open of the next business day — then
`ship_by = 17:00 facility-local on the business day after the normalized
receipt day`. Examples: Friday 14:00 → Monday 17:00. Friday 18:00 →
(received Monday) → Tuesday 17:00. Saturday any time → Tuesday 17:00.

**Why.** The stated rule covers the common case; receipt normalization is
the smallest consistent extension to the uncovered cases (a warehouse that
closes at 17:00 cannot "receive" later orders into that day's queue).
Cutoff (17:00) and ship-by hour (17:00) are separate named constants —
coinciding by policy today, independently changeable.

**Alternatives rejected.** (a) Calendar-day rule (Sat → Mon ship): breaks
the "following business day" framing for weekend receipts and silently
gives weekend orders faster service than Friday-evening ones. (b) No
cutoff: implies a 23:59 order ships in 17 hours, faster than the rule
intends for any other hour.

**Outcome.** Encoded in golden tests G02-G06 (the golden table is
the spec). **Status:** locked.

## 2026-06-06 — D2: Partial-stock policy

**Decision.** `ship_date()` takes a `partial_policy` enum, default
`SHIP_COMPLETE`: requested qty > quantity-on-hand routes the whole line
through the restock path (lead time governs the single ship date).
`SPLIT_SHIP` returns both dates (in-stock portion next-BD-17:00, remainder
on restock). The result's `basis` names the policy that fired.

**Why.** Quote lines need ONE definitive date by default (the stated goal);
ship-complete is the conservative customer promise. Split is real
operational behavior, so the engine models it rather than pretending it
away — but as an explicit opt-in, not a silent default.

**Alternatives rejected.** Split-by-default (two dates on every thin line
complicates the quote contract); rejecting partial-stock requests (a
solvable case answered with an error is engine laziness).

**Outcome.** Golden tests G13, G14. **Status:** locked.

## 2026-06-06 — D3: Calendar, timezone, holidays

**Decision.** Facility timezone `America/New_York` (constant, per-tenant
configurable later — NOT a forward-built abstraction now). Business days =
Mon–Fri minus a fixed holiday table (US federal holidays observed,
2026–2027, listed in `src/fulfillment/calendar.py`). `ship_date()` requires
a timezone-aware receipt timestamp and raises `ValueError` on naive input —
totality over the *valid* domain, with invalid input rejected loudly, never
guessed at.

**Why.** Ship-by-5-PM only means something in the facility's clock. Naive
timestamps are where silent off-by-timezone bugs live; rejecting them is
the deterministic-engine equivalent of never-invent-a-SKU.

**Alternatives rejected.** UTC-internal-only (hides the 17:00 boundary
semantics); accepting naive timestamps as facility-local (one DST bug away
from wrong promises).

**Outcome.** Golden tests G07-G09 (holidays), G10 (DST), G15 (naive rejection), G18 (horizon).
**Status:** locked.

## 2026-06-06 — D4: Synthetic inventory over the export's real QoH

**Decision.** Inventory is GENERATED (seeded, re-runnable:
`scripts/generate_inventory.py`, seed 20260606) rather than read from the
catalog export's `quantity_on_hand` column. In-stock probability and depth
are weighted by each SKU's REAL `sales_count` (high-velocity SKUs stock
deeper), targeting ~85% in stock. Out-of-stock SKUs get synthetic lead
times: 5–10 business days standard, 15–30 for human-review/custom patterns;
obsolete-flagged SKUs are forced out-of-stock at the long band. Every SKU
gets a record — no third state.

**Why.** The export's QoH is a stale point-in-time snapshot (spot-checked:
high-velocity rows showing 0 on hand), and the milestone's purpose is a
controlled, reproducible test distribution — while sales-weighting keeps it
*shaped like the real business* instead of uniform noise.

**Alternatives rejected.** Real QoH as-is (stale, uncontrolled, weakens
boundary coverage); uniform random (plausibility matters in a work sample
built on a real catalog).

**Outcome.** `data/inventory.json` + distribution stats logged by the
generator. **Status:** locked.

## 2026-06-06 — D5: Retrieval fallback proposes; it never resolves

**Decision.** The BM25 fallback emits ranked candidates at confidence
'low' with `needs_review=True` — it can never produce a RESOLVED result.
RESOLVED belongs to the deterministic translator exclusively.

**Why.** The production-validated architecture closes the loop with an LLM
chooser over a K=25 hybrid pool (88.2% conditional accuracy, locked
2026-05-02 readout). This repo runs no model calls in CI, so an
auto-resolving retrieval path would be an unvalidated chooser — the exact
thing the architecture exists to forbid. BM25 stays because it is the
component that carried exact-token recall in the validated hybrid
(recovered K4-12SBA where dense embeddings conflated close variants).

**Alternatives rejected.** Score-threshold auto-resolve (an invented,
untested decision rule); shipping MiniLM + chooser (model weights and API
calls in a work sample no reviewer will run).

**Outcome.** tests/test_resolution_service.py::test_retrieval_fallback_*;
adversarial suite confirms candidates are always real rows. **Status:**
locked.

## 2026-06-06 — D6: needs_review is state-grounded; the structural flag rule stays out

**Decision.** needs_review = NOT(high-confidence RESOLVED with no flags).
The production structural flag rule ("top-1 customer-novel AND in-history
candidate in top-5") is NOT implemented.

**Why.** That rule requires per-customer purchase history this dataset
lacks — and its measured F1 collapsed to 0.15-0.33 at production accuracy
(locked 2026-05-02; redesign is milestone M2, deliberately out of scope).
Implementing a rule whose deficiency is already quantified, against data
we'd have to fabricate, fails both honesty and scope discipline.

**Outcome.** Documented inline in service.py; the seam is stated, not
smoothed. **Status:** locked.

## 2026-06-06 — D7: Catalog content hash as row-version surrogate

**Decision.** Every Resolution carries sha256(catalog bytes)[:12]. The
export has no per-row versioning, so the catalog-level content hash is the
auditability anchor: a resolution is valid against exactly those bytes.

**Why.** "Which catalog answered this?" must be answerable for any quote
line ever produced. **Alternatives rejected:** per-row hashes (false
granularity over an export that changes atomically); no version (audit
dead end). **Status:** locked.

## 2026-06-07 — D8: Synthetic BC-shaped twin as the v1 validation target

**Decision.** The harness validates against an in-process, fault-injectable
twin implementing the DOCUMENTED BC behaviors (429 throttling, $metadata
tableextensions, $skiptoken pagination, per-entity grants, destination-side
audit log) — not the container replica, which remains the later integration
target.

**Why.** Demonstrate-the-catch requires *configurable* faults; a synthetic
twin plants them deterministically in CI in milliseconds. The twin seeds
items from the real catalog fixture so the golden path's acceptance (the
translator's identity guarantee on synced data) runs against real SKU
shapes. **Alternatives rejected:** container twin in CI (minutes-slow,
license-encumbered, faults hard to plant); mocked responses per test
(no behavioral coherence — pagination, throttling, and grants must interact).

**Outcome.** `src/erp_twin/`; every planted-fault E2E in the matrix.
**Status:** locked.

## 2026-06-07 — D9: The proposer is a pluggable Explorer protocol

**Decision.** Mapping proposals come through an `Explorer` protocol. CI runs
a deterministic HeuristicExplorer plus adversarial fixtures (wrong-mapping
and write-attempt explorers). An LLM-backed explorer is a drop-in.

**Why.** Every guarantee (read-only transport, budget, verification probes,
review gate, drift halt) lives OUTSIDE the proposer — so the intelligence
can be swapped without re-proving anything. This is the resolution service's
division of labor applied to onboarding: the model nominates; code binds.
The planted-fault tests require an adversarial proposer anyway; a protocol makes
one a fixture instead of a fork. **Alternatives rejected:** LLM calls in CI
(nondeterministic, costs, and the guarantees should not depend on model
behavior); hardcoded heuristics (no seam for the real agent later).

**Outcome.** `explorer.py`; AdversarialMappingExplorer / AdversarialWrite-
Explorer in the test matrix. **Status:** locked.

## 2026-06-07 — D10: defusedxml as a runtime dependency

**Decision.** `$metadata` parsing uses defusedxml in src, not just tests.

**Why.** Real tenant metadata is untrusted input; stdlib XML is
XXE/billion-laughs vulnerable by default. The safe parser costs nothing.
**Status:** locked.

## 2026-06-07 — R0 defect fixes (self-audit remediation)

Six defects surfaced by adversarial self-review, each fixed with a
failing-first regression test (demonstrate-the-catch):

- **#1 (correctness, was silent):** `add_business_days`/`next_business_day`
  now raise `CalendarHorizonError` when a walk crosses the holiday-table
  horizon. Previously an OOS item with a long lead ordered near year-end
  computed a ship date in an undefined calendar year, silently counting
  e.g. Jan 1 as a business day. Golden G19/G20 + property guard
  (`local.date() <= CALENDAR_HORIZON`).
- **#2:** load-bearing `assert`s in harness production paths (manifest
  least-privilege, contract totality, discovery status) replaced with
  typed raises (`InvariantViolation`, `DiscoveryError`). Asserts vanish
  under `python -O`; a subprocess test now proves the invariants fire under
  `-O`, plus a static no-assert check on harness source.
- **#3:** identifier-length verification ceiling is now tenant-RELATIVE
  (fraction of the longest sibling free-text field's p95, floored), not a
  hardcoded 30. A genuinely long-SKU tenant verifies; description-as-SKU
  still rejected (the relativity self-corrects: when the candidate is the
  long field, the short siblings yield a low ceiling).
- **#4:** discovery + verification use a stratified sample across the entity
  (`sample_rows`), not the first N PK-sorted rows. Stated frame limitation
  for entities larger than the scan cap, logged not hidden.
- **#5:** `Retry-After` parser handles both delta-seconds and HTTP-date
  forms; never raises, never negative.
- **#6:** `ALTERNATIVE_ENTITY` gap class wired (was a dead enum): a contract
  field unverified on its expected entity but present on another discovered
  entity is named for remap.

**Status:** all locked; suite 141 -> 149.

## 2026-06-07 — R2 harness architecture gaps

- **#7 atomic refresh:** `AtomicCatalogRef` holds the live index; refresh is
  build-new-then-swap (one rebind), so an in-flight resolution sees either
  the old snapshot in full or the new one in full, never a half-built index.
- **#7 incremental sync:** `sync_items_incremental(since=, prior=)` fetches
  only rows modified after `since` via a server-side `$filter` and merges
  them onto the prior snapshot. Stated limitation: merges adds/updates by
  SKU, does NOT detect deletions (needs a tombstone feed the standard
  surface lacks) — flagged for the sync phase, not assumed away.
- **#7 persisted BM25:** deliberately NOT built. Pickling a BM25Okapi index
  is brittle across library versions; the real in-flight-consistency risk is
  closed by atomic refresh. Rebuild cost is a resolution-layer perf concern
  for the production-validation gate, not a correctness gap.
- **#8 transport timeouts:** `TransportTimeout` is a Backend-contract
  exception (a production HTTPS backend raises it on socket deadline); the
  enforcer journals it and retries with backoff, halting cleanly via
  BudgetExhausted rather than hanging. The in-process twin never raises it.
- **#9 token-expiry vs never-granted:** a 401 raises `AuthExpiredError`
  (transient auth), with an optional `auth_refresh` callback retried once;
  a 403 remains `MissingGrantError` (permission gap). The two are now
  distinct paths, tested both ways.

**Status:** locked; suite 163 -> 172.

## 2026-06-07 — P2: LLM seams + provider layer (enforced-with-override)

The model proposes; deterministic code still binds. Added `model_provider/`:
provider-agnostic `ModelProvider` (Anthropic + OpenAI + OpenRouter adapters,
each provider-pure with lazy SDK import; ScriptedProvider for CI), BYOK key
resolution from env (never logged), and an opinionated `TIER_FOR_TASK`
routing policy grounded in the locked record (intent=cheap/Haiku,
retrieval_select=medium/Sonnet citing the 88.2% arc, onboarding_map=medium).
Enforced-with-override: the policy picks per task; an explicit model id wins
and is recorded. Every call logs to the cost ledger (model/provider/tokens/
cost) — this is what makes observability model-adaptable.

Three seams now have real LLM implementations behind deterministic fallbacks,
all exercised with ScriptedProvider in CI (no network):
- retrieval Chooser (the 88.2% selection step): a chosen SKU is bind-guarded
  to the retrieved candidate set, so never-invent holds through a
  hallucinating model. Supersedes D5 when a chooser is configured.
- onboarding Explorer: LLM proposes field mappings; proposals naming a
  non-existent field are dropped, and C7 probes still verify every survivor.
- gateway IntentRouter: replaces the brittle inline regex routing with a
  seam; RuleBased (CI default) reproduces the legacy routing exactly, LLM
  router drops in for production. Either way the gates still bind.

A provider being unavailable raises ModelUnavailable and the seam falls back
to its deterministic path — the system degrades, never breaks. Suite 212->232.

## 2026-06-07 — Unknown-catalog grammar induction + multi-format ingestion (C4 expanded)

C4 originally ran the *known* the example catalog grammar (`part_number_parser`) over a
discovered items entity — useless on a tenant whose grammar we have not yet
written. Added `erp_harness/grammar_induction.py`: a tenant-agnostic decoder
that *learns* an unknown catalog's SKU grammar from the strings themselves,
generalizing the techniques proven on the example catalog decoder rather than its
hardcoded patterns (segmentation into typed runs, family-by-leading-prefix,
per-family regex induction, positional role semantics, separator/case
normalization).

It runs as an iterative loop: round 0 is structural induction (the quick win —
on the real the example catalog it structurally decodes ~55% with zero human
effort); later rounds propagate high-confidence clues to families that share a
shape ("segment 2 was a diameter in family K, so test that in family M with the
same shape"); the loop stops at diminishing returns and hands the residual to
manual decodification, ranked by SKUs unlocked. Roles (diameter / length /
finish / sequence) are inferred by correlating segment values with the
description text.

Architecture spine held: induction PROPOSES. Every family and every segment
role is an `Assumption(status='proposed')` carrying evidence + confidence for a
human SME to confirm or correct — nothing binds without review. Segments that
do not correlate become ranked SME questions, each naming what answering it
unlocks. An optional `LLMRoleProposer` labels still-unknown segments (routed at
the new `catalog_decode_role` task tier); it too only proposes — confidence is
floored and marked `proposed_by='llm'`, and the human gate is unchanged. CI
runs `NoRoleProposer` (zero model calls).

**Multi-format ingestion** (`erp_harness/catalog_source.py`): one decoder, many
sources. Pure row-extraction functions (`rows_from_catalog_lines` for PDF/text,
`rows_from_worksheet` for Excel, `rows_from_html_tables` for web) turn each
format into `{sku, description}` rows; thin `*CatalogSource` adapters do the
I/O. PDF reading uses pypdf (`[pdf]` extra); Excel uses openpyxl (core dep);
web/HTML uses stdlib `html.parser`. Adding a source is a new pure function plus
a tiny adapter — the decoder never changes.

**Validated on a real, unseen vendor catalog** (World American heavy-duty
engine parts, ~1,200 SKUs, PDF): the pathway extracted the rows, discovered the
dominant `WA` family (shape `AN-N-N`, 760 SKUs) and structurally decoded ~66%,
and — correctly — asked the SME what the line-code and sequence segments encode,
because those are not inferable from the description text. A file-gated live
test exercises this (skipped in CI when the file/pypdf are absent).

**Status:** suite 232 -> +24 (grammar induction + catalog source). Brand scrub:
all manufacturer references removed from code/docs; tenant-ID format throughout.

## 2026-06-07 — P3 (voice): Streaming STT — Twilio Media Streams + AssemblyAI v3

Durable-vs-hack call (raised explicitly): the AssemblyAI **Voice Agent API**
runs a hosted LLM that speaks the reply — that relocates the binding decision to
an uncontrolled model and breaks never-invent + the pricing gate at the voice
layer. So we use AssemblyAI **Universal-Streaming STT** (transcribe-only) and
keep the deterministic gateway as the brain. AssemblyAI's own docs point this
way ("bring your own LLM/TTS → use Streaming STT"). The Voice Agent API is the
easy hack for a different product; Streaming STT is the durable path for this one.

Built behind the existing seam (no gateway change):
- `gateway/voice_stream.py` (pure, CI-tested): Twilio Media Streams frame
  envelope parsing + G.711 mu-law → PCM16 decode. mu-law is hand-rolled and
  reference-tested because stdlib `audioop` was removed in Python 3.14 (PEP 594)
  — no third-party audio dep.
- `gateway/asr_streaming.py`: `StreamingASR` seam. `SimulatedStreamingASR` (CI)
  makes the frames→turn bridge testable with no audio/network;
  `AssemblyAIStreamingASR` is the live v3 client (wss://streaming.assemblyai.com
  /v3/ws, Authorization header = key, binary audio, 'Turn'/end_of_turn parsing,
  Terminate). `[voice]` extra (websocket-client); key from ASSEMBLYAI_API_KEY.
- `runtime/app.py` `/voice-stream` WebSocket + `twiml.connect_stream`
  (`<Connect><Stream>`): ingest → transcribe → gateway turn per finalized
  utterance, all gates intact. Boots with a no-op simulated ASR; switches to
  AssemblyAI when the key is present. Replies emit as JSON 'assistant' events.

Honestly scoped: speaking replies back as audio needs a TTS leg (TTS → mu-law →
Twilio media messages) — named as the remaining integration step. The callable
talking bot today is the `<Gather>` path (P4); the streaming path is the
transcription-fidelity upgrade and the foundation for full duplex.

Tested: mu-law reference points, frame parsing, stream→ASR→gateway bridge,
and the `/voice-stream` WebSocket (TestClient + scripted ASR) all in CI; live
AssemblyAI socket in test_live_voice_smoke.py (credential-gated, not CI).

Remaining P3 (not voice): ERP HttpBackend/OAuth transport and DB-backed
customer/price adapters — same seam pattern, deferred.

## 2026-06-07 — P3 (rest): production data + wire adapters

Remaining P3 adapters, behind the existing protocols (config selects by env;
"go to production" = set a var, not edit code):

- **SQLite CustomerDB + PriceBook** (`gateway/db_adapters.py`): persistent
  implementations of the CustomerDB / PriceBook protocols. SQLite is stdlib, so
  unlike the credentialed adapters these are FULLY CI-tested — parity with the
  in-memory/synthetic defaults, LIKE-wildcard escaping (no injection), and the
  gateway verifying+pricing end-to-end against them. Config: SKU_CUSTOMER_DB
  ending .db/.sqlite -> SqliteCustomerDB; SKU_PRICEBOOK_DB -> SqlitePriceBook.

- **ERP HttpBackend + OAuth** (`erp_transport/` — a NEW package, deliberately
  OUTSIDE erp_harness): the harness stays import-pure ("the Backend protocol is
  the only wire boundary" — test_harness_purity forbids any net import in
  erp_harness), so the real HTTPS client lives outside and is injected into the
  SafetyEnforcer. urlopen is injectable, so request building, the bearer header,
  JSON parsing, non-2xx pass-through (429/403 reach the enforcer as responses
  for backoff/grant-gap, not exceptions), timeout->TransportTimeout, read-only
  method enforcement, and OAuth client-credentials token caching/refresh
  (deterministic ManualClock) are all unit-tested with no network. Live-tenant
  runs remain gated behind the twin demonstrate-the-catch matrix (harness spec
  §6), so there is no live ERP smoke — the mocked transport is the coverage.

Hardening pass (same session): extracted the credentialed seams' parsing into
pure functions with non-live tests (AssemblyAI Turn, Anthropic/OpenAI responses)
and fixed a silent cost_usd=0 on the OpenAI-compat path. Edge/property tests
added for the catalog decoder, voice stream, and catalog ingestion.

## 2026-06-07 — Shadow / listen-in onboarding + service-improvement capture

A second onboarding mode beside catalog decode: the agent rides along on real
rep<->customer calls in **observe-only** mode (it never speaks or acts), runs the
resolution pipeline on each customer utterance, and records what it WOULD have
done and whether it succeeded. Across many calls over a configurable window
(`ShadowCampaign`, `window_days=None` = continuous) it builds one aggregate
**capability/failure map** — failure points ranked by how often they recur.

That map drives a HITL session: the SME fixes each failure either with a
grammar/semantic alias (a phrase → a REAL catalog SKU) or a chosen
graceful-degradation behavior. Corrections go in a `CorrectionStore` the observer
consults FIRST, so a fixed failure resolves on the next pass — the improvement
loop, closed and demonstrated in a test. Never-invent holds: an alias may only
target a SKU that exists in the catalog (a bogus target is rejected).

`gateway/shadow.py` is pure (no I/O; uses the deterministic ResolutionService),
so the whole loop runs in CI. The observe→map→correct artifacts are captured by
`observability/service_improvement.py` (`ImprovementLog`) — anonymized
(one-way-hashed tenant/account keys) and PII-scrubbed (phone/account digits
removed) — retained to improve resolution quality and the service over time.
Off by default.

### Self-healing addendum (same day)

The shadow observer also learns from how the human REP handled an inquiry the
tool missed. On a failed customer utterance, `observe_call_with_healing` scans
the rep's following turns and harvests a `SelfHeal`:
  - `rep_said_sku` — the rep stated a REAL catalog SKU (strongest signal), or
  - `rep_restatement` — the rep restated the part and the tool could resolve it.
Never-invent holds: the healed SKU is always a real catalog row (a bogus code
the rep says is ignored). With `autonomous=True` the strongest signal
(`rep_said_sku`) is auto-applied to the CorrectionStore so the same failure
resolves next pass; weaker signals stay PROPOSED for the HITL gate. Self-heals
accumulate across a `ShadowCampaign` and are captured (anonymized, scrubbed) via
`ImprovementLog.record_self_heal`. Tested end to end: failure → rep says SKU →
auto-applied → re-resolves.

## 2026-06-07 — Always-on continuous self-improvement (3 sources, periodic HITL) + wiring audit

Reframed shadow onboarding into a permanent `ContinuousImprovement` loop fed by
THREE always-on sources, all read-only:
  1. training ride-along on rep<->customer calls (auto-applies strong heals);
  2. post-handoff learning (after the agent degrades+transfers, learn what the
     human did with the inquiry it couldn't);
  3. self-monitoring of calls the agent runs ITSELF (flags its own uncertain
     moments as opportunities — never auto-applied).
Strong, never-invent-safe signals (a human stated a real catalog SKU) auto-apply
between reviews; everything else accumulates and surfaces as a `ReviewBatch` on a
configurable cadence (`review_every` / `review_every_calls`) for periodic HITL.
Wired into `/voice-stream` (self-monitoring on the agent's own call audio;
config `SKU_IMPROVEMENT`, off by default) and tested over the ASGI app with a
simulated ASR.

Wiring audit (holistic review): found two exported-but-never-called pieces —
`AlertRouter` and `verification_preflight`. Both now wired to their intended
triggers: AlertRouter fires a 'review due' alert from ContinuousImprovement when
the cadence is reached (injected; off unless provided); verification_preflight
guards the live smokes via an autouse conftest fixture (don't verify live
behavior against uncommitted/stale code). Capstone `test_e2e_holistic.py` walks
onboarding -> resolution -> conversation -> continuous-improvement in one flow.

## 2026-06-07 — Live-audio shadow bridge + review-queue persistence + accent fail-loud

Made the two remaining always-on sources real on call audio: `ShadowStreamBridge`
turns a Twilio dual-channel call (media.track inbound/outbound -> customer/rep)
into a speaker-tagged transcript via a per-track streaming ASR and feeds
`ContinuousImprovement.ingest_call` — so the rep's resolution of anything the
tool missed is harvested from AUDIO. Wired to an observe-only `/shadow-stream`
WebSocket. Tested with a simulated per-track ASR (no audio/creds): ride-along
self-heal closes from audio.

Review queue now persists (`ContinuousImprovement(state_path=...)` snapshot/
restore of pending proposals + opportunities) — an in-flight review survives
restart (corrections already persisted separately). A test caught a real
ordering bug (restore ran before the list fields were initialized and got
clobbered) — fixed.

Persona accents: real voice ids resolve from SKU_VOICE_ID or per-accent
SKU_VOICE_ID_<ACCENT>; ElevenLabsTTS now FAILS LOUD on a placeholder voice id
rather than speaking in a wrong/placeholder voice.

## 2026-06-07 — Voice readback states DECODED attributes; do not interrogate

**Decision:** the voice confirmation reads the part's attributes back from the
SKU grammar's own decode (K5-24SBC -> "the 5 by 24 inch curved-top stack,
straight bottom, chrome. Is that the one?") instead of asking the caller "what
diameter or finish were you after?". A new pure `gateway/spoken.py` renders
parser meanings to natural speech and normalizes residual raw notation
(`5"X24` -> "5 by 24 inch") at every audio boundary.

**Why:** a live call surfaced the agent interrogating the caller for a diameter
and finish that the part number already encodes, and TTS speaking the raw
catalog string `5"X24` as "5 inch ex 24". The decoder already produced
diameter/length/body/finish; the readback step was discarding it. The fix is to
USE the decode, not ask for it.

**Alternatives rejected:** (a) drop the voice readback entirely for confident
exact-SKU hits — rejected: STT garbling onto a near-neighbour SKU is real, so a
confirmation must stay. Stating the decoded attributes keeps the defense (a
mis-heard K5-26 reads back as "6 inch" and gets corrected) while sounding human.
(b) Fix only the TTS rendering — rejected: leaves the redundant interrogation.

## 2026-06-07 — Hosted ElevenLabs Agent as speech shell; gateway stays the tool

**Decision:** run the conversational surface as an ElevenLabs Agent that calls
ONE server tool, `resolve_part` = our gateway (`POST /agent/turn`). Speech,
turn-taking, small talk, greeting, and natural voice move to ElevenLabs; every
binding part fact stays in code behind the tool. Artifact:
`voice_agent/SYSTEM_PROMPT.md` (six-block prompt) + `runtime/voice_agent.py`
(pure payload builder + `validate_system_prompt` guardrail check + key-gated
create/update) + `scripts/elevenlabs_agent.py` (--dry-run/--validate/--apply).
See docs/VOICE_AGENT.md.

**Why:** a live call showed the self-hosted media pipeline produced robotic,
slow speech and jumped the caller — exactly the conversational concerns a hosted
voice-agent platform already solves. The never-invent / pricing-behind-
verification guarantees are NOT conversational; they live in `/agent/turn` and
do not move. The agent's only freedom is to be a friendly voice around the tool.

**Alternatives rejected:** (a) keep hand-rolling the self-hosted Twilio Media
Streams pipeline — kept in-tree as fallback, but not the primary surface; we
lose STT keyterm tuning but the deterministic grammar + decoded readback absorb
garble. (b) AssemblyAI Voice Agent — STT-only origin, still needs a TTS pairing;
ElevenLabs gives voice + agent runtime in one. **Guardrails grounded in
authoritative ElevenLabs docs** (prompting-guide six blocks; Guardrails
Focus/Manipulation/Content/Custom across three layers), cited in
docs/VOICE_AGENT.md. Data-capture parity kept via `observe_agent_turn`.

**Status:** build + validation + guardrail-catch tested in CI
(`tests/test_voice_agent_config.py`, 9 tests). Live `--apply` is key-gated and
unproven until run with a real ELEVENLABS_API_KEY + public tool URL.

Still genuinely unproven (needs credentials the sandbox blocked a broad scan
for): no real LLM/AssemblyAI/Twilio/ERP call has executed. The gated live smokes
are ready to run once credentials are provided in-session.

## 2026-06-08 — Internal resolution state is structurally barred from the spoken say

**Decision:** treat "the say never speaks the resolver's internal working state"
as a structural invariant enforced in code, the deterministic twin of the
provenance-completeness invariant. New `src/gateway/say_guard.py`:
`internal_state_tokens(say)` / `assert_no_internal_state(say)` (the CI gate, the
analogue of `assert_complete`) + `safe_voice_say(text)` (the runtime say boundary:
voice-render then enforce FAIL-SAFE — log + hand off, never speak the leak, never
crash). All four `voice_render` say-boundary sites (app.py ×3, endpoint_harness)
route through `safe_voice_say`. First two guard tests are the two leaks the live
adversarial run surfaced: a BM25 score and a taxonomy-code option list.

Fixes at the source: (a) `Candidate` gains a caller-safe `description` field;
`escalation.informed_question` reads it for the candidate readback and NEVER mines
the internal `reason` (which carries `bm25 score …: …`) — the original sin was
`reason.split(':')[0]`, which grabbed the score prefix. (b) the missing-field
question names the attribute ("what body style?") and offers only curated
human-word glosses, never the raw internal codes (`SB/EX/XB`, `K/BH/BR`,
`A/C/P/S3/S4/BS`) carried in `OpenQuestion.options`.

**Why this is more than a CX reword:** both leaks reached the caller through the
gateway's OWN `say` (substitute_say route), not the model — the model never
fabricated them; the say layer rendered its working state into spoken text. The
taxonomy-code leak is **containment-adjacent**, not cosmetic: speaking
`(SB, EX, XB)` teaches the caller the internal vocabulary, and a caller can speak
those codes back as tier2 tokens that steer resolution. Downstream check
performed: a code spoken back still flows through the full resolver and its
ambiguity gate (a code fills a field, exactly like a description would; if still
ambiguous it still asks; binding facts still come live) — so there is **no bypass
of the disambiguation-on-ambiguity behavior or the fact path**, only a faster
field-spec for a knowledgeable caller. The reason to stop speaking codes is the
principle (don't teach internal vocabulary) + CX, not a hole. Filed accordingly.

A structural guard, not a manual reword, because the next internal field threaded
into a `reason` or an option list would leak the same way — now it is caught in
CI over real say outputs instead of in another adversarial run.

**Collision-safety:** the guard does not false-positive on the legitimate spelled
SKU readback. `voice_render` spells `K5-24SBC` as `K 5, 24 S B C` (space-separated
single letters, never a parenthesized comma-list of multi-letter codes), and the
`(B M 25 …)` / `(SB, EX, XB)` leak shapes are distinct from it. Positive controls
(spelled SKU, prices, lowercase human-description parentheticals) are tested to
PASS; synthetic leaks tested to RAISE.

**Re-proof discipline:** changing the say is a change to the fact path's verbatim
output, so the live-model adversarial suite was re-run after the fix — 12/12 still
CONTAINED, and both leaks confirmed gone from the real says
(`docs/ADVERSARIAL_LIVE_RESULTS.json`). Nothing touches the fact path without
re-clearing the gate.

**Deliberate follow-ups (not done here, by design):**
- Candidate readback currently degrades to spelled-SKU-only because
  `identification.py` re-wraps candidates positionally and drops `description`.
  Wiring it through is NOT a one-liner: the raw catalog descriptions are
  notation-dirty (`CLAMP, 3-1/2" SS PRE-FORMED(FIVE STAR MFG)`) and piping them to
  TTS would leak raw dimensional/vendor notation to the ear — a different
  machinery class. A richer readback must route through the decoded
  `spoken_description`, tracked as its own task. Spelled-SKU-only is the safe
  interim.

**Status:** 13 tests in `tests/test_say_guard.py`; full suite 451 passed, 25
skipped; live adversarial re-run green.

## 2026-06-08 — Custom-LLM seam mounted: POST /v1/chat/completions

**Decision:** mount the containment brain as the OpenAI-compatible custom-LLM
endpoint ElevenLabs points its Agent's LLM at (`runtime/custom_llm_route.py`,
`register_custom_llm`). Transport + auth + instrumentation over the already-proven
`handle_async`; the gateway-as-tool seam (`/agent/turn`) is unchanged and holds
the hard guarantees independently. Live model = `google/gemini-2.5-flash` via
OpenRouter through a new ASYNC model_fn (`make_async_model_fn`). Runbook:
docs/CUTOVER.md.

**Real deadline = real abort (the integrity fix this turn turned up):**
`handle_async` previously wrapped the sync model_fn in `asyncio.to_thread`, which
`wait_for` CANNOT cancel — at B the endpoint returned fallback while the HTTP
request kept running (and billing) in the background. Fixed: handle_async now
awaits a coroutine model_fn DIRECTLY (cancellable), so B's cancellation propagates
into the in-flight httpx request and aborts the connection at the source; SDK
`request_timeout=B` is the backstop. Proven by unit test (the coroutine receives
CancelledError and never reaches completion) AND live (tiny budget → returned at
~B, not after a full generation). The sync/thread path stays for CI only.

**B is the model-route budget by construction:** handle_async checks substitution
FIRST and never subjects it to the deadline, so the gateway/substitution route is
not held hostage to the model's jitter. The two latency populations the B-probing
must characterize separately — substitution (narrow tail, model not invoked) and
free/model (fat, non-stationary tail: 1.0s then 6.28s on the same input) — are
already split by the per-route `route` tag. B is set at a HIGH PERCENTILE of the
model route (not a median, or answerable turns false-block to fallback), kept
below ElevenLabs' tolerance and above the model's high percentile. Provisional
B=8s for first probes (env `CUSTOM_LLM_BUDGET_SECS`), tightened post-probe.

**Instrument-then-expose:** per-route latency + decision trace emit on EVERY call
from the first — telemetry span + append-only JSONL ledger (`CUSTOM_LLM_TRACE_LOG`,
scalar-only, safe to keep) the B-probing reads to compute per-route distributions
and over-B rates. The first live calls are the highest-information samples for a
CI-to-live discrepancy and are observed once.

**Rollback (stated before exposure):** one ElevenLabs Agent config change — flip
the LLM from `custom-llm` back to hosted `gemini-2.5-flash`. Immediate on next
call, no redeploy, our service can stay up. `/agent/turn` unchanged so never-invent
+ pricing gate + provenance still enforce at the tool boundary; what's lost is the
custom-LLM filter (residual = pre-containment prose-fabrication posture, bounded
and known). Finer in-endpoint lever: drop `CUSTOM_LLM_BUDGET_SECS` or the key to
neutralize the model route while substitution keeps serving real facts. Trigger:
free-route `fallback_used`/`over_budget` spike, or ANY fabricated id in a
transcript (must be zero; adversarial suite is the standing guard).

**Streaming:** buffer-don't-stream in SSE clothes — we filter the COMPLETE reply,
then emit it as role + one content/tool_calls delta + terminal finish + `[DONE]`.
No partial fact content is ever streamed.

**Status:** `tests/test_custom_llm_route.py` (9) + the async-abort test; full suite
461 passed, 25 skipped (was 451). App boots with no key; substitution works keyless; free turns
fail-closed keyless. Live smoke green (real reply, SSE, deadline honored). The one
remaining external unknown is B against the real audio layer — closed by the
docs/CUTOVER.md B-probing, which needs the live Agent pointed at the endpoint.

## 2026-06-08 — Two fallbacks (fact vs service) + first-probe stopping rule

**Decision:** split the single fallback into two with different COHERENCE domains
(the distinction is tonal safety, not containment — both are safe non-fact lines):
- `FALLBACK` ("Let me get a rep to confirm that exact part number — one moment.")
  — used ONLY when the model produced output we then blocked for containment
  (fabricated id/value, ungrounded tool key, filter error). `filter_free` only
  BLOCKs on an invented id or fabricated value, so the caller was demonstrably
  talking parts; the part-number line is coherent.
- `SERVICE_FALLBACK` ("Sorry, I didn't catch that — could you say it again?") —
  used when the model produced NOTHING usable (model_error, over_budget,
  mapping_error, key unavailable). Topic unknown (may be small talk), so the
  part-number line would be a non-sequitur — a caller saying "thanks, you've been
  helpful" must not hear "let me get a rep to confirm that part number." Routed in
  `decide_turn`/`handle`/`handle_async` failure paths; the containment-block path
  keeps `FALLBACK`. Regression test:
  `test_small_talk_failure_is_tonally_coherent_not_a_part_number_nonsequitur`.

**Why now:** the keyless-boot and any key/budget-outage path routes free turns to
the failure fallback; with the key present in normal operation it never fires, so
the tonal bug would have been invisible until a real caller hit an outage and the
agent sounded broken. Closed before cutover.

**First-probe protocol (docs/CUTOVER.md):** the cutover is now a MEASUREMENT
experiment, not a build, so it gets pre-registered abort conditions decided BEFORE
the config flip (deciding a threshold while calls degrade is deciding it under the
pressure that loosens it): (1) any fabricated id → rollback (zero tolerance);
(2) free-route fallback rate >~20% over the first N≈20–30 calls → pause (live
free-turn distribution off from tested); (3) model-route over-B rate >2× the
offline-characterized rate → pause (production latency worse than offline; do NOT
widen B past ElevenLabs' tolerance to mask it). The runbook also now carries the
REASONING next to "faster model, not looser B": B is bounded above by ElevenLabs'
tolerance because widening past it trades our controlled fallback for their
uncontrolled degradation (dead air) — strictly worse. The alert is correctly
saying the model is too slow; don't shoot the messenger.

**Status:** full suite 462 passed, 25 skipped. Live: small-talk failure → soft
line confirmed; facts still substitute keyless. Cutover gated on these two
pre-flight items (free-turn tone — DONE; abort conditions stated — DONE).

## 2026-06-08 — Dependency fault-injection harness + four fail-closed hardenings

**Decision:** the durability bar for someone else's callers — every dependency,
faulted at every seam of the turn, must fail CLOSED and COHERENT (never a hang,
crash/500, incoherent/empty utterance, or fabricated fact). Mapped first
(docs/FAULT_INJECTION_PLAN.md) against the two endpoints/flows so the harness
covers every seam, not the reachable ones, then built: tests/test_fault_injection.py
(18). Same adversarial discipline as containment, applied to exogenous faults with
deterministic correct behavior; demonstrate-the-catch where a real fix is involved
(the unwrapped path is shown to fail BEFORE asserting the wrapped path holds).

**Two endpoints, two flows.** custom-LLM seam (handle_async): S1 transport-in, S2
tool-message (the gateway RESULT as ElevenLabs relays it), S3 substitution, S4
model, S5 filter, S6 transport-out. Gateway (/agent/turn): G1 intent, G2
resolution, G3 inventory, G4 pricing, G5 customer-DB, G6 journal. Defense-in-depth:
G* must keep /agent/turn from 500ing (we do NOT control what ElevenLabs does with a
tool 500 — assume dead air), and S2/S3 fail closed if the relayed result is
degraded anyway.

**Four hardenings the harness justified (code, not just tests):**
1. **S3** `substitution()` — an empty/whitespace say now fails to `SERVICE_FALLBACK`
   (route=`substitute_empty`) instead of speaking silence on a fact turn.
2. **S4** `apply_model_output` — a malformed tool_call (args not JSON, or no `text`)
   fails to `SERVICE_FALLBACK` (route=`malformed_tool_call`) instead of forwarding
   an un-executable call to ElevenLabs.
3. **G1–G5** `gateway.turn()` top-level fail-closed: dispatch moved to `_dispatch`,
   wrapped so ANY internal dependency exception becomes a coherent escalation
   (kind=escalate, refused=`internal_error`, guard-clean say), journaled — never a
   500. Auth/session resolution (`state_of`) stays OUTSIDE the wrapper (a bad token
   is not an internal fault to swallow; `state_of` returns UNVERIFIED, doesn't
   raise — the /agent/turn `X-Agent-Token` check is the real auth boundary).
4. **G6** `journal.record()` best-effort: a write failure is logged, never raised —
   losing an audit row beats dropping a caller's turn.

**The slow tail is a first-class fault, not a happy-path afterthought:** an async
model slower than B at a seconds-scale budget is injected and asserted to be really
aborted (coroutine receives CancelledError, never completes, endpoint returns at ~B
not after the full generation) — the case the deadline exists for and the case the
under-sampled latency probe keeps brushing against.

**Containment re-proof:** the hardenings only add branches for degraded/malformed/
exception inputs; valid fact-path output is byte-identical, and the deterministic
adversarial endpoint tests (test_endpoint_harness) stayed green, so containment is
preserved without re-burning the live adversarial run.

**Status:** full suite 480 passed, 25 skipped (was 462). This is the pre-customer
durability piece; the pilot/learning harness (turn the pilot's first week into eval
data) is the next pre-customer piece. Cutover + ElevenLabs-tolerance B remain
parked for the live phase (need a customer / a live deployment).

## 2026-06-08 — Disclosure gate floor (state model + pure-function gate) + two filed gaps

**Decision:** begin CONVERSATION_STATE_SPEC at its stated build order — step 1
(state objects) + step 2 (the gate as a pure function), with invariants 2–4 proven
in ISOLATION before any orchestration is wired. "Build the gate first so the
orchestration has something it cannot talk around." Artifacts:
`src/gateway/conversation_state.py` (§2: ConversationState / PartContext / Fact[T]
/ IdentityState / AccountState — the durable-identity vs perishable-fact split as
types), `src/gateway/disclosure_gate.py` (§3: `precondition_met` + `fresh` +
`discloseable`, pure, with injectable `Horizons`), `tests/test_disclosure_gate.py`
(11 invariant tests).

**Why pure-function-first:** the gate is the deterministic floor; the orchestration
is the agentic, forgiving layer above it. A wrong LLM move is cheap (a redundant
question) only BECAUSE the gate below cannot be talked around. So the gate is built
and proven before the agency that depends on it — same prove-the-floor-then-build
discipline as containment.

**The load-bearing invariant proven now: invariant 3 (inherited disclosability
forbidden).** The account is SHARED and durable (established once, indexes every
part's price), but identity and the fresh read are PER-PART. The attack the spec
flags as most-likely-wrong-on-first-wire — an unidentified part C inheriting price
disclosability from an identified sibling B under the same established account —
fails closed at the gate on C's OWN identity precondition. Proven at the gate level
now (`test_inv3_inherited_disclosability_is_forbidden`); the full §10 suite against
wired orchestration is step 6 (later).

**§8 pricing-unreadable falls out of the gate, no special case:** an `unreadable`
price (no source wired) is never `READ`, so never `fresh`, so never discloseable →
the orchestration's can't-quote handoff. Flips on with zero code change when the
pricing source appears. Tested.

**Two durability gaps FILED (V5, V6 in PRODUCTION_VALIDATION_GATE.md):**
- **V5 stale-but-well-formed data** — the dependency that LIES (returns wrong/stale
  data) vs errors. An exception harness cannot catch it. The planned guard is the
  gate's freshness (§3.2 `fresh()` + per-fact-type HORIZON, invariant 4), now built;
  the unknowable is the real HORIZON values (the customer's data velocity) and how
  their catalog signals supersession — tuned/wired with the pilot.
- **V6 correlated load** — single-fault injection misses correlated faults
  (model-slow + gateway-slow together). CONFIRMED in code
  (`test_correlated_load_substitution_route_stays_bounded_without_the_model`): the
  substitution route's B-exemption is safe for a STRONGER reason than "gateway
  fast" — it does ZERO I/O (gateway latency already spent on the prior /agent/turn
  hop). Remaining unknowable: the gateway's own internal dependency calls aren't
  bounded by an internal timeout — a HANGING (not erroring) dependency rides to
  ElevenLabs' 12s tool ceiling; whether an internal per-dependency budget is needed
  is measured under real correlated load.

**Pilot-harness direction (recorded, not yet built):** shadow-first (zero caller
risk, full-volume corpus to retire the authored 14-case eval), graduate to propose
once the shadow eval clears — the open input is the prospect's daily parts-call
volume (low volume could flip to propose-first). Labeling boundary = three label
types kept from contaminating each other (resolution correctness → CorrectionStore
candidate; behavioral → eval candidate pool, NOT straight to frozen/holdout;
conversational quality → quarantined naturalness store, never feeds the loop), unit
of labeling = the decision point (from the per-decision trace), labels are
confidence-weighted by provenance (acquiescence thumbs-up < explicit correction <
order-placed-not-returned gold), and the not-exercised trichotomy gates which label
questions are even asked. This is the next design pass before build.

**Status:** full suite 492 passed, 25 skipped (was 480). Gate floor done; spec steps
3 (say extensions: per-part-explicit + quantity in the internal-state guard), 4
(orchestration moves), 6 (§10 adversarial suite) remain.

## 2026-06-08 — Conversation layer built (spec steps 3→4→6); §10 adversarial green

**Decision:** complete CONVERSATION_STATE_SPEC build steps 3 (say extensions), 4
(orchestration), 6 (§10 adversarial suite) on top of the proven gate floor. Spec
now canonical in-repo at `docs/CONVERSATION_STATE_SPEC.md`.

**Step 3 — say extensions + the quantity guard (demonstrate-the-catch).** Added
on-hand-QUANTITY detection to `say_guard` (`_QTY`): a number ADJACENT to on-hand
language ("58 on hand", "we have 58", "qty: 58") — never bare "in stock", never
ship-times / lead-time numbers / dimensions / prices (their trailing words aren't
on-hand words), never the spelled SKU. The guard immediately CAUGHT the existing
`_plain_availability` say ("...in stock — {qty} on hand"), a real legacy leak of
invariant 5 — so availability was made BOOLEAN ("in stock. It ships by 5 PM the
next business day"); the structured AvailabilityAnswer still carries qty internally,
the say does not. Per-part-explicit multi-part rendering in `disclosure_say.py`
(§7): each part its own sentence, structurally impossible to aggregate
("those are mostly available" is unreachable). One provenance test updated: the
availability turn now asserts NO quantity leak in the say while surfaced_values
still carries qty — invariant 5 tied to provenance.

**Step 4 — orchestration (`conversation.py`).** `Conversation` owns durable state,
focus/anaphora (`resolve_reference` = 0/1/many → none/resolved/DISAMBIGUATE, never
guess), the gate-enforced `read_and_disclose` (reads in-scope perishable facts
FRESH and TOGETHER at one `now` so the bundle shares an `as_of`; a stale cached
fact is re-read, never re-spoken), and the closure loop (a disclosure NEVER sets
`caller_intent_complete`; only `note_completion_signal` does). The orchestration
PROPOSES disclosure; the gate AUTHORIZES it — every fact passes `discloseable()`.

**Step 6 — §10 adversarial suite GREEN (`test_section10_adversarial.py`, 8).** Each
conversational freedom's deterministic twin, all failing closed: price-without-
account (precondition), inherited-disclosability (part C ambiguous cannot ride
identified sibling B under the shared account — blocked on C's OWN identity),
stale-read (gate rejects; cached-stale re-read not re-spoken), incoherent-bundle
(read-together shared as_of), quantity-leak (say-guard), aggregated-multi-part
(renderer per-part-explicit), premature-close (disclosure ≠ close), wrong-referent
(disambiguate, never a guess — no focus written on the ambiguous case). Combined
with containment (the say comes only from the gateway, model authors nothing), the
only path to disclosure is the gated `read_and_disclose` — so no LLM move sequence
reaches past the gate.

**Honest scope:** the conversation layer + gate are built and PROVEN IN ISOLATION,
not yet WIRED into the live `/agent/turn` (which still runs the legacy fixed-
sequence backend). The availability-boolean change IS live (it's `answers.py`).
Wiring the orchestration in to REPLACE the fixed backend is a separate integration
step; the §10 proof is the gate to it. The pilot/learning harness (already designed:
three label types, decision-point unit, confidence-weighted provenance, shadow-
first) rides on the wired orchestration's decision points and comes after.

**Status:** full suite 516 passed, 25 skipped (was 492). Gate floor + conversation
layer + §10 all green.

## 2026-06-08 — Orchestration wired into /agent/turn (backend swap); both gate proofs green

**Decision:** replace the legacy fixed-sequence backend with the orchestration
(`Gateway.converse`) on the `/agent/turn` path — the custom-LLM tool path. Legacy
`turn()` stays ONLY on the other channels (/voice, /v1/turns); it is NOT a fallback
on the agent path. Swap points: `app.py` /agent/turn, `endpoint_harness` (scripted
front), `scripts/adversarial_live.py` — all now drive `converse`.

**De-risked by construction:** `converse` REUSES the answer builders
(`availability()`, `pricing()`) and the verify/authorization/lockout machinery, so
the containment keystone (`surfaced()` provenance, read structurally from
`resp.availability`/`price`/`candidates`/`meta`) and the security gates are
preserved unchanged. The orchestration layers the spec's durable Conversation state
(parts/account/focus, server-side per caller_id), the closure loop, and the
per-turn DECISION POINT (`resp.meta['decision']`: move / focus / account_established
/ disclosed / refused / complete) — the unit the pilot harness will instrument.

**Review scope, all four covered:**
1. *Full swap, no hybrid:* `/agent/turn` runs converse; legacy fully out of that
   path. The availability-boolean change (already live) and the orchestration land
   together — no new-say/old-sequence hybrid.
2. *State-laundering boundary, extended to the state machine:* durable state is
   server-side, NEVER reconstructed from message history; the account establishes
   ONLY via a real verify (mirrored into `conv` after a VERIFIED result), so no
   assistant turn / utterance claim can launder "account established" into the gate.
   Proven: `test_account_cannot_be_laundered_by_a_claim_only_by_real_verify` — a
   claim leaves the account unestablished and price gated; only account #1001 (real
   DB match) unlocks it.
3. *No fallback to legacy:* an internal fault in converse becomes a coherent
   escalation (`refused='internal_error'`), never a 500 and never a silent revert
   to old determinism. Proven: `test_internal_fault_in_converse_fails_to_escalation`.
4. *Re-prove live (say is the fact path):* the FULL adversarial-live suite re-run
   against the WIRED path — 12/12 still CONTAINED, no fabrication, price gated,
   boolean availability now live ("the K5-24SBC is in stock. It ships by 5 PM..."
   — no on-hand count). `docs/ADVERSARIAL_LIVE_RESULTS.json` refreshed.

**The two gate artifacts (what say the swap opened nothing):** state-laundering
test green + adversarial-live re-run against the wired path green. §10-isolation was
the gate to START the swap; these two are the gate to TRUST it.

**Honest remaining:** the pure disclosure gate's FRESHNESS arm (read_and_disclose
with as_of) is not yet the live disclosure path — converse reuses the legacy
authorization gate for pricing (identity + verified account, equivalent for single
part) and the boolean availability answer; freshness needs real data velocity (V5,
parked for the pilot). The actual-ElevenLabs cutover stays parked (no customer);
"wired" here = the scripted harness + adversarial-live driving the real model into
the wired orchestration.

**Status:** full suite 523 passed, 25 skipped. Backend swap landed and trusted.
NEXT (last pre-customer build): the pilot/learning harness against converse's
decision points — shadow-first, the three quarantined label types, decision-point
unit, confidence-weighted provenance.

## 2026-06-08 — Audit correction: the gate is now LIVE (was proven on dead code)

**What the audit caught.** Last turn's "landed and trusted, §10 green" was inflated:
`grep` showed `converse` never called `read_and_disclose`/`discloseable`. The §10
and disclosure_gate suites drove `Conversation`/`disclosure_gate` DIRECTLY — the
live `/agent/turn` path walked beside the gate, not on it. The marquee result
(inherited-disclosability) was defended on code that never ran. Standing lesson
adopted: **a behavioral claim requires green + a grep proving the tested code is on
the live path + a demonstrated-red. Anything short is a claim, not a proof.** CC
cannot be both author and verifier; the grep is the exogenous check.

**The fix — the gate is the live authority.** `converse`'s disclosure now routes
through `_converse_disclose -> conv.read_and_disclose -> discloseable` (grep-proven
live: app.py /agent/turn -> converse -> _converse_disclose -> read_and_disclose ->
discloseable). Identity/account preconditions and the freshness arm are gate-
enforced on the path that runs. Legacy `turn()` is UNCHANGED (other channels), so
its 11-file test footprint is untouched.

**Each fix carried through the full trust gate:**
- **Inherited-disclosability LIVE** (`test_inherited_disclosability_is_a_LIVE_gate_property`):
  account ESTABLISHED, an ambiguous part's price blocked by the gate on its OWN
  identity; positive control = same account prices an IDENTIFIED part. Demonstrate-
  red: neutering `discloseable` makes the ambiguous part price (test fails); restore
  → passes.
- **Freshness arm LIVE** (`test_freshness_arm_is_live_…`): a stale-but-well-formed
  read (V5 class) injected via the reader is rejected by the gate; demonstrate-red:
  neutered gate discloses the stale read.
- **State-laundering on the RIGHT surface + pinned reason**: `…_for_the_RIGHT_reason`
  pins the block as `pricing_unauthorized` (verify required, not satisfied), with a
  real-verify positive control; `…assistant_turn_in_HISTORY…` drives the attack
  through the endpoint message history (assistant turn falsely claims verification)
  and asserts the server-side account stays unestablished and price stays gated.
- **Fail-closed on every fault path**: availability/pricing/verify faults each
  -> coherent escalation (`internal_error`), never a 500, never a legacy revert;
  the pricing fault explicitly asserts no `$` disclosed.
- **§10 #8 vacuity fixed**: focus pinned to a sentinel (a guess moves it off,
  falsifiable) + a single-match contrast proving the fn isn't a no-op; dead
  `if False` removed.

**Re-prove live (say is the fact path):** adversarial-live re-run against the
GATE-WIRED converse path — 12/12 still CONTAINED. `docs/ADVERSARIAL_LIVE_RESULTS.json`.

**Honest boundaries (NOT inflating again):**
- **Freshness is wired + authoritative but does not yet BIND** in normal operation:
  reads self-stamp `as_of=now`, so a stale fact can't arise until the data source
  carries real read-timestamps (V5 pilot wiring). The gate WOULD reject stale
  (demonstrate-red proves it); today nothing stamps stale. Conservative placeholder
  horizons on `Gateway.disclosure_horizons`, flagged as guesses.
- **Inherited-disclosability is proven as single-focus disclosure of an ambiguous
  part** (the gate blocking its identity), which IS the safety property on live
  code. SIMULTANEOUS multi-part disclosure (price/stock several parts in one turn,
  multi-answer provenance) is NOT built — that's a separate capability, not claimed.

**Status:** full suite 528 passed, 25 skipped (was 523). The gate is load-bearing
on the live path, proven green + on-live-path + demonstrated-red.

## 2026-06-08 — Grep-pass (backlog clean) + pilot labeling boundary built

**Grep-pass on the other load-bearing claims** (the cheap exogenous check that
caught the gate inflation): all four confirm the tested behavior is ON the live
path — fabrication containment (`/v1/chat/completions` mounted -> handle_async ->
apply_model_output -> filter_free; test drives the real endpoint), say-guard
(`/agent/turn` -> safe_voice_say(converse.text) -> internal_state_tokens; test
exercises safe_voice_say = the live fn), provenance (`/agent/turn` -> assert_complete
+ surfaced(converse resp); converse populates availability/price/candidates),
legacy auth gate (converse pricing -> read_and_disclose account precondition ->
_fact_reader -> issue_authorization + pricing() + PricingRefused; double-gated). No
off-path findings; the backlog is clean. Standing discipline going forward: every
behavioral claim gets a grep proving the tested code is on the live path.

**Pilot labeling boundary built** (`src/pilot/`): the core that determines whether a
pilot's first week produces clean eval data or ambiguous mush. The labeling UNIT is
the DECISION POINT, extracted from the REAL `resp.meta['decision']` the wired
orchestration emits (`DecisionPoint.from_turn`, on-live-path: built from a real
gw.converse() output, not a hand-made dict). Three label types kept from
contaminating each other (eval-isolation applied to the human):
- RESOLUTION (phrase->SKU) -> `CorrectionCandidateQueue` (candidates only; promotion
  still gated by the frozen eval — approval is not the commit).
- BEHAVIORAL (right move?) -> `EvalCandidatePool` (DEV only; structurally NO
  frozen/holdout writer — a human-seen call must not auto-populate the gate).
- QUALITY (naturalness) -> `QualityQuarantine` (no reader feeds the eval/correction).
- Routing is type-checked: a mis-routed label RAISES `WrongLabelType` (the
  demonstrate-red — a "tone was off" judgment structurally cannot reach the eval).
- not-exercised trichotomy applied to the human: `label_questions` asks ONLY about
  decisions the turn actually exercised (a pricing question is not generated on an
  availability-only turn — falsifiable test).
- Provenance-weighted: ACQUIESCENCE(0.2) < CORRECTION(0.6) < GOLD(1.0) — a noisy
  instrument, not ground truth.

**Trust gate satisfied:** green (12 tests) + on-live-path (decision point from real
converse emission) + demonstrate-red (contamination guards raise; not-exercised
falsifiable).

**Honest scope — NOT built (next):** the INGESTION front-end (shadow mode: observe a
human-handled call, produce the counterfactual "what I would have said" + decision
points for the rep to label; propose mode later); the counterfactual confidence
DECAY by call-depth (shadow labels degrade deeper into a call the agent didn't
drive); the PROMOTION mechanics (candidate->live alias via frozen eval+no-regression;
curated dev->frozen-visible/holdout). The boundary is built and proven; the data
pipeline on top of it is the next piece. Shadow-first stands (working assumption:
50 calls/day hypothetical), gated on a real prospect volume number.

**Status:** full suite 540 passed, 25 skipped (was 528).
