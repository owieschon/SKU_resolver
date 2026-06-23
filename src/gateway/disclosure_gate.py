"""The disclosure gate (CONVERSATION_STATE_SPEC §3) — the deterministic core.

A fact may be spoken iff BOTH hold: its precondition is met (state check) AND it
was read fresh (timestamp check). Neither part is the LLM's call; both are
evaluated here, at the gateway, at disclosure time. The orchestration layer
advances the conversation toward a satisfied gate — it never satisfies the gate.
This is the floor the spec says to build first "so the orchestration has something
it cannot talk around"; accordingly it is a PURE FUNCTION — no I/O, no model, no
mutation — and the invariants (§9.2–9.4) are proven against it in isolation.

The load-bearing subtlety is the multi-part × shared-account interaction (§3.3,
invariant 3): the account is SHARED and durable, so establishing it once satisfies
the account-component of every part's price precondition — but identity and the
fresh read are PER-PART. An unidentified part must never inherit price
disclosability from an identified sibling under the same established account.
"""
from __future__ import annotations

from dataclasses import dataclass

from gateway.conversation_state import (
    AccountState, FactState, FactType, PartContext,
)


@dataclass(frozen=True)
class Horizons:
    """Per-fact-type freshness horizons in SECONDS (§3.2). These are CONSERVATIVE
    PLACEHOLDERS — short = re-read often — tuned against the customer's real data
    velocity (V5 in the production validation gate). Do NOT hard-code at a call
    site; inject this so the pilot can tune it without a code change."""
    availability: float = 120.0          # stock moves fastest -> short
    lead_time: float = 1800.0            # changes on restock cadence -> medium
    price: float = 86400.0              # account pricing stable day-to-day -> longer

    def for_type(self, fact_type: FactType) -> float:
        return {FactType.AVAILABILITY: self.availability,
                FactType.LEAD_TIME: self.lead_time,
                FactType.PRICE: self.price}[fact_type]


DEFAULT_HORIZONS = Horizons()


def precondition_met(part: PartContext, fact_type: FactType,
                     account: AccountState) -> bool:
    """State check (§3.1). Availability/lead-time need the part identified; price
    additionally needs the account established — because price is a property of the
    (part, account) pair and is UNDEFINED (not merely blocked) without it.

    Invariant 3: the identity check is on THIS part, never inherited from a sibling.
    The account is shared; identity is not."""
    if not part.identity.is_identified:
        return False
    if fact_type is FactType.PRICE:
        return account.is_established
    return True


def fresh(part: PartContext, fact_type: FactType, account: AccountState,
          now: float, horizons: Horizons = DEFAULT_HORIZONS) -> bool:
    """Timestamp check (§3.2). The fact must be READ (an unread or unreadable fact
    is never fresh — this is also how the pricing-not-wired interim §8 falls out:
    an `unreadable` price is never fresh, so never discloseable), within its
    horizon, and — for price only — read for the CURRENT account."""
    fact = part.fact(fact_type)
    if fact.state is not FactState.READ or fact.as_of is None:
        return False
    if (now - fact.as_of) > horizons.for_type(fact_type):
        return False
    if fact_type is FactType.PRICE and fact.account_id != account.account_id:
        return False                                  # price read for a different account
    return True


def discloseable(part: PartContext, fact_type: FactType, account: AccountState,
                 now: float, horizons: Horizons = DEFAULT_HORIZONS) -> bool:
    """The gate (§3): precondition_met AND fresh. The single predicate the say
    layer must consult before speaking any binding fact (invariant 2)."""
    return (precondition_met(part, fact_type, account)
            and fresh(part, fact_type, account, now, horizons))
