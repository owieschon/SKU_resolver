# Conversation State & Disclosure Spec — Parts Voice Agent

**Status:** design, pre-wire. Supersedes the fixed-sequence backend (`"what part?" → say part → availability+inventory → require account → price`).
**Scope:** the conversation-state model and the disclosure gate the orchestration layer wires against. Containment (substitute-say, allowlist, fail-closed) is unchanged and assumed; this spec only formalizes the layer that was missing — caller-led sequencing over multiple parts — and the deterministic gate that keeps facts safe regardless of conversational path.

---

## 1. The architectural seam

Three layers, with a hard line between what the model decides and what the gateway decides.

- **Orchestration (LLM, agentic, forgiving).** Owns the conversation: tracks what's established and what's missing, resolves which part the caller is referring to, chooses the next move, decides when to read facts, runs the not-done-until-they-say-so loop. A wrong move here is cheap and self-correcting (asks a redundant question), *because the gate below cannot be talked around.*
- **Gate (gateway, deterministic, absolute).** Decides whether a given fact about a given part may be disclosed right now. Two-part check: precondition met AND read fresh. The LLM advances the conversation toward a satisfied gate; it never satisfies the gate.
- **Say (gateway, verbatim, guarded).** Renders disclosed facts into speech, word-for-word from the gateway, never authored by the model. Per-part-explicit, boolean availability, internal-state-guarded.

The seam maps onto the temporal structure of the data: **durable identity** (which parts, which account) is gathered by the LLM in any order; **perishable facts** (availability / lead time / price, each a time-stamped child of a part) are read by the gateway, fresh and together, at the moment of disclosure.

---

## 2. State model

### 2.1 ConversationState (top-level, one per call)

| field | type | durability | notes |
|---|---|---|---|
| `account` | `AccountState` | durable for call | shared across all parts; the price index |
| `parts` | `dict[part_ctx_id, PartContext]` | durable for call | the set of parts in play |
| `focus` | `part_ctx_id \| None` | volatile | which part the caller is currently referring to; LLM-maintained |
| `caller_intent_complete` | `bool` | — | starts `False`; only an affirmative caller signal sets it `True`; a disclosure never sets it |

`AccountState`: `unknown` | `established(account_id)`. Durable because the account is a property of the caller, not the part. Established once, indexes every part's price.

### 2.2 PartContext (one per distinct part the caller raises)

A part number is an **identity**, not a record. Each PartContext carries durable identity plus a set of perishable fact-children.

| field | type | durability | notes |
|---|---|---|---|
| `ctx_id` | id | — | stable handle for focus/anaphora |
| `identity` | `IdentityState` | durable for call | identity doesn't change mid-call |
| `caller_reference` | str | durable | how the caller named it ("the big chrome stack") — for read-back and disambiguation |
| `availability` | `Fact[bool]` | perishable | boolean: available / not (lead-time-only). NEVER on-hand quantity |
| `lead_time` | `Fact[value]` | perishable | |
| `price` | `Fact[value]` | perishable | indexed by `account_id`; undefined without it |

`IdentityState`: `unknown` | `ambiguous(candidates)` | `identified(sku)`.

### 2.3 Fact[T] (temporal child of a PartContext)

A fact without an `as_of` is not a fact, it's a rumor. Every binding fact carries when it was read.

| field | type | notes |
|---|---|---|
| `state` | `unread` \| `read` \| `unreadable` | `unreadable` = no source wired (see §8) |
| `value` | `T \| None` | only meaningful when `read` |
| `as_of` | timestamp \| None | the read time; part of the fact's identity |
| `account_id` | id \| None | **price only**; the index the price is valid for |

---

## 3. The disclosure gate (deterministic core)

A fact may be spoken iff **both** hold:

```
discloseable(part, fact_type, now) :=
      precondition_met(part, fact_type)          # state check
  AND fresh(part.fact[fact_type], now)           # timestamp check
```

Neither part is the LLM's call. Both are evaluated at the gateway at disclosure time.

### 3.1 Preconditions (per fact type)

```
precondition_met(part, AVAILABILITY) := part.identity is identified
precondition_met(part, LEAD_TIME)    := part.identity is identified
precondition_met(part, PRICE)        := part.identity is identified
                                        AND account is established
```

The account component of the price precondition is not a bolted-on verification step. **Price is a property of the `(part, account)` pair** — in B2B it literally does not exist until the account is known, because different accounts price at different levels. So an unaccounted price is not "blocked," it is *undefined*: there is no price tuple to construct.

### 3.2 Freshness

