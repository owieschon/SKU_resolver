"""Memory layer — persists rep disambiguation choices for replay.

When a rep types ambiguous text and the disambiguator returns multiple
candidates, the rep picks one. That pick is a labeled training example:
(input_signature, customer_context, chosen_sku). Memory records it.

On the next translation request, before going to the disambiguator, the
orchestrator asks memory: "have we seen this signature for this customer
before? If yes with high enough confidence, replay the choice."

Architecture commitment
-----------------------
This module's data model maps onto the shared ``agent_events`` substrate
(per the architectural decision to share the substrate between OMA, PA,
and the translator). The real production implementation will write rows
with ``module='translator_memory'`` to Postgres via Supabase. The
implementation in this file is in-process for unit testing and
local validation; the orchestrator can swap in a Supabase-backed store
that implements the same protocol.

Schema (logical)
----------------
event_id          : uuid
module            : 'translator_memory'
created_at        : timestamp
signature         : str   -- normalized input fingerprint (see _signature)
customer          : str | None  -- customer id when known
chosen_sku        : str
spec_at_choice    : json  -- the PartSpec the rep saw when choosing
candidates_shown  : json  -- the candidate list the rep chose from
rep_id            : str | None
confidence_seen   : str   -- 'high' / 'medium' / 'low' from the disambiguator at the time

Threshold-based replay
----------------------
A signature gets replayed only if:
  - same signature has been chosen >= MIN_OBSERVATIONS times (default 3)
  - the same chosen_sku is at least DOMINANCE_RATIO of those (default 0.7)
  - within the last RECENCY_WINDOW_DAYS (default 365)

Below threshold, memory records the choice but doesn't yet trust it.
This is the "60-day learning period" model: log everything, replay only
when consensus is clear.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

try:
    from sku_translator.extractor import PartSpec
except ImportError:
    from extractor import PartSpec


# ============================================================================
# Configuration
# ============================================================================

MIN_OBSERVATIONS = 3
"""Minimum number of times a signature must be seen before replay is offered."""

DOMINANCE_RATIO = 0.7
"""Fraction of observations that must agree on the same SKU."""

RECENCY_WINDOW_DAYS = 365
"""Only count observations from the last N days."""


# ============================================================================
# Event record
# ============================================================================

@dataclass
class TranslatorEvent:
    """One persisted rep choice. Maps onto a row in agent_events."""
    signature: str
    chosen_sku: str
    customer: str | None = None
    rep_id: str | None = None
    spec_at_choice: dict[str, Any] = field(default_factory=dict)
    candidates_shown: list[str] = field(default_factory=list)
    confidence_seen: str = 'medium'
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================================================
# Signature
# ============================================================================

def _signature(spec: PartSpec, customer: str | None = None) -> str:
    """Compute a stable fingerprint for a partial spec.

    Two specs that differ only in raw_input but agree on extracted fields
    should produce the same signature. We use a sorted, key=value
    representation hashed with SHA-1 (we don't need cryptographic strength;
    just a stable short identifier).

    Customer scoping
    ----------------
    Customer is included in the signature so a (signature, customer) pair
    is the lookup key. The same input from a different customer should
    not replay the prior customer's choice.
    """
    parts = []
    for key in sorted(_SIGNATURE_FIELDS):
        val = getattr(spec, key, None)
        if val is None:
            continue
        parts.append(f'{key}={val}')
    if customer:
        parts.append(f'customer={customer}')
    payload = '|'.join(parts)
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]


_SIGNATURE_FIELDS = (
    'family', 'diameter', 'length', 'angle', 'leg1', 'leg2',
    'finish', 'body', 'inlet_diameter', 'outlet_diameter',
    'oem', 'truck_model', 'customer_program',
)


# ============================================================================
# Memory store protocol
# ============================================================================

class MemoryStore(Protocol):
    """Protocol that any backing store must implement.

    The in-process implementation (InMemoryStore below) is for local
    testing. Production will be a SupabaseMemoryStore that issues writes
    to agent_events with module='translator_memory'.
    """

    def record(self, event: TranslatorEvent) -> None:
        """Persist a rep choice."""
        ...

    def find_recent(self, signature: str, customer: str | None) -> list[TranslatorEvent]:
        """Return all events matching this signature/customer in the recency window."""
        ...


@dataclass
class InMemoryStore:
    """In-process implementation of MemoryStore. For tests and local runs."""
    events: list[TranslatorEvent] = field(default_factory=list)

    def record(self, event: TranslatorEvent) -> None:
        self.events.append(event)

    def find_recent(self, signature: str, customer: str | None) -> list[TranslatorEvent]:
        cutoff = datetime.now(timezone.utc).timestamp() - (RECENCY_WINDOW_DAYS * 86400)
        return [
            e for e in self.events
            if e.signature == signature
            and e.customer == customer
            and e.created_at.timestamp() >= cutoff
        ]


# ============================================================================
# Replay decision
# ============================================================================

@dataclass
class ReplayDecision:
    """Memory's verdict on whether to short-circuit disambiguation."""
    replay: bool
    """If True, the orchestrator should use ``chosen_sku`` directly."""

    chosen_sku: str | None = None
    """The SKU memory recommends."""

    observations: int = 0
    """How many matching observations memory has."""

    dominance: float = 0.0
    """Fraction of observations that agreed on chosen_sku."""

    reason: str = ''
    """Human-readable rationale, useful for audit logs."""


