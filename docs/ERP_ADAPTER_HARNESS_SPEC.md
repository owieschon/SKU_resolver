# ERP Adapter Agent Harness — Dispatch Spec v1

**Status:** IMPLEMENTED 2026-06-07 (`src/erp_harness/`, `src/erp_twin/`, 41 tests — every detector validated against planted faults). Spec locked 2026-06-07 before code; deviations recorded in docs/DECISION_LOG.md D8–D10.
**Date:** 2026-06-07
**Validation target:** the NAV/BC replica twin defined in `erp-replica-research-spec.md` (2026-06-03) — NOT a live tenant ERP. No live-tenant run until the full fault-injection check matrix passes on the twin.
**Canonical copies:** prior private work held under NDA and the sku-resolution-engine repo (`docs/`). The repo copy exists because this harness is the live-ERP-sync milestone in that project's path to production.

---

## 1. Purpose

Per-tenant ERP onboarding is the dominant scaling cost. A static adapter per ERP product fails because tenant customization is unbounded and publicly unquantifiable (erp-replica spec, customization-prevalence gap): you cannot pre-enumerate what every tenant did to their NAV instance, but you can build the thing that maps it in an afternoon per tenant.

The harness is a three-phase agent system that (A) identifies the connection type and emits a least-privilege **permissions manifest** before any access, (B) explores the granted surface read-only under hard code-enforced budgets, and (C) emits a typed, versioned, human-reviewed **Tenant ERP Profile** that a *deterministic* adapter consumes.

## 2. Architecture spine (non-negotiable)

| Rule | Application |
|---|---|
| **Agent proposes; code binds** | The agent never writes adapter code or sync config directly. It proposes mappings; deterministic verification probes accept/reject them; only verified mappings enter the profile; only the profile drives sync. |
| **Read-only by grant, not by promise** | Exploration credentials are scoped read-only at the ERP. The safety enforcer (C2) additionally blocks write methods in code. Two independent layers. |
| **Budgets are code, not judgment** | Rate ceilings, total-call budgets, and backoff live in the only transport the agent can use. The agent cannot exceed them by being clever. |
| **Every agent claim is verified** | A proposed mapping is hearsay until a deterministic probe confirms it against sampled data. Unverified claims are recorded as `proposed`, never silently accepted. |
| **Human gate before binding** | The profile ships to a named human reviewer with a diff-style checklist. Sync starts only on explicit approval. Schema drift after onboarding halts sync pending re-approval. |
| **Prove the check catches the fault** | Every detector is validated against *injected* faults on the twin, not by running clean. A detector that has never caught a planted fault is unproven. |

## 3. Components

### C1 — Recon & Permissions Manifest Generator (Phase A)

Input: ERP identity descriptor (vendor, product, version, deployment type, endpoint URL if known). Output: `permissions-manifest.json` + `permissions-manifest.md` (the IT-facing document: every requested grant with object, scope, and one-line justification; explicit statement of what is NOT requested).

v1 supported classes: **BC SaaS (OAuth/Entra)** and **NAV on-prem (read-only SQL user)**. P21 and Eclipse are explicitly stubbed `unsupported` with a named reason (no public instance access; reconstruction-grade docs only — per erp-replica spec).

**DoD:**
- [ ] For each supported class, the manifest enumerates 100% of grants Phase B will use — nothing Phase B calls is absent from the manifest
- [ ] Zero write/admin scopes appear in any exploration manifest (checked mechanically against a deny-list)
- [ ] Deterministic: same descriptor → byte-identical manifest
- [ ] The `.md` rendering passes a plain-language review: an IT admin can action it without a call

**Smoke:** fixture descriptor → manifest validates against its JSON schema; deny-list check green.
**E2E behavioral:** apply the manifest's grants to the BC twin → Phase B completes (sufficiency); then for each grant, revoke it singly and re-run → Phase B fails **loudly with the missing grant named** (minimality + no silent degradation). Both directions mandatory.

### C2 — Rate-Budget & Safety Enforcer (pure code; the agent's only transport)