```
fresh(fact, now) := fact.state == read
                    AND (now - fact.as_of) <= HORIZON[fact_type]
                    AND (fact_type != PRICE OR fact.account_id == account.id)
```

`HORIZON` is a per-fact-type parameter, defaulted conservative (short = re-read often), tuned against the customer's real data velocity:

| fact | default horizon | rationale |
|---|---|---|
| `availability` | short | stock moves fastest |
| `lead_time` | medium | changes on restock cadence |
| `price` | longer-but-account-scoped | account pricing stable day-to-day; invalid if account differs |

These are guesses until the customer tells you their velocity. Ship parameterized; do not hard-code.

### 3.3 The multi-part × account interaction (hold this line)

The account is **shared and durable**, so establishing it once satisfies the account-component of the price precondition for *every* part under that account. The caller never re-establishes their account per part.

But identity and fresh-read are **per-part**. Therefore:

```
price(part_i) discloseable iff
      part_i.identity is identified        # per-part, NOT inherited
  AND account is established                # shared
  AND fresh read for (part_i, account)      # per-part read, account-scoped
```

The dangerous failure to forbid: an **unidentified** part inheriting disclosability from an identified sibling because the shared account is established. The account is shared; identity and the read are not. An identified part B and an ambiguous part C under the same established account have different gate outcomes for price — B may disclose, C must not.

---

## 4. Freshness & bundle coherence

Preconditions **accumulate** across the conversation in any caller-led order. Facts are **read fresh and together at disclosure time**, not accumulated across turns.

- Within a part: read its requested facts together so they share an `as_of`.
- Across parts (the common "are both available, what's the total" case): read the in-scope parts' facts together at the moment of bundled disclosure, so the bundle shares an `as_of` across parts and never mixes a minute-zero availability with a minute-three price.
- Cached reads are legal but horizon-bounded: if a caller circles back to a fact read earlier and it is still within horizon, the cached read is a valid fact; if past horizon, re-read before speaking.

Rule of thumb: **gather durable state conversationally (any order); read perishable facts together at the instant of disclosure.**

---

## 5. Orchestration layer (the LLM's role)

The LLM reads each caller utterance, updates durable state, maintains `focus`, and selects the next move. It owns ambiguity; it owns sequencing; it owns reference resolution. It does **not** decide disclosure.

### 5.1 State updates per utterance
- Did the caller give/raise an **account**? → update `account`.
- Did the caller raise a **new part**? → create a new `PartContext` (often via the "anything else?" doorway, §6).
- Did the caller refer to an **existing part** ("it", "that one", "the other one", "the K5")? → resolve against `parts`, set `focus`. If the reference is ambiguous among established parts → **disambiguate-which-part** (same contested→ask discipline as SKU resolution).

### 5.2 Moves (chosen against the in-scope part's unmet needs)
- `identify_part` — if focus part identity is `unknown`/`ambiguous` (ask for or disambiguate the part).
- `establish_account` — if a price is wanted and `account` is `unknown`.
- `disambiguate_which_part` — if the caller's reference is ambiguous among ≥2 established parts.
- `read_and_disclose(fact_set)` — when target facts' preconditions are met; gateway reads fresh+together and the say speaks them. (The gate still runs; orchestration proposing disclosure does not authorize it.)
- `anything_else` — after any disclosure (§6).
- `close` — only on an affirmative completion signal (§6).

### 5.3 Focus & anaphora
`focus` is volatile and updated every turn. Early-call references are usually unambiguous; deep-call references degrade. When confidence in the referent is low, the move is `disambiguate_which_part`, not a guess. Cost of asking is trivial; cost of resolving to the wrong part is a wrong quote.

---

## 6. Closure loop (not-done-until-they-say-so)

A successful disclosure is **not** an end state. The fixed backend's assumption that a successful tool call ends the interaction is wrong for the same reason fixed question-order was wrong: it imposes a deterministic shape on a caller-driven process.

```
disclosure  →  anything_else ("anything else I can help with?")
                   ├─ caller raises new intent  →  new/updated PartContext, back to gather/disclose
                   └─ caller signals complete    →  caller_intent_complete = True  →  close
```

- `caller_intent_complete` starts `False` and is set `True` **only** by an affirmative caller signal ("no, that's it"). A disclosure never sets it.
- The multi-part doorway: a second (third, …) part most often enters through `anything_else` after the first is handled. Closure and multi-part are the same loop.
- Symmetric discipline (do not over-correct into clinginess): on a clear completion signal, **close immediately and gracefully**. Do not re-prompt, do not ask twice, do not fish for more. Not-done-until-they-say-so **and** done-the-moment-they-say-so.

