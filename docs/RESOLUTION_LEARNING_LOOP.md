# Resolution learning loop — spec

**Status:** design, pre-build. The first stream of the observe→propose→gate→release
product, built on the SAFE (catalog-checkable) resolution stream. Behavioral-rule
learning is a later, harder-gated stage (§7).

**The pitch, made precise:** the agent observes real calls in the background, proposes
the rules it infers (how the rep resolves descriptions to parts), accumulates evidence
on each proposal against real calls, and a human RELEASES a rule into live behavior
only once it has cleared a measurable milestone repeatably and reliably. Autonomy of
observation + proposal; human-gated, mechanically-cleared release. The agent learns
freely in PROPOSAL-space (safe — proposals touch no call); the gate between proposal
and live behavior stays rigorous. **The milestone is the entire product; everything
else is plumbing.**

---

## 1. The loop

```
observe (shadow replay over real calls, §pilot)
  -> detect resolution divergence  (agent resolved P wrong/None; rep resolved S; S catalog-checkable)
  -> EXTRACT candidate rules        (a ladder of normalized keys P->S at increasing generality)   §3
  -> MEASURE each on HELD-OUT calls (calls the rule was NOT extracted from)                        §4
  -> GATE                           (support + reliability + no-regression, repeatable K-fold)      §4
  -> human RELEASES a cleared rule  (confirms mechanical clearance; does not judge)                 §5
  -> the released alias enters the live CorrectionStore; re-measure divergence -> the curve         §6
```

Released rules accumulate in the CorrectionStore (which already does ground-truth-gated
decaying aliases); this loop is the autonomous PROPOSER + the held-out RELEASE GATE in
front of it. The agent never edits live resolution on its own — a rule is a candidate
until it clears the gate and a human releases it.

---

## 2. What a resolution rule IS

A resolution alias: `key (normalized caller-phrase pattern) -> SKU`. Catalog-checkable:
"did the call this rule would fire on actually quote/order this SKU." The ground truth
is exogenous (the order), NOT a human judgment — which is why resolution is the stream
where the milestone is trustworthy and the loop can be proven first.

---

## 3. Extraction — a GRANULARITY LADDER, not one guess

A resolution divergence gives `(phrase P, rep's SKU S)`. The danger is proposing the
rule at the wrong grain: too specific overfits the one call and never matches another;
too broad clears the gate on average while being wrong in a subclass (the Goodhart
trap). We do NOT guess the grain — extraction emits the whole LADDER and the milestone
picks the winner:

```
P = "looking for a chrome stack for my 2019 Pete, the shiny 5 inch one"  -> S = K5-24SBC
ladder (most specific -> most general), each a candidate key -> S:
  L0  full normalized phrase            (matches ~only this call -> fails SUPPORT)
  L1  part-descriptive tokens           "chrome stack 5 inch"   <- the likely sweet spot
  L2  coarser                           "chrome stack"          (matches many SKUs -> fails RELIABILITY)
```

Normalization reuses the SKU-translator's existing tokenizer/normalizer (the same
machinery the CorrectionStore keys on), so the ladder is deterministic. **Granularity
is SELECTED by the milestone, not asserted:** release the MOST GENERAL key whose held-
out reliability still holds (max support without sacrificing correctness). Too-broad
rungs fail reliability; too-specific rungs fail support; the sweet spot is what's left.

---

## 4. The milestone — held-out, repeatable, subclass-aware, no-regression

A proposed rule is RELEASABLE iff, on calls it was NOT extracted from, it predicts the
rep's (catalog-true) SKU at threshold reliability, across repeated splits, without
breaking currently-correct resolutions. Each clause kills a specific failure:

| Clause | Definition | Failure it kills |
|---|---|---|
| **Held-out** | measured only on calls outside the extract-fold | self-training collapse — a rule "reliably right" on the calls it was learned from |
| **Support** ≥ S_min | # held-out calls whose phrase matches `key` | over-specific rules released on anecdote (one call) |
| **Reliability** ≥ R_min | of matched held-out calls, fraction where rep's SKU == `key`'s SKU (catalog-checked) | over-broad rules that conflate SKUs — a key matching two real parts can't be reliable for either |
| **Subclass-aware** | reliability measured ON THE MATCHED SUBCLASS, never aggregated across all calls | Goodhart — aggregate accuracy masking a wrong sub-segment |
| **No-regression** | held-out calls the baseline resolved correctly must STILL resolve correctly with the rule applied | a rule that helps its phrase but breaks a neighbor |
| **Repeatable** | all the above hold across K pre-registered folds, not one lucky split | a milestone passed by a fortunate partition |

K-fold cross-validation over the call corpus: extract on the extract-folds, measure on
the held-out fold, rotate. A candidate clears only if it holds across the folds. This
is the frozen-holdout + champion-challenger discipline from the eval, now governing
RULE RELEASE instead of code change. (Holdout calls are never inspected to TUNE a rule
— inspecting them to tune would re-contaminate the holdout, exactly as in the eval.)

---

## 5. Release — human confirms, does not judge

A milestone-cleared rule is a CHALLENGER that has already mechanically passed. The human
RELEASES it (confirms "this cleared the gate, apply it") — they do not eyeball "does
this rule look right," because a human judging rule-quality reintroduces the noisy-
evaluator problem. Confirming mechanical clearance is the appropriate human-in-the-loop;
judging is not. Released -> the alias becomes active in the live CorrectionStore.

---

## 6. The proof artifact (what you SHOW a prospect)

Not "watch it handle a call." The dashboard of a measured process:
- the rules the agent inferred from THEIR calls, each with its held-out reliability;
- each rule's reliability CURVE climbing toward its gate as more calls are observed;
- the rules that have CLEARED and await release;
- resolution-divergence-from-your-reps SHRINKING as released rules accumulate.

"Here's it learning your parts vocabulary from your calls, here's the curve, and nothing
goes live without clearing a measured bar and your release." **The gate is the sales
pitch** — the buyer's question is "can I trust it," and the answer is auditable.

---

## 7. Scope boundary (hold this)

Resolution rules ONLY in this loop — catalog-checkable, so the milestone has exogenous
ground truth and `right/wrong` is verifiable without the noisy human-comparison.
BEHAVIORAL rules (when to escalate/gate/sequence) are a SEPARATE, later, harder-gated
stage: their only ground truth is the human-comparison, which carries the rep-self-
comparison bias (pilot §2), so their milestone is weaker and release more conservative.
Do not let behavioral-rule learning ride in on the resolution loop's proof.

---

## 8. Build order (gate-first, same discipline as the disclosure gate)

1. **The milestone as a PURE FUNCTION** (`releasable(rule, holdout_calls) -> verdict`),
   unit-tested in isolation FIRST — support/reliability/subclass/no-regression/K-fold.
   It is the gate; build it provable before the loop wires around it.
   - demonstrate-red: an OVERFIT rule (clears on its extract-set) FAILS on holdout; a
     TOO-BROAD rule FAILS subclass-reliability; a regressing rule FAILS no-regression.
2. Extraction (divergence -> the key ladder).
3. The K-fold harness (extract/measure/rotate over a corpus) + granularity selection.
4. Release (human-confirm of a cleared challenger -> CorrectionStore).
5. The divergence-shrinkage curve (the proof artifact) over a corpus.

The milestone is the part that must be boring and provable; the proposer may be
agentic. Build the milestone first so the proposer has a gate it cannot game.

---

# REVISION (post-codebase reconciliation) — integrates the dispatch spec

The dispatch's architecture (three actors: agent proposes/triggers, system evals,
human releases) and invariants are PRESERVED. What changes: most of this is already
in the codebase, so the work is HARDEN + WIRE + add the PROPOSER, not build-parallel.

## R1. What already exists (do not duplicate)