Hard ceilings per ERP class (BC default: 50% of the documented 429 threshold), total-call budget per run, exponential backoff on 429/503/504, HTTP method allowlist (GET/HEAD + read-only OData), full request journal (append-only JSONL).

**DoD:**
- [ ] The agent has no transport except the enforcer (verified by import-graph test, same pattern as `test_fulfillment_purity.py`)
- [ ] Write-method attempt → typed refusal exception + journal entry; never reaches the wire
- [ ] Budget exhaustion → clean halt with partial-results manifest, not a crash
- [ ] Journal is sufficient to replay/audit every call made in a run

**Smoke:** unit tests — write blocked, ceiling trips, backoff sequence correct, journal entries complete.
**E2E behavioral:** adversarial agent prompt ("verify write access by updating a test record") → refusal recorded in journal, **twin's own audit log shows zero write attempts received**. The twin-side check is the point: prove at the destination, not the source.

### C3 — Surface Discovery & Schema Profiler (Phase B)

`$metadata` crawl (BC/OData — the only surface where tenant tableextension custom fields are visible, per erp-replica finding) or `information_schema` crawl (NAV SQL). Entity inventory, field types, sample-row profiling (null rates, cardinality, value distributions), FK inference, custom-field discovery (50000+ field-number range).

**DoD:**
- [ ] Discovers 100% of entities exposed by the granted surface on the twin (twin entity list is ground truth)
- [ ] Custom-field discovery proven against *injected* synthetic tableextensions — all planted fields found and flagged as custom
- [ ] Reproducible: identical twin state → identical profile fragment (modulo timestamps)
- [ ] Completes within the C2 budget on a twin sized to realistic tenant scale

**Smoke:** run against twin, profile fragment validates against schema, within budget.
**E2E behavioral (fault-injection check matrix):** inject K known mutations on the twin — renamed column, added custom field, hidden entity, changed type, dropped FK — harness output names **all K**, each classified correctly. A run that misses any planted mutation fails the component.

### C4 — Item-Master Catalog Decode Module

Two paths, one report:

1. **Known-grammar pass** (`catalog_decode.analyze_items`) — runs an existing
   tenant grammar over the discovered items entity (vocabulary co-occurrence +
   family histogram). Fast when the tenant's grammar is already written.
2. **Tenant-agnostic induction** (`grammar_induction.decode_catalog`) — for an
   UNKNOWN tenant, *learns* the SKU grammar from the strings themselves,
   generalizing the example catalog decoder's techniques (segmentation, family-by-
   prefix, per-family regex induction, positional role semantics, separator/
   case normalization) rather than its hardcoded patterns. Iterative: round 0
   is structural induction (the first result — large coverage, zero human effort);
   later rounds propagate high-confidence clues to same-shape families; the loop
   stops at diminishing returns and hands the residual to manual decodification,
   ranked by SKUs unlocked. Roles (diameter/length/finish/sequence) come from
   correlating segment values with description text.

Architecture spine: induction PROPOSES. Every family and segment role is an
`Assumption(status='proposed')` with evidence + confidence for an SME to confirm
or correct; segments that don't correlate become ranked SME questions, each
naming what answering it unlocks. An optional `LLMRoleProposer` (task tier
`catalog_decode_role`) labels still-unknown segments — proposes only, confidence
floored, human gate unchanged.

**Ingestion** (`catalog_source.py`): one decoder, many sources. Pure
row-extractors (`rows_from_catalog_lines` PDF/text, `rows_from_worksheet` Excel,
`rows_from_html_tables` web) emit `{sku, description}`; thin `*CatalogSource`
adapters do the I/O (pypdf `[pdf]` extra / openpyxl core dep / stdlib
html.parser). A new source = a pure function + a tiny adapter; the decoder is
untouched.

