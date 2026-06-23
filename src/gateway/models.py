"""Typed domain model for the conversational service gateway.

Spec: docs/CONVERSATIONAL_GATEWAY_SPEC.md (G1-G8 + §2.5 hardening). Every
cross-boundary value is a frozen dataclass here; the gates are real state
machines enforced in session.py / pricing.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# --- session / verification (G2, #10, #13) -----------------------------------

class SessionState(Enum):
    UNVERIFIED = 'unverified'
    VERIFIED = 'verified'
    LOCKED = 'locked'          # enumeration lockout (G2)


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    phone: str | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    """#10: identification (verification) is NOT authorization. The pricing
    service requires one of these, carrying the SOURCE by which entitlement
    was established — a value conversational input cannot forge because it is
    only minted by the verification/entitlement code path."""
    account_id: str
    source: str                # e.g. 'verified_account_self'
    granted: bool


# --- identification (G3, #11, #14) -------------------------------------------

class Channel(Enum):
    TYPED = 'typed'
    VOICE = 'voice'


class ConfirmationStrength(Enum):
    NONE = 'none'
    WEAK = 'weak'              # bare yes/no — flagged, insufficient for pricing
    DISCRIMINATING = 'discriminating'   # caller affirmed a distinguishing attr


@dataclass(frozen=True)
class IdentifiedSKU:
    sku: str
    confirmed: bool
    strength: ConfirmationStrength
    source: str                # resolution source path


@dataclass(frozen=True)
class Candidate:
    sku: str
    reason: str


# --- answers (G4, G5) ---------------------------------------------------------

@dataclass(frozen=True)
class AvailabilityAnswer:
    sku: str
    in_stock: bool
    quantity_on_hand: int
    ship_by_iso: str
    basis: str                 # the ship-date rule that fired (provenance)
    plain: str                 # rep-language rendering
    catalog_version: str


@dataclass(frozen=True)
class PriceAnswer:
    sku: str
    account_id: str
    unit_price: float
    source: str                # authorization source (audit anchor)
    plain: str


# --- turn envelope (G1) -------------------------------------------------------

class TurnKind(Enum):
    IDENTIFY = 'identify'
    AVAILABILITY = 'availability'
    PRICING = 'pricing'
    VERIFY = 'verify'
    CONFIRM = 'confirm'
    ESCALATE = 'escalate'
    UNKNOWN = 'unknown'


class EscalationReason(Enum):
    EXPLICIT_REQUEST = 'explicit_request'      # caller asked for a human
    OUT_OF_SCOPE = 'out_of_scope'              # not a parts/availability/pricing ask
    REPEATED_FAILURE = 'repeated_failure'      # couldn't resolve after N tries
    LOW_CONFIDENCE = 'low_confidence'          # resolved but below the trust bar


@dataclass(frozen=True)
class Escalation:
    """A graceful-degradation handoff. The gateway emits this rather than
    guessing when it cannot confidently help — the most senior behavior a
    system can exhibit: knowing its own edge."""
    reason: str                    # EscalationReason value
    summary: str                   # why, for the receiving human
    action: str = 'connect_to_agent'


@dataclass(frozen=True)
class TurnResponse:
    """The single structured answer shape every channel receives."""
    kind: str
    text: str                                  # the spoken/typed reply
    session_state: str
    needs_confirmation: bool = False
    candidates: tuple[Candidate, ...] = ()
    availability: AvailabilityAnswer | None = None
    price: PriceAnswer | None = None
    escalation: Escalation | None = None       # populated on graceful handoff
    refused: str | None = None                 # why a request was refused (loud)
    meta: dict[str, Any] = field(default_factory=dict)