def consult_memory(
    spec: PartSpec,
    store: MemoryStore,
    customer: str | None = None,
    min_observations: int = MIN_OBSERVATIONS,
    dominance_ratio: float = DOMINANCE_RATIO,
) -> ReplayDecision:
    """Ask memory whether this spec has a high-confidence replay.

    Returns a ReplayDecision. The orchestrator should:
      - if replay=True, skip the disambiguator and return chosen_sku
      - if replay=False, proceed to disambiguator (and later, record the
        rep's pick by calling ``record_choice``)
    """
    sig = _signature(spec, customer=customer)
    events = store.find_recent(sig, customer)
    if len(events) < min_observations:
        return ReplayDecision(
            replay=False,
            observations=len(events),
            reason=f'Have {len(events)} prior observations; need {min_observations}',
        )
    counts = Counter(e.chosen_sku for e in events)
    chosen, count = counts.most_common(1)[0]
    dominance = count / len(events)
    if dominance < dominance_ratio:
        return ReplayDecision(
            replay=False,
            observations=len(events),
            dominance=dominance,
            reason=(
                f'{len(events)} observations but dominance only {dominance:.0%} '
                f'(need {dominance_ratio:.0%})'
            ),
        )
    return ReplayDecision(
        replay=True,
        chosen_sku=chosen,
        observations=len(events),
        dominance=dominance,
        reason=f'{count}/{len(events)} observations chose {chosen}',
    )


def record_choice(
    spec: PartSpec,
    chosen_sku: str,
    store: MemoryStore,
    customer: str | None = None,
    rep_id: str | None = None,
    candidates_shown: list[str] | None = None,
    confidence_seen: str = 'medium',
) -> None:
    """Persist a rep's disambiguation choice for future replay."""
    sig = _signature(spec, customer=customer)
    event = TranslatorEvent(
        signature=sig,
        chosen_sku=chosen_sku,
        customer=customer,
        rep_id=rep_id,
        spec_at_choice={k: getattr(spec, k, None) for k in _SIGNATURE_FIELDS},
        candidates_shown=candidates_shown or [],
        confidence_seen=confidence_seen,
    )
    store.record(event)


# ============================================================================
# Self-test
# ============================================================================

def _selftest() -> None:
    store = InMemoryStore()

    # Spec: ambiguous, missing length and finish
    spec = PartSpec(family='K', diameter=5.0, body='SB', raw_input='K 5 SB')

    # First call: no history
    decision = consult_memory(spec, store, customer='DEMO')
    assert not decision.replay, decision

    # Record three identical choices
    for _ in range(3):
        record_choice(spec, 'K5-24SBC', store, customer='DEMO')

    # Now replay should succeed
    decision = consult_memory(spec, store, customer='DEMO')
    assert decision.replay, decision
    assert decision.chosen_sku == 'K5-24SBC', decision

    # Different customer should NOT replay
    decision = consult_memory(spec, store, customer='FOURCORNERS')
    assert not decision.replay, decision

    # Mixed signal: dominance below threshold
    store2 = InMemoryStore()
    record_choice(spec, 'K5-24SBC', store2, customer='X')
    record_choice(spec, 'K5-24SBA', store2, customer='X')
    record_choice(spec, 'K5-30SBC', store2, customer='X')
    decision = consult_memory(spec, store2, customer='X')
    assert not decision.replay, decision  # 3 observations but no dominance

    # Dominance threshold met (3 of 4 chose same SKU)
    store3 = InMemoryStore()
    for _ in range(3):
        record_choice(spec, 'K5-24SBC', store3, customer='Y')
    record_choice(spec, 'K5-30SBC', store3, customer='Y')
    decision = consult_memory(spec, store3, customer='Y')
    assert decision.replay, decision
    assert decision.dominance == 0.75, decision

    print('memory v1.0 — self-test passed')


if __name__ == '__main__':
    _selftest()
