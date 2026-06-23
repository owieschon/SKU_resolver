# SKU Resolution Engine

Turn free-form human input — typed, spoken-then-transcribed, or pasted — into a
**verified catalog part number**, for an industrial parts catalog of ~9,500
SKUs. A counter rep hears *"five inch chrome curved stack, twenty-four long"*;
the engine answers `K5-24SBC` — or, when it isn't sure, it says so instead of
guessing.

The one rule the whole system is built around:

> **The engine cannot invent a part number.** Every resolved answer points at a
> real row in the catalog, and that property is re-checked over the *entire*
> catalog on every commit. When the input is ambiguous or unknown, the result
> is an honest `pending` / `unresolvable` — never a plausible-looking fake.

```
text ─▶ normalize ─▶ extract ─▶ ┌─ verbatim ─┐
 "5in chrome          (spec)    │  grammar    │ ─▶ TranslationResult
  curved 24 SB"                 │  construct  │     (sku · state · source ·
                                │  fuzzy      │      confidence · flags)
                                │  memory     │
                                └─ disambiguate ┘
```

---

## Why this exists (the core design decision)

In this domain the worst possible failure is a **wrong-but-plausible** part
number on a quote or order: `K4-12SBA` and `K5-12SBA` are one keystroke apart
and a different physical part. A mistake like that survives human review
*because it looks right*, and it gets caught downstream — on the loading dock,
or by the customer.

So the binding output is produced by **deterministic rules, not a language
model**:

- A hand-authored grammar (312 compiled patterns) decodes a canonical SKU into
  structured fields (family, diameter, length, finish, …), and the same grammar
  runs in reverse to rebuild it — so resolution is auditable and reproducible.
- Resolution runs in well under a millisecond per call, with **no network and
  no LLM in the hot path**.
- An LLM has a role, but only as a *proposer* in the larger system (suggesting
  candidates, driving the conversational front-end) — never as the author of the
  final part number. A model that is usually right is the wrong tool when the
  cost of a confident error is this high.

See [`docs/DECISION_LOG.md`](docs/DECISION_LOG.md) for the decisions, the
alternatives weighed, and their trade-offs.

---

## Architecture

Four layers, each able to run on its own; the deterministic core never depends
on the layers above it.

| Layer | What it does | Where |
|---|---|---|
| **Resolution core** | text → normalize → extract → resolve → result. Six resolution paths, each with a confidence grade. No LLM, no network. | `src/sku_translator/`, `src/resolution/` |
| **Fulfillment** | A pure, total `ship_date()` from (inventory, qty, order time) to a dated promise, each tagged with the rule that produced it. | `src/fulfillment/` |
| **Onboarding / catalog induction** | Point it at an *unknown* tenant's catalog and it infers the SKU grammar from the strings alone (segment roles from description co-occurrence), emitting reviewable assumptions and ranked questions for what it can't crack. Plus a least-privilege ERP adapter harness. | `src/erp_harness/`, `src/erp_twin/`, `src/erp_transport/` |
| **Conversational gateway** | Chat + voice front-end. Availability/lead-time are open; pricing is gated behind account verification; the same gates hold over the phone. | `src/gateway/`, `src/runtime/`, `src/model_provider/` |

**Storage / serving.** The catalog and inventory are flat files
(`data/catalog.csv`, `data/inventory.json`) read through a `CatalogIndex`
interface, so the same code accepts a production ERP-backed index unchanged.
The runtime is a FastAPI app (`src/runtime/`) exposing the chat/voice endpoints.

[`docs/README.md`](docs/README.md) is the full documentation index.

---

## Verified on every commit

These are checked by the test suite and `scripts/roundtrip_audit.py` against the
catalog in this repo — clone it and reproduce them yourself.

