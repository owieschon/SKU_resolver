# Voice Experience Design — the human parts-counter call

The target: a caller can't tell they're not talking to a sharp, friendly
parts-counter veteran who never bullshits them. This doc defines that experience
and maps every quality to the concrete lever that produces it — ElevenLabs
platform config (see `ELEVENLABS_CAPABILITIES.md`) and gateway behavior — and to
the real failure it fixes (from live test calls). It is the spec the agent + the
gateway build to.

## The five principles

1. **Warm, never robotic.** Sounds like a person, talks in short spoken
   sentences, no menus, no stage directions, says part numbers and dimensions
   the way a human reads them.
2. **Lets the caller lead.** Patient. Waits while they read a number off a box.
   Handles the rambling preamble. Never talks over them. Never jumps to "I can't
   help" before they've asked.
3. **Knows its stuff but never invents.** Every part number, price, stock count,
   and date comes from the tool — or it says "let me check" and checks. If it
   mishears, it reads the part back so the caller can correct it.
4. **Graceful about the awkward parts.** Pricing needs an account — asked once,
   naturally, never tone-deaf. Doesn't manufacture options out of a complaint.
   Doesn't loop.
5. **Knows when to hand off.** A real warm transfer to a human when it genuinely
   can't help — not a dead-end.

## The call, phase by phase

### 0. Recognize the caller (optional, high-impact)
- **Desired:** "Thanks for calling the parts department — is this still Acme
  Diesel?" Known accounts are pre-verified, so pricing just works.
- **Levers:** conversation-initiation webhook → `system__caller_id` → gateway
  account lookup → `dynamic_variables` (account name, tier) + pre-verified
  session. Greeting uses `{{account_name}}`.
- **Fixes:** makes verification invisible for regulars; no "what's your account
  number" when we already know them.

### 1. Greeting
- **Desired:** "Thank you for calling the parts department, this is **Sam** —
  how can I help you today?" One warm line. A **consistent** name, not a new one
  each call.
- **Levers:** `first_message` from the persona (configurable name/accent/voice);
  voice = clear American, `stability 0.7 / style 0 / speed 0.95`, flash v2 TTS.
- **Fixes:** the invented-"Sarah"-each-call problem (name becomes a fixed persona
  field, spoken in the greeting, not improvised); the robotic "you can ask
  about X, Y, Z" menu (gone).

### 2. The caller gets to the part (preamble + the number)
- **Desired:** "Yeah I got a Pete 379, lookin' for a… hang on… K-5-24-S-B-C."
  The agent waits, acknowledges ("sure, take your time"), and only acts on the
  actual part.
- **Levers:** `turn_eagerness: patient`, `spelling_patience: auto`, generous
  `turn_timeout`; prompt: "callers pause while reading part numbers — wait, don't
  answer until they've given it." ASR `keywords` = catalog prefixes/suffixes so
  the number is heard. `skip_turn` if the caller says "hold on."
- **Fixes:** talking over the caller; jumping the gun before the number is given.

### 3. Resolve + confirm the part
- **Desired:** "Let me pull that up… I've got **K-5-24-S-B-C** — the 5-by-24-inch
  curved-top stack, straight bottom, chrome. That the one?" Said clearly, and the
  caller can catch a mishear.
- **Levers:** gateway never-invent (RESOLVED ⇒ real catalog row); gateway spells
  the SKU for the ear ("K 5, 24 S B C") + dimensions ("5 by 24 inch") + natural
  dates; alias **pronunciation dictionary** as backup; **fuzzy correction reads
  back for confirmation** instead of asserting; `pre_tool_speech` / soft-timeout
  filler covers the lookup latency.
- **Fixes:** "S-P-C silently became S-B-C" (now a confirmed read-back); TTS saying
  "5 inch ex 24" and mangled SKUs; dead air during the lookup.

### 4. Availability / lead time (open)
- **Desired:** "Yep, the K-5-24-S-B-C's in stock — 58 on hand. Ships by 5 PM the
  next business day." Plain, like a person, not a shipping policy.
- **Levers:** gateway availability phrasing (already de-roboticized); facts from
  fulfillment, never guessed.
- **Fixes:** "on-time orders arrive on June ninth, two thousand twenty-six."

### 5. Pricing (gated, graceful)
- **Desired:** "I can pull pricing up as soon as I verify the account — what's
  the account number, or the name it's under?" Asked once. If a known caller
  (phase 0), skip straight to the price.
- **Levers:** pricing gated in code; **rewritten refusal phrasing** (no tone-deaf
  "I can tell you availability now" after just doing it); pre-verification via the
  init webhook for known accounts.
- **Fixes:** "You just told me about availability and lead time" — the refusal no
  longer repeats what it just said.

### 6. The off-script / unclear turn
- **Desired:** a complaint, small talk, or an unclear line is handled like a
  person — acknowledge, steer back — **never** turned into fake part suggestions.
- **Levers:** gateway suppresses candidate SKUs when the text carries no part
  signal (returns a gentle clarify, not bogus "did you mean…"); prompt: only call
  resolve_part for actual part references; small-talk allowed but brief.
- **Fixes:** "You just told me about availability…" → "Did you mean SB-2-6745-GM…"
  → escalate. (The manufactured-candidates-from-prose bug.)

### 7. Escalation (real)
- **Desired:** "Let me get you to someone who can dig into that — one sec."
  Then an actual warm transfer.
- **Levers:** `transfer_to_number` (conference/warm), with `agent_message`
  briefing the human; gateway decides *when* to escalate.
