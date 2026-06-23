"""Orchestration layer (CONVERSATION_STATE_SPEC §5–6) — the agentic, forgiving
half, built ON TOP of the proven gate. It owns durable state (which parts, which
account), focus/anaphora, move selection, and the not-done-until-they-say-so
closure loop. It does NOT decide disclosure: every fact still passes the §3 gate at
`read_and_disclose`, so an adversarial move sequence cannot talk a fact past the
gate (§10). "Orchestration proposing disclosure does not authorize it."

The deterministic substrate is what is built and tested here (state transitions,
reference resolution, the gate-enforced disclosure path, closure semantics); in
production an LLM selects the moves, but no move it can pick reaches past the gate.

`read_and_disclose` honors §4 bundle coherence: it reads the in-scope perishable
facts FRESH and TOGETHER at one `now`, so the bundle shares an `as_of` and never
mixes a minute-zero availability with a minute-three price; a stale cached fact is
re-read, never re-spoken.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from gateway.conversation_state import (
    AccountState, ConversationState, Fact, FactState, FactType, IdentityState,
    PartContext,
)
from gateway.disclosure_gate import DEFAULT_HORIZONS, Horizons, discloseable
from gateway.disclosure_say import render_availability, render_price


@dataclass
class DisclosureOutcome:
    spoken: list = field(default_factory=list)    # (ctx_id, FactType, value) disclosed
    blocked: list = field(default_factory=list)   # (ctx_id, FactType, reason) not disclosed
    say: str = ''

    @property
    def all_blocked(self) -> bool:
        return not self.spoken and bool(self.blocked)


class Conversation:
    """One call. The orchestration maintains this; the gate guards every disclosure."""

    def __init__(self, *, horizons: Horizons = DEFAULT_HORIZONS):
        self.state = ConversationState()
        self.horizons = horizons

    # -- durable-state updates (§5.1) ---------------------------------------
    def establish_account(self, account_id: str) -> None:
        self.state.account = AccountState.established(account_id)

    def add_part(self, ctx_id: str, caller_reference: str = '') -> PartContext:
        part = PartContext(ctx_id=ctx_id, caller_reference=caller_reference)
        self.state.parts[ctx_id] = part
        self.state.focus = ctx_id                  # a freshly raised part takes focus
        return part

    def identify_part(self, ctx_id: str, sku: str) -> None:
        self.state.parts[ctx_id].identity = IdentityState.identified(sku)

    def mark_ambiguous(self, ctx_id: str, candidates) -> None:
        self.state.parts[ctx_id].identity = IdentityState.ambiguous(candidates)

    def set_focus(self, ctx_id: str) -> None:
        self.state.focus = ctx_id

    # -- reference resolution / anaphora (§5.3) -----------------------------
    def resolve_reference(self, candidate_ctx_ids) -> tuple:
        """Map a caller reference (already narrowed by the LLM to some candidate
        part contexts) to a focus. 0 -> none; 1 -> resolved; >=2 -> DISAMBIGUATE,
        never guess (a wrong referent is a wrong quote; the cost of asking is
        trivial). Mirrors the 0/1/many SKU-resolution discipline."""
        ids = [c for c in candidate_ctx_ids if c in self.state.parts]
        if not ids:
            return ('none', None)
        if len(ids) == 1:
            self.state.focus = ids[0]
            return ('resolved', ids[0])
        return ('disambiguate', tuple(ids))

    # -- disclosure THROUGH the gate (§5.2, §4) -----------------------------
    def read_and_disclose(self, ctx_ids, fact_types, *, reader, now) -> DisclosureOutcome:
        """Read the in-scope perishable facts fresh+together at `now`, then disclose
        ONLY those the gate clears. The gate runs regardless of the orchestration's
        proposal — that is the whole safety property."""
        out = DisclosureOutcome()
        for ctx in ctx_ids:
            part = self.state.parts[ctx]
            for ft in fact_types:
                # read fresh+together at the SAME `now` so the bundle is coherent.
                # (A cached fact past horizon is overwritten here -> never re-spoken.)
                fact = reader(part, ft, self.state.account, now)
                _set_fact(part, ft, fact)
                if discloseable(part, ft, self.state.account, now, self.horizons):
                    out.spoken.append((ctx, ft, fact.value))
                else:
                    out.blocked.append((ctx, ft, _block_reason(part, ft, self.state.account)))
        out.say = self._build_say(out.spoken)
        return out

    def _build_say(self, spoken) -> str:
        avail_items, price_lines = [], []
        for ctx, ft, val in spoken:
            part = self.state.parts[ctx]
            label = part.identity.sku or part.caller_reference or ctx
            if ft is FactType.AVAILABILITY:
                lead = self._lead_text(part, spoken, ctx)
                avail_items.append((label, bool(val), lead))
            elif ft is FactType.PRICE:
                price_lines.append(render_price(label, str(val)))
        say = render_availability(avail_items) if avail_items else ''
        if price_lines:
            say = (say + ' ' + ' '.join(price_lines)).strip()
        return say

    def _lead_text(self, part, spoken, ctx):
        for c, ft, val in spoken:
            if c == ctx and ft is FactType.LEAD_TIME:
                return str(val)
        return None

    # -- closure loop (§6) ---------------------------------------------------
    def note_completion_signal(self) -> None:
        """Only an affirmative caller signal sets caller_intent_complete (inv 7)."""
        self.state.caller_intent_complete = True

    @property
    def is_complete(self) -> bool:
        return self.state.caller_intent_complete


def _set_fact(part: PartContext, fact_type: FactType, fact: Fact) -> None:
    if fact_type is FactType.AVAILABILITY:
        part.availability = fact
    elif fact_type is FactType.LEAD_TIME:
        part.lead_time = fact
    elif fact_type is FactType.PRICE:
        part.price = fact


def _block_reason(part: PartContext, fact_type: FactType, account: AccountState) -> str:
    if not part.identity.is_identified:
        return 'identity'                          # ask / disambiguate this part
    if fact_type is FactType.PRICE and not account.is_established:
        return 'account'                           # establish account
    if part.fact(fact_type).state is FactState.UNREADABLE:
        return 'unreadable'                        # §8 can't-quote handoff
    return 'unfresh'