**DoD:**
- [x] On the example catalog fixture catalog, induction recovers family structure (SB/ZP/L/S/K/… ; ~55% structural coverage) WITHOUT the hardcoded parser
- [x] Validated on an unseen third-party catalog (World American engine parts PDF, ~1,200 SKUs): WA family discovered, ~66% structural coverage, line-code/sequence segments correctly surfaced as SME questions
- [x] SME question list is ordered by volume-of-SKUs-resolved and each question names what answering it unlocks
- [x] Assumptions carry evidence + confidence and are never auto-confirmed; clue propagation and diminishing-returns termination are tested; fault-injection check on a planted unknown grammar (diameter/finish inferred, opaque segment left as a question)

**Smoke:** 200-row sample → report with all sections present.
**E2E behavioral:** feed the report's vocabulary output into the translator's alias table for a held-out fixture → previously-unresolvable family-word inputs resolve.

### C5 — Behavior Probes (empirical, read-only)

Measured (not assumed): throttle response curve, pagination semantics, timestamp/timezone handling, observable eventual-consistency lag (posting-queue visibility — observation only). Idempotency and write-path consistency experiments (erp-replica E1) are **explicitly deferred to the sync phase** and marked as such in output — exploration never writes.

**DoD:**
- [ ] Every probe emits a measured value + the method that produced it (no asserted values)
- [ ] All probes route through C2; ceiling never exceeded (journal-verified)
- [ ] Deferred experiments appear in output as named deferrals, not gaps or guesses

**Smoke:** probes run on twin, emit numbers with method fields populated.
**E2E behavioral:** configure the twin's throttle mode to a known profile → probe reports it within tolerance. Reconfigure → probe tracks the change.

### C6 — Gap Detector (canonical-contract coverage)

For every field the canonical contract requires (CatalogIndex shape, inventory shape, order shape), the detector produces exactly one of: a verified mapping, or a **named gap** with remediation class — `custom_api_page_required` (the Value Entry / Item Vendor class, vendor-confirmed absent from standard v2.0), `alternative_entity`, `permission_gap`, or `unavailable`.

**DoD:**
- [ ] Totality: mapped ∪ gapped = 100% of the canonical contract; the sum is checked mechanically
- [ ] On a twin with Value Entry hidden from the standard surface, detects and classifies it `custom_api_page_required`
- [ ] Permission-gaps are distinguished from absence-gaps (revoking a grant changes the classification, not the count)

**Smoke:** coverage sum = 100% on every run.
**E2E behavioral:** revoke one entity grant → that entity's contract fields re-classify to `permission_gap`, everything else unchanged.

### C7 — Tenant ERP Profile + Verification Probes (Phase C)

Typed, versioned JSON artifact: entity/field mappings (each carrying embedded verification evidence — probe sample size, type-check result, canonical round-trip result), schema fingerprint baseline, rate budget, consistency flags, named gaps, and the human-review checklist. Mapping states: `proposed → verified | rejected`. **Only `verified` mappings are consumable by the adapter.**

**DoD:**
- [ ] Profile validates against its schema; schema is versioned
- [ ] 100% of consumable mappings carry verification evidence; a mapping without evidence cannot reach `verified` (enforced in code, not convention)
- [ ] Human-review artifact renders every mapping, every gap, every probe stat as a reviewable diff-style checklist
- [ ] Fingerprint baseline embedded and reproducible

**Smoke:** schema validation; a hand-built known-bad mapping (wrong type) is rejected by the probe.
**E2E behavioral (planted-fault test):** prompt the agent adversarially so it proposes a wrong mapping (e.g., description field as SKU) → the verification probe rejects it → the profile records it under `rejected` with the probe's reason. The wrong claim must be *caught and preserved as evidence*, not silently dropped.

### C8 — Drift Guard (post-onboarding)

Scheduled re-fingerprint against the baseline. Unacknowledged drift → **sync halts** with a human-readable named diff. Acknowledged drift → profile version bump through the review gate, then resume.

**DoD:**
- [ ] Injected drift on the twin (rename a column) → halt within one cycle, diff names the exact change
- [ ] N unchanged cycles → zero false halts
- [ ] Resume requires an explicit acknowledgment artifact; no auto-resume path exists

**Smoke:** fingerprint determinism — two runs, same twin state, identical fingerprints.
**E2E behavioral:** rename → halt + diff → acknowledge → resumes under profile v+1; the journal shows the full sequence.

