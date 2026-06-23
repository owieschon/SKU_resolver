"""Conversation state model (CONVERSATION_STATE_SPEC §2) — the substrate the
disclosure gate reasons over and the orchestration layer maintains.

The seam maps onto the data's temporal structure: DURABLE IDENTITY (which parts,
which account) is gathered conversationally in any order; PERISHABLE FACTS
(availability / lead time / price — each a time-stamped child of a part) are read
by the gateway fresh and together at the moment of disclosure. This module is the
durable/perishable split as types; it is pure data (no I/O, no model), so the gate
built on it (disclosure_gate.py) can be boring and provable.

`account_id` lives on a price Fact because PRICE is a property of the (part,
account) pair — in B2B it does not exist until the account is known (§3.1). A Fact
without an `as_of` is a rumor, not a fact (§2.3): every binding fact carries when
it was read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Optional, TypeVar

T = TypeVar('T')


class FactType(Enum):
    AVAILABILITY = 'availability'
    LEAD_TIME = 'lead_time'
    PRICE = 'price'


class FactState(Enum):
    UNREAD = 'unread'          # never read this call
    READ = 'read'              # read; value + as_of meaningful
    UNREADABLE = 'unreadable'  # no source wired (e.g. pricing-not-yet, §8)


@dataclass(frozen=True)
class Fact(Generic[T]):
    """A temporal child of a PartContext (§2.3). Default = unread."""
    state: FactState = FactState.UNREAD
    value: Optional[T] = None
    as_of: Optional[float] = None        # read time (epoch secs); part of identity
    account_id: Optional[str] = None     # PRICE only: the index the price is valid for

    @classmethod
    def unread(cls) -> 'Fact':
        return cls(state=FactState.UNREAD)

    @classmethod
    def unreadable(cls) -> 'Fact':
        """No source wired — its precondition can never be satisfied (§8)."""
        return cls(state=FactState.UNREADABLE)

    @classmethod
    def read(cls, value, *, as_of: float, account_id: Optional[str] = None) -> 'Fact':
        return cls(state=FactState.READ, value=value, as_of=as_of,
                   account_id=account_id)


# -- durable identity --------------------------------------------------------

class IdentityKind(Enum):
    UNKNOWN = 'unknown'
    AMBIGUOUS = 'ambiguous'
    IDENTIFIED = 'identified'


@dataclass(frozen=True)
class IdentityState:
    """unknown | ambiguous(candidates) | identified(sku) (§2.2)."""
    kind: IdentityKind = IdentityKind.UNKNOWN
    sku: Optional[str] = None
    candidates: tuple = ()

    @classmethod
    def unknown(cls) -> 'IdentityState':
        return cls(kind=IdentityKind.UNKNOWN)

    @classmethod
    def ambiguous(cls, candidates) -> 'IdentityState':
        return cls(kind=IdentityKind.AMBIGUOUS, candidates=tuple(candidates))

    @classmethod
    def identified(cls, sku: str) -> 'IdentityState':
        return cls(kind=IdentityKind.IDENTIFIED, sku=sku)

    @property
    def is_identified(self) -> bool:
        return self.kind is IdentityKind.IDENTIFIED


@dataclass(frozen=True)
class AccountState:
    """unknown | established(account_id) (§2.1). Durable: the account is a property
    of the caller, not the part — established once, indexes every part's price."""
    account_id: Optional[str] = None

    @classmethod
    def unknown(cls) -> 'AccountState':
        return cls(account_id=None)

    @classmethod
    def established(cls, account_id: str) -> 'AccountState':
        return cls(account_id=account_id)

    @property
    def is_established(self) -> bool:
        return self.account_id is not None


# -- the part and the call ---------------------------------------------------

@dataclass
class PartContext:
    """One distinct part the caller raised (§2.2). A part number is an IDENTITY,
    not a record: durable identity + perishable fact-children."""
    ctx_id: str
    caller_reference: str = ''
    identity: IdentityState = field(default_factory=IdentityState.unknown)
    availability: Fact = field(default_factory=Fact.unread)
    lead_time: Fact = field(default_factory=Fact.unread)
    price: Fact = field(default_factory=Fact.unread)

    def fact(self, fact_type: FactType) -> Fact:
        return {FactType.AVAILABILITY: self.availability,
                FactType.LEAD_TIME: self.lead_time,
                FactType.PRICE: self.price}[fact_type]


@dataclass
class ConversationState:
    """Top-level, one per call (§2.1)."""
    account: AccountState = field(default_factory=AccountState.unknown)
    parts: dict = field(default_factory=dict)        # part_ctx_id -> PartContext
    focus: Optional[str] = None                       # volatile; LLM-maintained
    # starts False; only an affirmative caller signal sets it True; a disclosure
    # NEVER sets it (invariant 7).
    caller_intent_complete: bool = False