- **Fixes:** today escalation just speaks a line and dead-ends.

### 8. Close
- **Desired:** "Anything else I can grab for you? … Thanks for calling, take
  care." Clean hang-up.
- **Levers:** `end_call` system tool (API agents don't get it by default);
  prompt: confirm "anything else" before closing.
- **Fixes:** calls that hang open / no graceful end.

## Quality → lever map ()

| Experience quality | ElevenLabs lever | Gateway lever |
|---|---|---|
| Human voice & pacing | flash v2, stability 0.7/style 0/speed 0.95, persona voice | — |
| Clear part numbers | alias pronunciation dict | spell SKUs ("K 5, 24 S B C") |
| Natural dimensions/dates | text-normalization off (we own it) | `spoken.py` to_spoken/_spoken_date |
| Doesn't rush the caller | `turn_eagerness: patient`, `spelling_patience`, `skip_turn` | — |
| Hears the SKU | `asr.keywords` (catalog) | fuzzy resolution absorbs garble |
| No dead air | `pre_tool_speech`, `soft_timeout_config` | fast (~2ms) deterministic resolve |
| Never invents | guardrails (focus/custom parts_only) | never-invent; RESOLVED⇒real row |
| Catches mishears | — | fuzzy correction reads back to confirm |
| Graceful pricing gate | pre-verify via init webhook | rewritten refusal; pricing in code |
| No fake options | — | suppress candidates w/o part signal |
| Recognizes regulars | init webhook + dynamic vars | account lookup by caller_id |
| Real handoff | `transfer_to_number` (warm) | escalation decision |
| Clean close | `end_call` | — |
| Gets better over time | post-call webhook + eval criteria + data collection | improvement loop ingest |

## Knowledge base / RAG — what belongs there (and what must not)

RAG is the *wrong* tool for the core job and the *right* tool for a narrow,
high-value slice. The dividing line is the same as the whole repo's thesis:

- **NEVER in RAG — the catalog/ERP facts:** part numbers, prices, stock, ship
  dates. These must stay behind the deterministic gateway tool.
  Putting the catalog in a document store would (a) go stale the moment inventory
  moves, (b) add ~250ms/turn, and (c) break never-invent — RAG returns the
  closest *chunk*, not a guaranteed-real catalog row. A wrong-but-plausible price
  is exactly the failure we've engineered out.
- **GOOD in RAG — the static prose a counter person just *knows*** and that isn't
  in the structured catalog, where being approximately right is fine and a human
  would rattle it off:
  - store hours, address, holiday closures, will-call / pickup info
  - return / core-charge / warranty / RMA policy
  - freight & shipping rules (LTL, minimum order, cutoff times)
  - brands carried (general)
- **NOT in scope — fitment / "what fits a Pete 379":** the tenant-001 catalog has
  **no make/model/application/fitment data** (only part number, description,
  material, dimensions, posting group, qty, price). The agent must NOT claim to
  look parts up by vehicle. A caller may *mention* their truck as context, but the
  agent resolves by **part number or by described attributes** (size, finish,
  family) — and if they only have the vehicle, it says it needs the part number
  or a description, or offers a human. The prompt's no-fitment-speculation
  guardrail enforces this; there is no fitment guide to RAG.

Why it matters for the experience: today, "what time do you close?" or "what's
your return policy?" makes the agent say "let me check" or escalate. A small KB
(hours/policy in `prompt` mode; larger policy docs in `auto`/RAG mode) lets it
answer those like a real person — without ever touching the part-fact guarantee.
**Verdict: add a small policy/hours KB; keep every catalog fact in the tool.**
It's step 7.5 in the sequence (cheap, additive, no risk to the core).

## Build sequence (smallest-risk first)

1. **Finish the open gateway bugs** — (a) suppress candidate SKUs from non-part
   prose; (b) rewrite the pricing-refusal phrasing. *(in progress)*
2. **Commit + re-apply the feature batch** so the next call reflects all of it
   (ASR keywords, patient turn-taking, soft-timeout, guardrails v1, voice tuning,
   system tools, SKU spelling, fuzzy-confirm). Verify via the simulator.
3. **Persona identity** — make the rep's name a configurable persona field, put it
   in the greeting, and instruct the agent never to invent one.
4. **Pronunciation dictionary** (alias) + **tool-auth secret** on `/agent/turn`.
5. **Real escalation** — `transfer_to_number` to the human parts line *(needs the
   number)*.
6. **Caller recognition** — conversation-init webhook → account lookup → personalized
   greeting + pre-verified pricing for known accounts.
7. **Self-improvement loop** — post-call webhook + evaluation criteria
   (`part_found`, `price_quoted`, `transferred`, `caller_satisfied`) + data
   collection (`resolved_sku`, `quoted_price`, `quantity`) → review queue.
8. **Config-as-code** — move the agent definition under `@elevenlabs/cli` so the
   whole thing (incl. guardrails) is version-controlled and CI-deployable.

## Open decisions (need your call)
- **Persona:** named rep ("this is Sam") vs nameless ("the parts department");
  accent (standard American / midwest / …).
- **Caller recognition:** build the caller-ID → account lookup (greet by name,
  auto-unlock pricing for known accounts)? Higher effort, big experience win.
- **Confirmation policy on voice:** always read the part back before answering
  (safest, most human) vs only on fuzzy/uncertain matches (snappier).
- **Escalation number:** the human parts line to warm-transfer to.
