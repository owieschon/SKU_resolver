"""G8 — append-only conversation audit journal.

Records every consequential event: turns, identifications, readback
confirmations (with strength), verification attempts (success AND failure),
pricing disclosures, refusals, lockouts. Two hardened properties:

  #12 transcript PII — every text field is scrubbed via observability.scrub_pii
  (which carries the account-number patterns) BEFORE it is written. Raw
  spoken account numbers never hit disk.

  Build-to-audit — refused and blocked attempts are journaled too, not just
  successes, so an enumeration attack or an injection attempt is reconstructable
  (the principle borrowed from a prior agent's sheet-write-blocked.jsonl).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from observability import scrub_pii

_log = logging.getLogger(__name__)


class EventType(Enum):
    TURN = 'turn'
    IDENTIFY = 'identify'
    CONFIRM = 'confirm'
    VERIFY_ATTEMPT = 'verify_attempt'
    VERIFY_LOCKED = 'verify_locked'
    PRICING_DISCLOSED = 'pricing_disclosed'
    PRICING_REFUSED = 'pricing_refused'
    ESCALATED = 'escalated'
    REFUSAL = 'refusal'


# Fields whose values are free text and must be scrubbed before persistence.
_TEXT_FIELDS = frozenset({'text', 'transcript', 'reply', 'readback', 'summary'})


@dataclass
class ConversationJournal:
    path: Path
    now_fn: Callable[[], str]              # returns an ISO stamp
    rows: list[dict] = field(default_factory=list)

    def record(self, event: EventType, session_id: str, **fields: Any) -> None:
        # BEST-EFFORT (G6): the audit journal is a dependency, and a dependency
        # must never fail a live turn. A disk-full / permission / serialization
        # failure here is logged, not raised — losing an audit row is bad, dropping
        # a caller's turn is worse. (The in-memory append still succeeds so the
        # same-process audit view stays consistent for the rest of the call.)
        try:
            scrubbed = {k: (scrub_pii(v) if k in _TEXT_FIELDS and isinstance(v, str)
                            else v)
                        for k, v in fields.items()}
            row = {'ts': self.now_fn(), 'event': event.value,
                   'session_id': session_id, **scrubbed}
            self.rows.append(row)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a') as f:
                f.write(json.dumps(row) + '\n')
        except Exception:                               # never fail the turn
            _log.warning('journal write failed for %s/%s', event, session_id,
                         exc_info=True)

    def events(self, event: EventType) -> list[dict]:
        return [r for r in self.rows if r['event'] == event.value]

    def for_session(self, session_id: str) -> list[dict]:
        return [r for r in self.rows if r['session_id'] == session_id]
