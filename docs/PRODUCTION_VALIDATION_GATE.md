# Production Validation Gate (R4)

**Purpose.** Some failure modes cannot be closed in code or against a
synthetic twin — they only reveal themselves against the real thing. This
document names them, and names the experiment that must pass against real
infrastructure before the corresponding capability ships to a paying tenant.
It is the accurate boundary of "141/172 tests green": green proves correctness
against *our model* of the failure modes, not against the field.

Each gate item has: the unknowable, the experiment that closes it, and a
**fault-injection check** acceptance criterion (run clean ≠ validated; the
experiment must catch a real fault shape, not just pass).

---

## V1 — ERP adapter against the real container twin

The synthetic BC twin (`erp_twin`) implements *documented* BC behavior. The
real NAV/BC container twin (`erp-replica-research-spec.md`) and, later, a
live tenant, are where undocumented behavior lives.

| Unknowable | Experiment | Fault-injection check acceptance |
|---|---|---|
| Real `$metadata` quirks + scale (hundreds of entities, MB-scale XML) | Run C3 discovery against the container twin's real `$metadata` | Discovery completes within budget AND a hand-injected custom tableextension on the real instance is found and flagged |
| Sustained-load throttling (real 429 curve, not the modeled one) | C5 throttle probe against the live instance under a measured burst | Probe's measured threshold is within tolerance of the documented 600/min, AND a deliberate over-budget burst is refused by the enforcer, journal-verified |
| Eventual-consistency timing (posting queue, deferred G/L, cost adjustment) | C5 posting-queue probe across a real posted transaction | Probe observes a non-zero lag that drains over time; no write is issued (twin audit + real instance audit both show zero writes) |
| Custom-field convention adherence (do tenant fields really use 50000+?) | Inventory a real tenant's `$metadata`; measure how many custom fields honor the range | If any custom field is OUTSIDE the assumed range, the discovery heuristic is corrected before that tenant ships (the catch: a planted out-of-range custom field is currently MISSED — verify and fix) |
| Missing-standard-API gaps are real per tenant | Confirm Value Entry / Item Vendor absence on the live instance | Gap detector classifies both as `custom_api_page_required`; the custom AL page is built and re-verified before cost/lead-time features ship |

**Standing rule:** no live-tenant onboarding run until V1's full demonstrate-
the-catch matrix passes on the container twin, and `verification_preflight`
(observability deploy guard) confirms the run is against current, committed
code.

## V2 — Voice gateway against real audio (the call-capture lesson)

The locked call-capture arc (2026-05-02) already proved the critical
unknowable: **synthetic transcripts ≠ real ASR output.** `SimulatedASR` will
pass tests that real AssemblyAI on real customer audio fails.