## 4. Whole-system Definition of Done

The harness is done when the **golden path runs end-to-end on the BC twin, in CI-able form**, and every detector has demonstrated its catch:

- [ ] **Golden path:** fresh twin → C1 manifest → grants applied → C3–C6 exploration completes within budget → C7 profile with 100% verified-or-gapped contract coverage → human approval recorded → deterministic adapter syncs the items entity into a CatalogIndex-shaped store → **the SKU translator resolves a canonical SKU against the adapter-synced catalog with the identity guarantee intact** → C8 drift injection halts sync
- [ ] All eight component DoDs green; all smoke + E2E behavioral tests green in one suite
- [ ] **Fault-injection check matrix:** every injected fault class (≥1 per detector: missing grant, write attempt, schema mutation ×4, hidden entity, wrong mapping, drift) caught and correctly named — a matrix row that runs clean against a planted fault fails the whole
- [ ] **Zero writes ever reached the twin during exploration** — verified from the twin's audit log across the entire test history, not from the harness's own journal
- [ ] Anything experiment-shaped that emerged during the build was pre-registered before running (standing discipline)
- [ ] Review artifacts: every profile in the test history has a recorded human (or fixture-human) approval; no sync ever started without one

The final golden-path step is deliberate: the harness's acceptance criterion is not "it produced a profile" but "**the translator runs against an adapter-synced catalog and the never-invent guarantee still holds**." That ties the onboarding layer to the resolution layer's existing verified guarantees.

## 5. Mandatory QA summary

| Component | Smoke (fast, every commit) | E2E behavioral (the catch) |
|---|---|---|
| C1 manifest | schema-valid, deny-list green | sufficiency + per-grant minimality on twin |
| C2 enforcer | write blocked, ceiling trips | adversarial write prompt → zero writes at twin |
| C3 profiler | fragment valid, within budget | K injected mutations, all named |
| C4 catalog decode | report sections on sample | vocabulary output resolves held-out inputs |
| C5 probes | measured values + methods | tracks reconfigured twin throttle |
| C6 gaps | coverage sums to 100% | revoked grant reclassifies correctly |
| C7 profile | bad mapping rejected | adversarial wrong mapping caught + preserved |
| C8 drift | fingerprint determinism | rename → halt → ack → v+1 resume |
| **Whole** | golden path on small twin | full fault matrix + translator-against-synced-catalog |

Smoke tests run on every commit. E2E behavioral tests run on every merge to main and before any tag. **No component ships on smoke alone.** A new detector merged without its planted-fault E2E test is a structural defect, not a style issue.

## 6. Out of scope (v1)

- Live-tenant runs (gated on the full matrix passing on the twin)
- P21 / Eclipse adapters (no instance access; documented stubs only)
- The sync engine beyond the minimal items-entity sync the golden path requires
- Write-path experiments (idempotency, draft-then-commit — erp-replica E1 territory, sync-phase work)
- Closing `custom_api_page_required` gaps (per-tenant AL engineering, priced into onboarding)

## 7. Open questions (cross-referenced)

Inherits the erp-replica spec's G-series gaps wholesale; the harness-specific additions: CRONUS demo-license posting-date window collides with consistency probes (resolve per erp-replica options before C5 build); twin sizing for realistic-scale budget validation; whether the fixture-human approval in CI is a rubber stamp risk (mitigation: CI approval fixtures must include at least one profile that *should be rejected*, and the test asserts it is).

## 8. Relationship to existing work

- **erp-replica-research-spec.md (2026-06-03):** provides the validation twin and the empirical findings this spec builds on (throttling, $metadata-only custom fields, missing standard APIs, consistency mechanisms)
- **Catalog Decoding Prompt (May 2026):** C4 is its productized, agent-executed form
- **sku-resolution-engine:** the consumer. `ERPCatalogIndex` is the stub this harness's adapter fills; the golden path's final step is that repo's identity audit running against synced data
- **the platform's Stage 4 (ML training agent architecture):** this harness is the data-onboarding component's foundation — same agent, data-layer entry point
