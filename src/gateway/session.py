"""G2 — Session, verification, and the pricing-authorization decision.

Three hardened concerns live here (spec §2.5):

  #13 session security — HMAC-signed tokens (constant-time compare), TTL,
      idle re-lock, absolute lifetime. Designed fresh: the borrowed the prior agent
      stack had no session-token security.

  G2 verification — UNVERIFIED -> VERIFIED only via a deterministic customer-DB
      match. No conversational content can move the state. Enumeration is
      defended by a per-session attempt budget -> LOCKED. No existence oracle:
      every non-single-match outcome returns the SAME neutral refusal.

  #10 authorization — issue_authorization() mints an AuthorizationDecision
      whose `source` conversational input cannot forge. A verified account is
      entitled to ITS OWN pricing only; cross-account is never granted.

Time is injected (now_fn) — deterministic under test, same discipline as the
fulfillment clock and the harness ManualClock.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from gateway.customer_db import CustomerDB
from gateway.models import (
    Account,
    AuthorizationDecision,
    SessionState,
)

MAX_VERIFY_ATTEMPTS = 5
IDLE_RELOCK_SECONDS = 600          # a quiet verified session re-locks
ABSOLUTE_LIFETIME_SECONDS = 3600   # hard ceiling regardless of activity

NEUTRAL_REFUSAL = (
    "I couldn't verify that account. I can share availability and lead times "
    "without an account; for pricing I need a matching account number or name."
)


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


@dataclass
class Session:
    session_id: str
    channel_id: str
    created_at: float
    last_active: float
    state: SessionState = SessionState.UNVERIFIED
    account_id: str | None = None
    verify_attempts: int = 0
    recent_skus: list[str] = field(default_factory=list)   # #14 anaphora context
    token_sig: str = ''


class VerificationResult(Enum):
    VERIFIED = 'verified'
    NEEDS_DISAMBIGUATION = 'needs_disambiguation'
    REFUSED = 'refused'
    LOCKED = 'locked'


@dataclass
class SessionManager:
    secret: bytes
    customer_db: CustomerDB
    now_fn: Callable[[], float]
    _sessions: dict[str, Session] = field(default_factory=dict)

    # -- lifecycle -------------------------------------------------------------

    def open(self, session_id: str, channel_id: str) -> str:
        now = self.now_fn()
        s = Session(session_id=session_id, channel_id=channel_id,
                    created_at=now, last_active=now)
        s.token_sig = _sign(self.secret, f'{session_id}:{channel_id}:{now}')
        self._sessions[session_id] = s
        return s.token_sig

    def _get_live(self, session_id: str, token: str) -> Session | None:
        """Return the session iff the token verifies (constant-time) AND it
        has not expired. Expiry/idle re-locks a VERIFIED session to
        UNVERIFIED (#13) rather than silently serving stale authorization."""
        s = self._sessions.get(session_id)
        if s is None:
            return None
        if not hmac.compare_digest(s.token_sig, token or ''):
            return None
        now = self.now_fn()
        aged = (now - s.created_at) >= ABSOLUTE_LIFETIME_SECONDS
        idle = (now - s.last_active) >= IDLE_RELOCK_SECONDS
        if (aged or idle) and s.state is SessionState.VERIFIED:
            s.state = SessionState.UNVERIFIED
            s.account_id = None
        s.last_active = now
        return s

    def state_of(self, session_id: str, token: str) -> SessionState:
        s = self._get_live(session_id, token)
        return s.state if s else SessionState.UNVERIFIED

    # -- verification (G2) -----------------------------------------------------

    def verify(self, session_id: str, token: str, *, account_no: str | None,
               name: str | None) -> tuple['VerificationResult', list[Account]]:
        s = self._get_live(session_id, token)
        if s is None:
            return VerificationResult.REFUSED, []
        if s.state is SessionState.LOCKED:
            return VerificationResult.LOCKED, []

        s.verify_attempts += 1
        if s.verify_attempts > MAX_VERIFY_ATTEMPTS:
            s.state = SessionState.LOCKED          # enumeration defense
            return VerificationResult.LOCKED, []

        match: Account | None = None
        if account_no:
            match = self.customer_db.by_number(account_no)
        elif name:
            hits = self.customer_db.by_name(name)
            if len(hits) == 1:
                match = hits[0]
            elif len(hits) >= 2:
                # 2+ -> disambiguation (the ONLY non-refusal that reveals
                # anything, and only that "narrow it down", not which exist).
                return VerificationResult.NEEDS_DISAMBIGUATION, hits[:3]

        if match is None:
            # No existence oracle: not-found and match-failed are identical.
            return VerificationResult.REFUSED, []
        s.state = SessionState.VERIFIED
        s.account_id = match.account_id
        return VerificationResult.VERIFIED, [match]

    # -- authorization (#10) ---------------------------------------------------

    def issue_authorization(self, session_id: str, token: str,
                            target_account_id: str) -> AuthorizationDecision:
        """Entitlement is SEPARATE from identity. A verified account may see
        only its OWN pricing; anything else is denied even when verified."""
        s = self._get_live(session_id, token)
        if s is None or s.state is not SessionState.VERIFIED or s.account_id is None:
            return AuthorizationDecision(target_account_id, 'unverified', False)
        if s.account_id != target_account_id:
            return AuthorizationDecision(target_account_id,
                                         'cross_account_denied', False)
        return AuthorizationDecision(s.account_id, 'verified_account_self', True)

    # -- anaphora context (#14) ------------------------------------------------

    def remember_sku(self, session_id: str, token: str, sku: str) -> None:
        s = self._get_live(session_id, token)
        if s is not None:
            s.recent_skus = ([sku] + [x for x in s.recent_skus if x != sku])[:5]

    def recent_skus(self, session_id: str, token: str) -> list[str]:
        s = self._get_live(session_id, token)
        return list(s.recent_skus) if s else []