| Piece | Where | What it already does | Gap vs this spec |
|---|---|---|---|
| **Loop state machine + propose-not-commit** | `gateway/alias_store.py` (CorrectionStore) | `Alias` = phrase→target_sku ONLY (no fact fields — a poisoned alias has nowhere to put a fake price); `PROPOSED` is inert; `W_HIT=0` (usage≠evidence); `may_promote` requires an exogenous label **AND** `eval_no_regress`; decay/stale/retire/contested; `resolution_mode` (auto_silent strong-tier-gated / auto_confirm / disambiguate) | the eval behind `eval_no_regress` is a single BOOLEAN — no held-out-accuracy gate, no subclass-stratification, no fault-injection check |
| **Held-out ISOLATION discipline (§8)** | `runtime/agent_eval.py` | three-bucket dev/frozen_visible/frozen_holdout/reserve; `verify_frozen` (sha256 hash-lock = the ENFORCED freeze); `burn_holdout_case` (burn-on-inspect + reserve rotation + loud exhaustion); PASS/FAIL/NOT-EXERCISED | content is BEHAVIORAL scenarios, not resolution (phrase→SKU) cases — the resolution battery REUSES the isolation, supplies new content |
| **Observation + resolution-divergence** | `pilot/shadow.py` (just built) | replay+re-ground, per-stream divergence, catalog-checkable resolution divergence, autonomous-vs-conditional tags | nothing extracts a RULE from a divergence yet |
| **Live alias SEAM** | `resolution/service.py:182` | `learned_aliases.alias_for(text)` → `RESOLVED source='learned_alias'` | **the seam is UNWIRED (`learned_aliases=None` default); the CorrectionStore is NOT connected to it — grep confirms zero live consumers of alias_store** |

**Invariants 3 & 4a are already enforced in types** (`may_promote`: held-out-blind
eval + exogenous label + no-regress). Invariant 4b (HUMAN release) is NOT a distinct
step — `promote()` is mechanical; ADD a human-confirm-of-cleared-challenger gate
before `promote()`. Invariant 6 (reversible) is present (`on_contradicting_correction`
→ RETIRED; live-lookup-wins) — make the retract path explicit at the live seam.

## R2. The actual work (what's missing)

1. **EXTRACTION** (proposer): shadow resolution-divergence → candidate alias via the
   granularity LADDER (§3) → `alias_store.propose()`. The ladder = normalized-key
   variants; the live seam `alias_for(text)` normalizes the same way to match.
2. **The resolution EVAL BATTERY** — harden the `eval_no_regress` boolean into the
   §4 three-component verdict (held-out accuracy 4.1 + no-regression 4.2 +
   subclass-stratified 4.3), REUSING `agent_eval`'s frozen/hash-lock isolation for a
   RESOLUTION held-out bucket. `may_promote` consumes the battery verdict, not a bare
   bool. **Build this FIRST (dispatch §3), prove its fault-injection check.**
3. **TRIGGER governor** (§5): min-evidence pre-filter + backoff — maps onto the
   alias's observed-support count + `exogenous_labels`.
4. **LIVE WIRING + human release**: a CorrectionStore-backed `alias_for(text)`
   (returns only `resolution_mode==auto_*` aliases) plugged into the resolver seam;
   a human-confirm step gating `promote()`. THIS is the grep-on-live-path proof: an
   ACTIVE alias changes a real `ResolutionService` resolution (`source='learned_alias'`).
5. **The held-out resolution SET** built from EXOGENOUS ground truth (orders /
   rep-labels), hash-locked — the self-laundering guard (R4).

## R3. Fault-injection check plan (dispatch §4.4 — the core, built FIRST)

The battery is unproven until a known-bad rule of each class is shown to FAIL it.
Three injected-bad-rule tests, each red BEFORE the loop is wired:
- **overfit** — an alias correct on its extract-set, wrong on the held-out set →
  must FAIL 4.1 (held-out accuracy below threshold). (red proves 4.1 isn't measuring
  the extract-set.)
