# Production Plan — scoped slice (M0 / M1 / M3)

**Purpose:** take a validated research prototype (a deterministic SKU
resolution engine for a ~10K-SKU industrial catalog) to genuine production
standard on a narrow slice: provable correctness, verified guarantees, clean
CI. Depth over breadth, deliberately.

**Architecture spine** (carried from prior private work's locked
experimental record): *rules own canonical output and all binding fields;
LLMs propose, extract, and draft prose only.* See the DECISION entry in the
top-level README for the evidence.

---

## M0 — Consolidation ✅ definition of done

- [x] One importable src-layout package (`sku_translator`)
- [x] Full integration suite green in CI on every commit (count recorded at
      collection time, never asserted from documentation)
- [x] Round-trip audit in CI over the entire catalog, count derived at
      runtime: identity 100%, zero silent rewrites beyond the pinned
      baseline, full-round-trip coverage ≥ 95%
- [x] Readiness gate (`scripts/readiness.py`): ready=false fails the build;
      green is tied to a specific commit and a clean tree
- [x] Stale partial copies tombstoned at their old locations
- [x] A fresh reader can navigate from the README alone (handoff +
      vocabulary docs live in `docs/`, not in someone's Downloads)

## M1 — Deterministic ship-date engine ✅ definition of done

- [x] Synthetic inventory assigned to 100% of catalog SKUs (seeded,
      re-runnable generation script; ~85% in stock weighted by sales
      frequency, remainder out-of-stock with synthetic lead times)
- [x] `ship_date()` is a PURE, TOTAL function — zero LLM imports anywhere in
      its module graph (verified by import-graph test)
- [x] Business rule: in-stock orders ship by 5:00 PM the next business day
      after receipt; out-of-stock by lead time + processing rule
- [x] Property tests sweep every catalog SKU × randomized + boundary
      timestamps (4:59/5:01 PM, Friday, Saturday, holiday eve, Dec 31) with
      zero undefined results
- [x] Golden test table of ≥ 12 named edge cases IS the business-rule spec
- [x] Open policy decisions (weekend receipt, order cutoff, partial-stock)
      locked as decision-log entries before coding

## M3 — Unified resolution service ✅ definition of done

- [x] One `resolve()` API: deterministic translator first, retrieval
      fallback only for UNRESOLVABLE/low-confidence
- [x] Never-invent guarantee verified across BOTH paths — adversarial test
      confirms no RESOLVED result ever references a non-catalog SKU
- [x] Per-tenant isolation enforced at the index level and proven by
      adversarial test (tenant A query can never resolve against tenant B's
      catalog), not by assertion
- [x] Every response carries: state, source path, confidence, flags, and the
      catalog row version it resolved against
- [x] `needs_review` emitted via the EXISTING structural flag rule, with its
      known-deficient F1 (0.15–0.33 at production accuracy, measured
      2026-05-02) documented inline — redesign is out of scope (see below)

---

## M-ERP — Adapter agent harness ✅ definition of done

Built per `docs/ERP_ADAPTER_HARNESS_SPEC.md` (all eight components + twin +
golden path; 41 tests, every detector validated against planted faults):

- [x] C1 manifest: sufficiency AND per-grant minimality proven on the twin
- [x] C2 enforcer: adversarial writes refused; zero writes at the twin's own audit log
- [x] C3 discovery: 5-fault mutation matrix, all named; custom 50000+ fields flagged
- [x] C4 catalog decode: planted family word discovered, not echoed
- [x] C5 probes: measured-with-method; tracks twin reconfiguration; write experiments deferred by name
- [x] C6 gaps: totality (mapped ∪ gapped = contract, mechanical); revocation reclassifies
- [x] C7 profile: state machine rejects illegal states; adversarial wrong mapping caught and preserved
- [x] C8 drift: rename → halt with named diff → acknowledge → v+1 with approval reset
- [x] Golden path: translator identity guarantee holds against the adapter-synced catalog
- [x] Rubber-stamp mitigation: a rejected profile demonstrably cannot sync

## Out of scope — deliberately

| Milestone | What it was | Why excluded |
|---|---|---|
| M2 | Flag-rule redesign experiment (F1 ≥ 0.65 target) | Experiment-shaped work with its own pre-registration discipline; M3 ships the existing rule with its limitation stated plainly |
| M4 | Voice/email/PDF modality front-ends | Breadth, not depth; the validated model-routing table exists in prior private work (under NDA) |
| M5 | Quote generation engine | Depends on M1+M3; out of slice |
| M6 | Agent loop + HITL approval surfaces | Product integration, not engineering-judgment demonstration |
| M7 | Load tests, multi-tenant ops hardening, runbooks | No reader of this repo will ever run them; isolation correctness (the provable part) lives in M3's adversarial tests |

No forward-looking hooks, stubs, or abstractions for excluded milestones.
If a seam is awkward because of an exclusion, the awkwardness is flagged in
code comments rather than smoothed over with speculative architecture.