---

## 7. The say (verbatim, guarded)

- **Per-part-explicit on multi-part.** A multi-part availability disclosure names each part with its own status. Never aggregate into a summary that loses which part is which ("those are mostly available" is forbidden — the partial case is exactly where a vague answer becomes a broken commitment). Render: part A status, part B status, each explicit.
- **Boolean availability, never quantity.** "Available" / "not in stock, lead time X." The on-hand count is internal state and must be caught by the say-guard the same way BM25 scores and taxonomy codes are.
- **Internal-state guard extension:** add on-hand-quantity-shaped disclosures to `assert_no_internal_state`.
- **Verbatim:** the say is the gateway's, not the model's. Multi-part bundles read together (§4) so the spoken bundle is temporally coherent.

---

## 8. Pricing-not-wired interim (and B2C note)

Per-customer pricing is not available yet. Model this clearly, not as a special case:

- `price.state = unreadable` (no source). Its precondition can never be satisfied because the fact has no readable source.
- The agent's correct move on a price request is the **can't-quote handoff** (escalate to rep / quote system), surfaced as a coherent service action.
- This flips on cleanly when per-customer pricing is wired: the source appears, `price` becomes `read`-able when account-indexed, and the existing precondition (`identified AND account established`) starts being satisfiable. No code path changes — only the fact's source.
- **B2C collapses this:** if price is a property of the part alone (no account index), the account-component of the price precondition disappears and the gate simplifies to `identified AND fresh`. Note which pilot type you're targeting; it determines whether the account-precondition exists at all.

---

## 9. Invariants (must always hold, regardless of conversational path)

1. The model never authors a binding fact. Facts come from the gateway; the say is verbatim. (Containment, unchanged.)
2. A fact is spoken only if `discloseable()` returns true at speak time — precondition met AND fresh. The LLM cannot satisfy either.
3. Price requires the part's **own** identification; it is never inherited from a sibling part under the shared account.
4. No binding fact is spoken without an `as_of` within its horizon.
5. On-hand quantity is never spoken.
6. A multi-part availability disclosure is per-part-explicit.
7. A disclosure never sets `caller_intent_complete`. Only the caller does.
8. A completion signal closes immediately; the agent does not re-fish.

---

## 10. Adversarial test obligations (prove the loosening is safe)

The orchestration layer is new agency. It is safe only if the gate holds against any conversational path the LLM can take. Each of these must fail closed regardless of how the LLM steered the conversation:

- **Price without account.** LLM tries to disclose price for an identified part while `account` is `unknown` → precondition fail.
- **Inherited disclosability.** Account established, part B identified; LLM tries to disclose price for part C which is still `ambiguous` → fail on C's identity precondition (invariant 3). This is the multi-part × account attack specifically.
- **Stale read.** LLM tries to re-speak a fact read earlier in the call, now past horizon → freshness fail (must re-read).
- **Incoherent bundle.** LLM tries to speak a bundle assembled from reads at different times → must read together at disclosure.
- **Quantity leak.** Availability disclosure attempts to include on-hand count → say-guard blocks.
- **Aggregated multi-part.** Multi-part availability rendered as a vague summary losing per-part status → forbidden; assert per-part-explicit.
- **Premature close.** Agent ends on a disclosure without `anything_else`, or fails to close on a clear completion signal → both are failures.
- **Wrong-referent quote.** Ambiguous deep-call reference resolved by guess rather than `disambiguate_which_part`, producing a quote against the wrong part → the orchestration must disambiguate, not guess.

Each obligation is the deterministic twin of a conversational freedom granted in §5–§6: every place the LLM gained latitude, there is a gate or a guard that an adversarial orchestration must not be able to talk around.

---

## 11. Build order (suggested)

1. State objects (§2) — `ConversationState`, `PartContext`, `Fact[T]`.
2. The gate (§3) as a pure function, with the per-part × shared-account semantics and the two-part predicate. Unit-test invariants 2–4 first.
3. The say extensions (§7) — per-part-explicit rendering + quantity in the internal-state guard.
4. Orchestration moves (§5) against the state + gate, with focus/anaphora and the closure loop (§6).
5. The pricing-unreadable interim (§8) so price requests handoff coherently now.
6. The §10 adversarial suite against the wired orchestration + unchanged gate.

The gate is the part that must be boring and provable. The orchestration is the part that may be agentic and forgiving. Build the gate first so the orchestration has something it cannot talk around.