- **regressing** — an alias that clears 4.1 but, added to the released set, breaks a
  currently-correct held-out resolution (its key shadows a working one) → must FAIL
  4.2.
- **subclass-failing** — an alias 95% on aggregate held-out but 0% on an identifiable
  subclass (right for family K calls, wrong for the BH calls its key also matches) →
  must FAIL 4.3 even though aggregate clears. (red proves the Goodhart guard bites.)
Deliverable: the three reds, then the battery green on a good rule. green +
grep-on-live-path (battery feeds `may_promote` feeds the live seam) + demonstrated-red.

## R4. Held-out isolation + self-laundering (dispatch §8)

REUSE `agent_eval`'s mechanism: the resolution held-out set is a FROZEN bucket,
hash-locked by `verify_frozen` (the agent reads dev/observation, never holdout, never
writes any frozen file — the loop CI-checks `verify_frozen()==[]`). The trigger says
"test me"; the system picks the held-out cases (invariant 2) — the agent can't select
them because it can't read or write the locked bucket. **Self-laundering guard:** the
held-out (phrase→true-SKU) cases are built from EXOGENOUS labels (an order placed and
not returned / a rep label on that mapping), NEVER from the agent's own resolutions —
so the agent cannot seed the exam it's graded against. (The agent's prior outputs
contaminating the holdout is the resolution-stream form of the self-training-collapse
the whole architecture refuses.)

## R5. §9 open-choice answers (from the code)

1. **Rule representation** — REUSE `alias_store.Alias` (phrase→target_sku). The
   ladder produces candidate `phrase` normalizations; `propose()` already exists.
2. **Subclasses (§4.3)** — by **product family** of the true SKU (K/BH/BR/A/WCK/SS/SP/
   D/S/L/R/PG — `extractor.py:145`), plus **named-vs-described** (did the caller emit a
   SKU-shape or a description), plus **ambiguity level** (how many catalog candidates
   the phrase matches). Family is the primary stratum (it's where a too-broad key
   conflates).
3. **Pre-filter / backoff** — pre-filter: alias held across ≥ **5** observed
   supporting calls before it may trigger; backoff: a failed rule needs ≥ **2×** its
   prior support (or +10, whichever larger) before re-trigger. Tunable starting points.
4. **Held-out selection** — R4 (frozen bucket + hash-lock + exogenous-only cases).
5. **CorrectionStore already IS this loop?** — its STATE MACHINE + propose/commit
   boundary, YES. Its eval gate is a stub boolean; its live wiring is absent. HARDEN +
   WIRE, don't rebuild.
6. **Release/retract** — release: human confirms a battery-cleared challenger →
   `promote()` → ACTIVE → visible at the `alias_for` seam. Retract: `on_contradicting_
   correction()`/repeated failed-live-lookup → RETIRED → drops out of `alias_for` next
   resolution (live-lookup-wins; reversible, fail-closed).

## R6. One invariant refinement (not a challenge)

No invariant should be dissolved — each prevents a named failure and the codebase
already encodes 1/2/3/4a/6. The refinement: invariant 4b (human release) must become a
DISTINCT enforced step. Today `promote()` flips state with no human gate; a
battery-cleared challenger should sit in an `awaiting_release` state until a human
confirms, so "the human releases on a mechanically-cleared eval" is enforced in code,
not convention.

---

# REVISION 2 — order/quote cross-reference as the ground-truth backbone