| Unknowable | Experiment | Fault-injection check acceptance |
|---|---|---|
| Real ASR error distribution on real customer speech | Credential-gated live smoke: real call via Twilio → AssemblyAI stream → G3 | A scripted call naming a known SKU is identified-after-readback; a deliberately mis-spoken SKU produces candidates + readback, never a silent wrong ID |
| Boost-hallucination on real audio (FB-5C→FB-5ZN class) | Replay the H1 confusion-pair audio through the live stream | The wrong SKU is caught by discriminating readback (#11), not accepted |
| keyterms boosting efficacy on the live model | Compare identification rate with/without catalog-derived keyterms on a real call set | keyterms improves identification AND does not raise the hallucination rate (the call-capture finding, re-confirmed live) |
| Caller-confirmation reliability (does the human actually evaluate?) | Operator review of real confirmed calls: how often did a caller affirm a wrong readback? | Below a pre-registered threshold; above it, the discriminating-readback design is revised (this is the #11 conditional-collapse risk measured in the field) |

**Standing rule:** unattended voice ingestion remains gated on the M2 flag
redesign regardless of V2. V2 validates only the ATTENDED gateway, where the
caller is the loop.

## V3 — Pricing & authorization against a real customer DB

| Unknowable | Experiment | Fault-injection check acceptance |
|---|---|---|
| Real account-name ambiguity (how often does a name match 2+ accounts?) | Run G2 verification against the real customer DB name distribution | The 0/1/many disambiguation fires correctly on real collisions; a scripted enumeration attack still locks out |
| Is name-only identity strong enough for this tenant's risk tolerance? | Operator/security review of the entitlement model against real pricing sensitivity | A second factor is added if review demands it; the decision is logged (the spec already flags name-only as weak) |
| Real per-account pricing source (the Value Entry gap) | Wire G5 to the real pricing source once the custom AL page (V1) exists | A verified account sees its own pricing; a cross-account request is refused at both gate layers, journal-verified |

## V4 — Service hardening at real scale

| Unknowable | Experiment | Acceptance |
|---|---|---|
| BM25 index build cost at real catalog size (100K+ SKUs) | Build/persist timing on a real-scale catalog | Within the resolution latency SLO; if not, the persisted-index path (R2 deferral) is built |
| Memory-store growth (rep choices / sessions accumulate unbounded) | Run with a retention policy under sustained load | Growth bounded; retention enforced (borrow a prior retention-policy mechanism) |

## V5 — Stale-but-well-formed data (the dependency that LIES, not errors)

**Why filed (2026-06-08).** The fault-injection harness covers the dependency
that *errors* (exception → fail-closed escalation). It cannot catch the dependency
that *returns successfully with wrong/stale data*: resolution returns a SKU valid
yesterday and superseded this morning; inventory returns a number correct for a
different warehouse; the gateway returns a real, well-formed, confidently-wrong
fact that substitutes into the say verbatim and is spoken with full confidence.
None raise, none trip a fail-closed wrapper. This is the gap between "the
dependency errored" and "the dependency lied," and an exception harness cannot
reach it. **The planned guard exists in design:** `CONVERSATION_STATE_SPEC` §3.2/
§3.3 (`fresh(fact, now)` + per-fact-type `HORIZON`) + invariant 4 (no binding fact
spoken without an `as_of` within horizon). The disclosure gate (built 2026-06-08,
`src/gateway/disclosure_gate.py`) enforces freshness structurally; what's unknowable
without the customer is the HORIZON values — they are conservative placeholders
until the customer's real data velocity (how fast does stock actually move, how
often does pricing change) is known.

| Unknowable | Experiment | Fault-injection check acceptance |
|---|---|---|
| The real per-fact-type data velocity (availability/lead-time/price horizons) | Tune `HORIZON[fact_type]` against the customer's actual catalog/stock/pricing update cadence | A fact read past its real horizon is re-read before speaking; a planted stale-but-well-formed read is caught by `fresh()` and not spoken |
| How the customer's catalog signals supersession/freshness (a version? a timestamp? nothing?) | Wire the resolution path to emit a real `as_of` + catalog-version on each resolved fact, mapped from the customer's freshness mechanism | A superseded SKU resolved from a stale index fails `fresh()` (or its precondition) and is not quoted; the catch is a planted supersession |

## V6 — Correlated load (two seams slow at once)

**Why filed (2026-06-08).** Single-fault injection (one dependency at a time) is the
right way to BUILD the harness, but production faults correlate: model-API
rate-limiting (S4) coincides with general load that also slows the gateway (G2/G3).
The B-as-model-route-budget design assumes the substitution route is fast because
the gateway is fast. Confirmed in code (`test_correlated_load_*`): the substitution
route is safe under correlated load for a STRONGER reason than "gateway fast" — it
does ZERO I/O (the gateway result is already in the request body; its latency was
spent on the prior `/agent/turn` hop). What CANNOT be closed without real infra:
the gateway's OWN internal dependency calls are not bounded by an internal timeout —
a *hanging* (not erroring) dependency makes `/agent/turn` slow until ElevenLabs'
`response_timeout_secs` (12s) fires. Under real correlated load that 12s ceiling is
the only bound.

**This is the SAME CLASS as the async-cancel finding, one layer down and not yet
fixed:** a latency fail-closed that is currently bounded only by *someone else's*
ceiling (ElevenLabs' 12s), not ours — exactly the gap that, at the model route, was
"return fallback while the request runs on." The decision to defer (don't build a
timeout you can't tune) is correct, but when real-load measurement comes back, an
internal per-dependency budget is NOT a new problem — it is the cancel discipline
applied to the gateway's internals, a pattern already built once (handle_async's
real deadline). File it as a known pattern so the measurement result is expected,
not a surprise.

| Unknowable | Experiment | Acceptance |
|---|---|---|
| Does the gateway need an internal per-dependency timeout (vs relying on ElevenLabs' 12s tool ceiling)? | Measure `/agent/turn` latency under real correlated load (slow customer-DB + slow resolution simultaneously) | If p99 `/agent/turn` latency under correlated load approaches the 12s ceiling, an internal per-dependency budget is added; the catch is a planted slow (not erroring) dependency that currently hangs to the ceiling |
| Multi-tenant isolation under concurrency (not just the in-repo adversarial test) | Concurrent load across two real tenants | Zero cross-tenant resolution or pricing disclosure under load |

---

## How this gate is used

1. Each capability's ship checklist references its V-section here.
2. No V-experiment is "passed" on a clean run alone — the demonstrate-the-
   catch column must be satisfied (a planted/real fault was caught).
3. Results are journaled; `verification_preflight` gates every live run
   against stale code.
4. This document is updated as experiments run — a passed gate item records
   the date, the instance, and the caught-fault evidence.

The point of this file: a future engineer can see exactly
where the synthetic-validation boundary is, and that the boundary is named and
gated rather than quietly crossed.