| Property | How it's checked | Result |
|---|---|---|
| Every catalog SKU resolves to itself | Identity audit over the full catalog (count derived at runtime) | **9,487 / 9,487** |
| No silent rewrites | `construct(extract(sku))` vs. a pinned baseline | **0 new** |
| Grammar invertibility | `construct(extract(sku)) == sku` coverage | **96.96%** (floor 95%) |
| Never-invent, under attack | seeded mutation fuzz + injection/unicode corpus: no resolved SKU outside the catalog | `tests/test_resolution_adversarial.py` |
| Tenant isolation | two services, disjoint catalogs, attacked both directions | same file |
| `ship_date()` is total & pure | property sweep over the full catalog × boundary timestamps; AST check that the fulfillment closure imports no LLM/network | `tests/test_ship_date_golden.py`, `tests/test_fulfillment_purity.py` |
| Pricing stays gated | injection / enumeration / cross-account / weak-confirmation probes | `tests/test_gateway_adversarial.py` |

Current suite: **556 passing, 25 skipped** (the skips are live-API smoke tests
that need real provider credentials — see *Status* below).

---

## Quick start

```bash
pip install -e ".[dev]"
pytest                              # full suite
python scripts/roundtrip_audit.py  # the never-invent / identity audit, ~1s
```

```python
from sku_translator import translate, FixtureCatalogIndex, InMemoryStore

catalog = FixtureCatalogIndex('data/catalog.csv', tenant_id='demo')
result = translate('5 inch chrome curved 24 long SB',
                   catalog=catalog, memory=InMemoryStore())
print(result.sku, result.source, result.confidence)
# → K5-24SBC construct high
```

---

## Repo map

| Path | What's there |
|---|---|
| `src/sku_translator/part_number_parser.py` | The grammar: 312 compiled patterns decoding canonical SKUs into structured fields |
| `src/sku_translator/normalizer.py` | Free-text / voice front-end: NATO-phonetic decoding, spoken fractions, units, family vocabulary |
| `src/sku_translator/translator.py` | Orchestrator: one `translate()` entry, confidence-graded resolution paths |
| `src/sku_translator/extractor.py` · `constructor.py` | tokens → spec → canonical SKU (invertible with the parser) |
| `src/resolution/` | Unified service: translator-first, BM25 candidate fallback (proposes, never resolves); every response carries state/source/confidence/flags |
| `src/fulfillment/` | Deterministic ship-date engine; every promise names the rule that produced it |
| `src/erp_harness/` · `src/erp_twin/` | Tenant onboarding: least-privilege discovery → human-gated profile → adapter + drift guard, against an in-repo ERP twin |
| `src/gateway/` | Chat/voice customer-service gateway: gates, HMAC sessions, PII-scrubbed journal, tool-calling connectors |
| `src/runtime/` | FastAPI app: chat API, Twilio voice endpoints, config-selected adapters |
| `scripts/roundtrip_audit.py` | The whole-catalog never-invent / identity verifier |
| `scripts/generate_catalog.py` · `generate_inventory.py` | Regenerate the synthetic catalog + inventory (see *Data*) |
| `docs/` | Architecture, decision log, specs, runbooks — indexed in `docs/README.md` |

---

## Data

The catalog in this repo is **synthetic**. It is generated by enumerating the
public part-number grammar (`scripts/generate_catalog.py`) — valid
family/diameter/length/finish combinations plus a set of deliberately-opaque
accessory codes — so it exercises the real engine end-to-end while containing
**no real company's part numbers, descriptions, prices, customers, or vendor
data**. Inventory quantities, prices, and sales figures are randomized (seeded,
so runs are reproducible). The grammar was originally developed against a real
industrial catalog under NDA; none of that data is included here.

## Status

This is a **work-sample / portfolio** project, not a deployed product. The
resolution core, fulfillment engine, onboarding harness, and gateway are
implemented and tested as described above. The provider integrations (LLM /
speech-to-text / text-to-speech / Twilio) are wired behind interfaces and have
live smoke tests, but those tests are **skipped by default** because they
require real API credentials. [`docs/MATURITY.md`](docs/MATURITY.md) gives the
honest per-capability map of what is fully tested vs. credential-gated vs. stub.