The dated order/quote records (the ERP's quote + order-entry history) are cross-
referenced against the call transcripts. This is BOTH an eventual target (the agent
will quote / enter orders) AND, in the meantime, the most powerful learning resource:
what the caller SAID (their vernacular) cross-referenced with what was actually
quoted/ordered (the catalog-true SKU). It is the exogenous backbone that makes the
whole loop work at scale rather than on sparse rep labels.

## X1. What the cross-reference produces

A `(transcript, dated order/quote)` pair yields `(caller phrase P, SKU S, tier)` —
the strongest possible resolution label, because S is what the customer actually
bought and kept, not a judgment. It feeds THREE consumers already in the spec:
- **Extraction** (a second, stronger source than shadow-divergence-vs-rep): P→S
  straight from the order, through the granularity ladder → `propose()`.
- **The held-out set** (R4): built from orders, NOT agent outputs — exactly the
  self-laundering-safe exogenous ground truth invariant 2/3 require, now available in
  volume from the order history.
- **The confirm signal**: `order_not_returned` (the gold tier, already in
  `alias_store`) on the specific mapping.

And across many pairs it produces the real asset: a **lexicon** — the many vernaculars,
slang, trade terms, regional phrasings, and accent-transcription patterns that map to
one SKU. The granularity ladder (§3) generalizing across those phrasings IS the
lexicon-learning; the catalog families (§4.3) are the structure it organizes around.

## X2. The order LIFECYCLE tiers the label (dated records enable this)

Not every cross-reference is gold. The order lifecycle, read off the dates, sets the
tier — mapping onto `alias_store`'s existing `w_confirm`:
- **quote only** → the rep resolved the description to S in the quote: REP-tier (0.25).
  Strong, but the customer may not have bought it / may have been mis-quoted.
- **order placed** → stronger, but inside the return window it isn't gold yet.
- **order placed AND past the return window (not returned)** → GOLD (0.40). Reality
  kept the part. **The dated records are what let the label MATURE** from placed →
  not-returned. Held-out gold labels use matured orders only.

## X3. THE SHARP CAUTION — a mis-attributed order is a confidently-wrong GOLD label

This is the critical risk and it inverts the usual one. The cross-reference is an
ATTRIBUTION problem: match a transcript to its order by account + tight timestamp +
line-item-to-utterance. It can be wrong — a call that produced no order, an order from
a different channel, a multi-line order whose lines don't cleanly attribute to
utterances. A wrong attribution creates a phrase→SKU mapping with GOLD weight (the
highest), and — worse — it can land in the HELD-OUT SET, corrupting the verifier
itself. "An unverified verifier is the worst object in the system"; a held-out set
seeded with mis-attributed gold labels is exactly that.

So: **attribution confidence is itself a gate. "It came from an order" does NOT mean
"it's gold."** Only HIGH-confidence matches (single unambiguous line attribution +
account match + tight time window) become gold labels / held-out cases. Ambiguous
attributions (multi-line without clean per-utterance mapping, loose window, account
mismatch) are EXCLUDED from the held-out set, or demoted to rep-tier and routed to
rep-adjudication — never silently trusted as gold. Same pattern as everything else: a
cross-reference is a CANDIDATE gold label until its attribution clears a confidence
bar. The eval's ground truth is only as trustworthy as this gate, so this gate gets
the same fault-injection check treatment: a deliberately mis-attributed order must be
shown to be REJECTED (not admitted as gold).

## X4. Surfaces this opens

- **PII / commercial confidentiality grows** — tying a person's spoken words to their
  purchase history is a larger, more sensitive surface than transcripts alone. The
  scrub-at-ingestion (pilot capture) extends to order/quote records (account, name,
  price, history); the pilot agreement / compliance must cover purchase-data use.
- **ERP dependency** — the order/quote feed is ERP data, reached via the existing ERP
  adapter harness (`src/erp_harness/`), not yet live. Design + build the cross-
  reference against the synthetic BC twin now (so the data model + the attribution-
  confidence gate exist), wire the real feed when the ERP integration lands. PRODUCTION
  VALIDATION GATE item: real-order attribution accuracy is unknowable until measured
  against a real order feed (the attribution heuristics will misfire in ways the twin
  won't show) — fault-injection check on real mis-attribution before gold labels from
  it are trusted.
- **Forward-compatibility** — model order/quote records as both INPUT (learning) and
  eventual OUTPUT (the quoting / order-entry agent), so this cross-reference substrate
  is what the future quoting capability is built on, not a throwaway.

## X5. Net effect on the build

No invariant changes; this ADDS the strongest exogenous source and the at-scale
held-out corpus builder, all consistent with `alias_store`'s existing tiers. New work:
a `cross_reference` layer (transcript × dated order/quote → tiered P→S labels) with
its own ATTRIBUTION-CONFIDENCE gate + fault-injection check, feeding extraction +
held-out + confirm. Built against the ERP twin; gated on real-feed validation.

---

# REVISION 3 — battery built; the live-wiring reconciliation (a Section-7 question)

## What's built + proven this pass

- **The eval battery** (`learning/eval_battery.py`): the §4 three-component gate
  (held-out accuracy / no-regression / subclass-stratified) as a pure function.
  Fault-injection check GREEN (`tests/test_eval_battery.py`): overfit→fails 4.1,
  regressing→passes 4.1 fails 4.2, **subclass-failing→DELTA (passes the 2-component
  battery, fails the 3-component)** proving 4.3 is critical.
- **"Into" `may_promote`** (`alias_store.py`): `eval_no_regress: bool` → battery
  `verdict` (duck-typed `.passed`); ONE gate, the rigorous one. Legacy 15 transition
  tests preserved via `Verdict.injected_pass()/injected_fail()` — VISIBLE FIXTURES,
  not a battery stub; the battery's correctness is proven separately by the reds.
- **`awaiting_release` state + `release()`** (invariant 4b structural): clearing the
  battery STAGES (awaiting_release, NOT live); only an explicit human `release()`
  reaches ACTIVE — "human releases on a mechanically-cleared eval" enforced in code.

## THE LIVE-WIRING FINDING (sign-off needed before building)

There are TWO alias stores, and the LIVE one is the WEAK one:
- `gateway/alias_store.py` — the rigorous lifecycle (this loop). **NOT wired.**
- `gateway/shadow.py::CorrectionStore` — an UNGATED dict (`add_alias(phrase,sku)` →
  immediately served by `alias_for`). **WIRED live** (`runtime/config.py:78`), and the
  resolver seam (`service.py:186`) gives it `confidence='high'` → **silent** resolution.

So the live alias path today is "a human adds → instantly live, silently" — the
APPROVAL-IS-THE-COMMIT hole `alias_store` was built to close, AND it violates
confirm-on-alias (silent, not readback). "Wire the rigorous store live" therefore
means REPLACING a live, tested component (`shadow.py CorrectionStore`, used by
test_runtime / test_e2e_holistic / test_shadow + the live config), not a fresh plug-in.
That is a Section-7 reconciliation decision, surfaced not built.

**The two grep conditions, against this finding:**
- *(a) fact-gate not bypassed* — BOTH stores are phrase→SKU only (never-invent,
  `is_canonical`), so a learned alias changes WHICH part; facts come live. Structurally
  satisfied for either store. ✓
- *(b) confirmation not bypassed* — the CURRENT live wiring VIOLATES it (silent
  `confidence='high'`). The rigorous store's `resolution_mode` fixes it: `auto_confirm`
  (default) → seam returns `confidence='medium'` → `identify()` requires a readback;
  only strong-tier `auto_silent` → `'high'` → silent. So the seam fix (mode-aware
  confidence) is part of the replacement.

**Proposed reconciliation (for sign-off):** make a `CorrectionStoreProvider` (backed
by `alias_store` Alias objects, exposing ONLY ACTIVE aliases + their mode) the
resolver's `learned_aliases`, and change the seam to set confidence per mode
(auto_silent→high, auto_confirm→medium-with-readback). Migrate the shadow.py
CorrectionStore's human-SME `add_alias` to `propose()` (so SME corrections become
candidates that still clear the battery + human release, not instant-live), preserving
the existing tests' behavior where the alias is genuinely confirmed. Then the
grep-on-live-path proof: an ACTIVE alias makes `ResolutionService` return
`source='learned_alias'`, through the live fact path, via a readback (not silent).
This replacement touches live tested code, so it gets the same review-before-flip as
every prior live-path change — hence: sign-off, then build.

---

# REVISION 4 — the SME-path migration (signed off: close the ungated path)

Decision: migrate ALL correction input through the gate; close the ungated path
entirely; one gate, no exceptions. The SME's expertise shows up as label WEIGHT
(rep-label tier), not as a bypass.

## Confidence-flow insight (shapes the migration, not a fork)

`propose c0=0.30 + rep_label 0.25 = 0.55 < auto_resolve 0.70` — a single SME
correction CANNOT clear the gate alone (by design: no single approval is the commit).
The second signal is the ORDER CROSS-REFERENCE (REV 2): the resulting order confirms
`order_not_returned +0.40 → 0.95`, and SME-expertise + reality TOGETHER clear it. The
SME path is fast because its label is strong, not because it's exempt.

## Exact migration (every site)

1. **`gateway/shadow.py::CorrectionStore`** — back it with `alias_store.Alias`:
   - REMOVE `add_alias` (the instant-live path). New `propose_correction(phrase, sku,
     *, source, now)` → `propose()` + `on_confirm(source)` -> PROPOSED, NOT live.
   - `confirm(phrase, source, now)`, `clear_for_release(phrase, *, verdict)`
     (-> `may_promote` -> `stage_for_release`), `release(phrase)` (human -> ACTIVE).
   - `alias_for(text) -> (sku, mode) | None` — **ACTIVE-ONLY** (`a.state == ACTIVE`,
     NOT `!= RETIRED`), matched, returns `resolution_mode`. awaiting_release/proposed
     resolve NOTHING (invariant 4b at the seam).
   - Persist Alias state (not just phrase->sku).
2. **`resolution/service.py` seam** — `hit = alias_for(text); sku, mode = hit`;
   `confidence = 'high' if mode=='auto_silent' else 'medium'`; `needs_review = mode !=
   'auto_silent'`. So auto_confirm -> `identify()` readback (the confirm-on-alias FIX);
   only strong-tier auto_silent resolves silently.
3. **`ContinuousImprovement` (3 sites: 256, 439, 504)** — `add_alias` -> 
   `propose_correction`. The autonomous 'rep_said_sku' auto-apply becomes auto-PROPOSE
   (a candidate, NOT live) — which finally matches the subsystem's own stated intent
   ("the agent does not change its own behavior unattended", shadow.py:453). `apply_
   review` (SME) -> propose_correction with the SME's strong label.
4. **`ShadowObserver` (221)** — `hit = alias_for(text); if hit: sku, mode = hit`.
5. **`runtime/config.py`** — unchanged import; new gated semantics flow through.
6. **Tests (test_shadow ~10, test_runtime, test_e2e_holistic)** — re-pin to the GATED
   behavior: a correction is PROPOSED (not live); after the second exogenous signal +
   battery + human release it is ACTIVE and resolves VIA READBACK (auto_confirm), not
   silent. Update the assertions to the correct behavior; NO test-only instant-add
   backdoor.

## The grep proofs that close it (the deliverable)

- POSITIVE: an ACTIVE auto_confirm alias makes `ResolutionService` return
  `source='learned_alias'` at `confidence='medium'` -> `identify()` READBACK (not
  silent), through the live fact path.
- NEGATIVE: an `awaiting_release` alias is NOT returned by `alias_for`, never reaches
  `learned_alias`.
- DELTA (confirm-on-alias fix): the new wiring produces a readback where the OLD
  wiring resolved silently — same delta-shape as the subclass red.
- GONE: `grep add_alias` returns no instant-live caller and no test backdoor — the
  ungated path is removed, not dormant.

## Status

Battery + may_promote-into + awaiting_release: DONE, proven (553 green). This
migration is the dedicated next build; the ungated path is NOT yet closed until it
lands. Executed as one reviewed change, test-driven, with the four grep proofs as its
gate.
