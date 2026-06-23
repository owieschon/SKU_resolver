"""G1 — the conversational orchestrator: one turn in, one TurnResponse out.

This is where the gates are enforced and the spine holds: never-invent
(SKUs only via the resolution service), pricing behind authorization (#10),
discriminating readback for voice (#11), PII-scrubbed journaling (#12),
session security (#13), anaphora context (#14). A turn is classified into a
TurnKind, then dispatched; every consequential step is journaled, including
refusals.

The intent classifier is deterministic and rule-based (regime:
DETERMINISTIC-PLUMBING for routing); it does NOT use an LLM — keeping CI
deterministic and the routing auditable. An LLM intent layer can replace it
behind the same interface later (the resolution service is where the model
already is justified).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from datetime import datetime

    from sku_translator import CatalogIndex

from gateway import escalation
from gateway.answers import (
    PricingRefused,
    availability,
    pricing,
)
from gateway.conversation import Conversation
from gateway.conversation_state import Fact, FactType
from gateway.disclosure_gate import Horizons
from gateway.identification import (
    classify_confirmation,
    identify,
    looks_like_anaphora,
)
from gateway.journal import ConversationJournal, EventType
from gateway.models import (
    Candidate,
    Channel,
    ConfirmationStrength,
    TurnResponse,
)
from gateway.pricebook import PriceBook
from gateway.session import NEUTRAL_REFUSAL, SessionManager, VerificationResult
from gateway.spoken import spoken_description, to_spoken
from observability import get_logger, log_event, set_attr, tracer
from resolution import ResolutionService

_log = get_logger('gateway')

_PRICE_RE = re.compile(r'\b(price|cost|how much|quote|pricing)\b', re.I)
_AVAIL_RE = re.compile(r'\b(in stock|available|availability|lead time|'
                       r'when can|ship|how many|on hand)\b', re.I)
_VERIFY_RE = re.compile(r'\b(account|acct)\b', re.I)
_ACCT_NO_RE = re.compile(r'\b(?:account|acct)\s*(?:no\.?|number|#)?\s*[:#]?\s*'
                         r'(\d{3,12})\b', re.I)


@dataclass
class Gateway:
    service: ResolutionService
    catalog: CatalogIndex
    inventory: dict
    catalog_version: str
    sessions: SessionManager
    journal: ConversationJournal
    pricebook: PriceBook
    account_tier_of: Callable[[str], str]
    now_fn: Callable[[], datetime]    # tz-aware datetime for ship dates
    # P2 seam: intent classifier. Default rule-based (deterministic); inject an
    # LLMIntentRouter for production language understanding. Either way the
    # gates below still bind.
    intent_router: object = None
    # session_id -> sku awaiting a readback confirmation
    _pending_sku: dict = field(default_factory=dict)
    # session_id -> consecutive unresolved-attempt count (drives REPEATED_FAILURE)
    _fail_count: dict = field(default_factory=dict)
    # caller_id -> Conversation (CONVERSATION_STATE_SPEC orchestration state). Server-
    # side and keyed by the call -> durable state (which parts, which account) is
    # NEVER reconstructed from attacker-influenceable message history; the account
    # establishes ONLY via a real verify, so a claim in the utterance/history cannot
    # launder "account established" into the durable state (the self-laundering
    # boundary, extended from the allowlist to the conversation state machine).
    _conversations: dict = field(default_factory=dict)
    # CONSERVATIVE PLACEHOLDER freshness horizons (CONVERSATION_STATE_SPEC §3.2,
    # PRODUCTION_VALIDATION_GATE V5). These are GUESSES — short = re-read often,
    # never serve stale — NOT tuned values. The real horizons come from the
    # customer's data velocity in the pilot. Flagged here so a future read does not
    # mistake the placeholder for a decision. NOTE: with self-stamped as_of=now on
    # each read, the freshness arm is wired and authoritative but does not BIND
    # until the data source carries real read-timestamps (the V5 pilot wiring); a
    # stale read is structurally rejected (test), it just can't arise yet.
    disclosure_horizons: Horizons = field(default_factory=Horizons)
    # escalate after this many consecutive failed identification attempts
    MAX_RESOLVE_ATTEMPTS: int = 2

    def _router(self):
        if self.intent_router is None:
            from gateway.intent import RuleBasedIntentRouter
            self.intent_router = RuleBasedIntentRouter()
        return self.intent_router

    # -- public surface (G1) ---------------------------------------------------

    def turn(self, session_id: str, token: str, text: str, *,
             channel: Channel) -> TurnResponse:
        self.journal.record(EventType.TURN, session_id, text=text,
                            channel=channel.value)
        # Auth/session resolution stays OUTSIDE the fail-closed wrapper: a bad
        # token is not an internal fault to swallow into an escalation — it must
        # surface to the caller (401/403), the same posture as never masking a
        # security error as a degraded part answer.
        state = self.sessions.state_of(session_id, token)
        # One span per customer turn; the resolve/LLM sub-spans nest under it, so
        # a whole turn is diagnosable in Phoenix/any OTLP backend. No-op when
        # tracing is off; attributes pass the redaction chokepoint.
        with tracer.start_as_current_span('gateway.turn') as sp:
            set_attr(sp, 'svc.task', 'gateway.turn')
            set_attr(sp, 'session.id', session_id)
            set_attr(sp, 'svc.channel', channel.value)
            try:
                resp = self._dispatch(session_id, token, text, channel, state)
            except Exception as e:
                # G1–G5 fail-closed: ANY internal dependency fault (resolution,
                # inventory, pricebook, customer-DB, intent) becomes a coherent
                # escalation instead of a 500 into ElevenLabs — we do not control
                # what ElevenLabs does with a tool 500 (assume dead air), so the
                # gateway owning a well-formed escalation keeps the turn alive.
                # The fault is journaled (loud for the operator), not swallowed.
                sp.record_exception(e)
                resp = self._fault_escalation(session_id, token, e)
            set_attr(sp, 'svc.outcome', resp.kind)
            if getattr(resp, 'refused', None):
                set_attr(sp, 'svc.refused', resp.refused)
            return resp

    def _dispatch(self, session_id, token, text, channel, state) -> TurnResponse:
        # Pending readback confirmation takes precedence (the prior turn asked).
        pending = self._pending_sku.get(session_id)
        if pending is not None and not _looks_like_new_request(text):
            return self._handle_confirmation(session_id, token, text, pending,
                                             channel)

        # Classify the turn via the intent seam (rule-based by default; an
        # LLM router drops in for production). The gates below still bind
        # regardless of how the intent was derived.
        from gateway.intent import Intent
        decision = self._router().classify(text)
        if decision.intent is Intent.HANDOFF:
            return self._escalate(session_id, token, decision.escalation)
        if decision.intent is Intent.VERIFY:
            return self._handle_verify(session_id, token, text)
        if decision.intent is Intent.PRICING:
            return self._handle_pricing(session_id, token, text, channel, state)
        # AVAILABILITY: identification / availability flow.
        return self._handle_availability(session_id, token, text, channel)

    def _fault_escalation(self, session_id, token, exc) -> TurnResponse:
        """Internal dependency fault -> coherent hand-off, never a 500. Journaled
        with the exception type so the operator sees it; the caller hears a normal
        escalation, not an error."""
        # Surface the swallowed fault as an actionable WARNING log (not just the
        # journal): a 500 turned into an escalation should still be visible to ops.
        log_event(_log, 'warning', 'gateway.internal_fault',
                  session=session_id, exc_type=type(exc).__name__)
        try:
            self.journal.record(EventType.ESCALATED, session_id,
                                reason='internal_fault',
                                summary=f'{type(exc).__name__}: {str(exc)[:120]}')
        except Exception:                               # journal is best-effort
            pass
        self._fail_count.pop(session_id, None)
        try:
            session_state = self.sessions.state_of(session_id, token).value
        except Exception:
            session_state = None
        return TurnResponse(
            kind='escalate',
            text="Let me connect you with someone who can help with that.",
            session_state=session_state or '', refused='internal_error')

    # -- orchestration-backed turn (CONVERSATION_STATE_SPEC) -------------------

    def converse(self, caller_id: str, token: str, text: str, *,
                 channel: Channel = Channel.TYPED) -> TurnResponse:
        """Caller-led, multi-part, closure-aware turn. REPLACES the fixed-sequence
        `turn()` on the /agent/turn path. Reuses the answer builders (so `surfaced`
        provenance and the authorization/lockout gates are preserved by
        construction) and layers the spec's durable Conversation state
        (parts/account/focus), the not-done-until-they-say-so closure loop, and the
        per-turn decision point the pilot harness instruments.

        Durable state is server-side (keyed by caller_id), NEVER reconstructed from
        message history: the account is established ONLY by a real verify, so no
        assistant turn / utterance claim can launder "account established" into the
        durable state. Fail-closed: an internal fault becomes a coherent escalation,
        never a 500 and NEVER a silent revert to legacy determinism."""
        conv = self._conversations.get(caller_id)
        if conv is None:
            conv = Conversation(horizons=self.disclosure_horizons)
            self._conversations[caller_id] = conv
        if _is_completion_signal(text):
            conv.note_completion_signal()              # only the caller closes (inv 7)
            return self._decided(TurnResponse(
                kind='close', text="You're all set — thanks for calling.",
                session_state=self.sessions.state_of(caller_id, token).value),
                conv, move='close')
        try:
            return self._converse_dispatch(caller_id, token, text, channel, conv)
        except Exception as e:
            return self._fault_escalation(caller_id, token, e)

    def _converse_dispatch(self, caller_id, token, text, channel, conv):
        from gateway.intent import Intent
        decision = self._router().classify(text)
        if decision.intent is Intent.HANDOFF:
            return self._decided(
                self._escalate(caller_id, token, decision.escalation),
                conv, move='escalate')
        if decision.intent is Intent.VERIFY:
            resp = self._handle_verify(caller_id, token, text)
            # mirror a REAL verification into durable state — the ONLY way the
            # account becomes established (state-laundering boundary).
            if (resp.kind == 'verify' and resp.refused is None
                    and not resp.needs_confirmation):
                acct = _verified_account(self.sessions, caller_id, token)
                if acct:
                    conv.establish_account(acct)
            return self._decided(resp, conv, move='establish_account')
        if decision.intent is Intent.PRICING:
            return self._decided(
                self._converse_disclose(caller_id, token, text, channel, conv,
                                        FactType.PRICE),
                conv, move='price')
        return self._decided(
            self._converse_disclose(caller_id, token, text, channel, conv,
                                    FactType.AVAILABILITY),
            conv, move='availability')

    # -- gate-backed disclosure (the live path RUNS through discloseable) ------

    def _converse_disclose(self, sid, token, text, channel, conv,
                           fact_type) -> TurnResponse:
        """Resolve identity, then DISCLOSE THROUGH THE GATE. The disclosure
        decision is `Conversation.read_and_disclose` -> `discloseable` (precondition
        AND fresh), NOT a legacy sequence position — so identity/account/freshness
        are gate-enforced on the path that actually runs. Resolution branching
        (readback, candidates, unresolved) reuses the deterministic helpers."""
        st = self.sessions.state_of(sid, token).value
        kind, payload = self._resolve_focus(sid, token, text, channel, conv,
                                             fact_type)
        if kind == 'confirm':
            self._pending_sku[sid] = payload
            return TurnResponse(
                kind='identify', text=self._readback_text(payload),
                session_state=st, needs_confirmation=True,
                meta={'surfaced_sku': payload})
        if kind == 'candidates':
            return self._candidates_or_escalate(sid, token, payload, st)
        if kind == 'unresolved':
            return self._unresolved_turn(sid, token, text, channel, st)
        # IDENTIFIED -> run the gate over THIS part (and only this part).
        ctx = payload
        now = self._disclosure_now()
        captured: dict = {}
        out = conv.read_and_disclose(
            [ctx], [fact_type], reader=self._fact_reader(sid, token, captured),
            now=now)
        return self._render_disclosure(sid, token, conv, ctx, fact_type, out,
                                       captured, st)

    def _resolve_focus(self, sid, token, text, channel, conv, fact_type):
        """Resolve the in-scope part and reflect it into durable state. Returns
        ('identified', ctx) | ('confirm', sku) | ('candidates', outcome) |
        ('unresolved', None). An AMBIGUOUS part is placed in `conv` as ambiguous and
        returned as 'identified'(ctx) for PRICE, so the GATE — not a pre-empting
        disambiguation — is what blocks pricing an unidentified part (inherited-
        disclosability is a gate property on the live path)."""
        anaphora_sku, _ = self._resolve_text(sid, token, text)
        sku = anaphora_sku
        if sku is None and not _has_sku_shape(text) and fact_type is FactType.PRICE:
            recent = self.sessions.recent_skus(sid, token)
            if recent:
                sku = recent[0]
        if sku is None:
            outcome = identify(text, channel=channel, service=self.service,
                               catalog=self.catalog)
            if outcome.state == 'identified':
                sku = outcome.identified.sku
            elif outcome.state == 'needs_confirmation':
                return ('confirm', outcome.identified.sku)
            elif outcome.state == 'candidates':
                if fact_type is FactType.PRICE:
                    # place the ambiguous part in scope and let the GATE block its
                    # price on identity (do not pre-empt with disambiguation).
                    ctx = f'amb:{outcome.candidates[0].sku}'
                    if ctx not in conv.state.parts:
                        conv.add_part(ctx)
                    conv.mark_ambiguous(ctx, tuple(c.sku for c in outcome.candidates))
                    conv.set_focus(ctx)
                    conv.state.parts[ctx]._candidates_outcome = outcome  # for render
                    return ('identified', ctx)
                return ('candidates', outcome)
            else:
                return ('unresolved', None)
        self.sessions.remember_sku(sid, token, sku)
        self._fail_count.pop(sid, None)
        ctx = f'part:{sku}'
        if ctx not in conv.state.parts:
            conv.add_part(ctx)
        conv.identify_part(ctx, sku)
        conv.set_focus(ctx)
        return ('identified', ctx)

    def _fact_reader(self, sid, token, captured):
        def reader(part, ft, account, now):
            sku = part.identity.sku
            if sku is None:                       # ambiguous/unidentified: no read
                return Fact.unread()
            if ft is FactType.AVAILABILITY:
                ans = availability(sku, inventory=self.inventory,
                                   received_at=self.now_fn(),
                                   catalog_version=self.catalog_version)
                if ans is None:
                    return Fact.unreadable()
                captured[(part.ctx_id, ft)] = ans
                return Fact.read(ans.in_stock, as_of=now)
            if ft is FactType.PRICE:
                own = _verified_account(self.sessions, sid, token)
                auth = self.sessions.issue_authorization(
                    sid, token, own or '__none__')
                try:
                    ans = pricing(sku, auth, pricebook=self.pricebook,
                                  account_tier_of=self.account_tier_of)
                except PricingRefused:
                    return Fact.unreadable()
                captured[(part.ctx_id, ft)] = ans
                return Fact.read(ans.unit_price, as_of=now,
                                 account_id=auth.account_id)
            return Fact.unreadable()
        return reader

    def _render_disclosure(self, sid, token, conv, ctx, fact_type, out,
                           captured, st) -> TurnResponse:
        ans = captured.get((ctx, fact_type))
        if out.spoken and ans is not None:        # gate cleared -> disclose
            if fact_type is FactType.AVAILABILITY:
                self.journal.record(EventType.IDENTIFY, session_id=sid,
                                    sku=ans.sku, confirmed=True)
                return TurnResponse(kind='availability', text=ans.plain,
                                    session_state=st, availability=ans)
            self.journal.record(EventType.PRICING_DISCLOSED, session_id=sid,
                                sku=ans.sku, account_id=ans.account_id,
                                price=ans.unit_price, source=ans.source)
            return TurnResponse(kind='pricing', text=ans.plain,
                                session_state=st, price=ans)
        # BLOCKED by the gate — map the reason to a coherent move.
        reason = out.blocked[0][2] if out.blocked else 'unfresh'
        part = conv.state.parts.get(ctx)
        if reason == 'account':
            return TurnResponse(
                kind='pricing', session_state=st, refused='pricing_unauthorized',
                text="I can pull pricing up as soon as I verify the account — "
                     "what's the account number, or the name it's under?")
        if reason == 'identity':                  # ambiguous part: disambiguate
            outcome = getattr(part, '_candidates_outcome', None)
            if outcome is not None:
                q = escalation.informed_question(outcome.open_questions,
                                                 outcome.candidates)
                return TurnResponse(kind='identify', text=q, session_state=st,
                                    candidates=outcome.candidates,
                                    needs_confirmation=True)
            return self._unresolved_turn(sid, token, '', Channel.TYPED, st)
        # unreadable (no source / not in inventory) -> can't-quote handoff (§8)
        return self._escalate(sid, token, escalation.repeated_failure(
            'could not pull that part up'))

    def _candidates_or_escalate(self, sid, token, outcome, st) -> TurnResponse:
        n = self._fail_count.get(sid, 0) + 1
        self._fail_count[sid] = n
        if n >= self.MAX_RESOLVE_ATTEMPTS:
            return self._escalate(sid, token, escalation.repeated_failure(
                'caller could not narrow to one part'))
        q = escalation.informed_question(outcome.open_questions, outcome.candidates)
        return TurnResponse(kind='identify', text=q, session_state=st,
                            candidates=outcome.candidates, needs_confirmation=True)

    def _unresolved_turn(self, sid, token, text, channel, st) -> TurnResponse:
        if _is_filler(text):
            return TurnResponse(kind='unknown', session_state=st,
                                text="Sure — what part can I help you with?")
        oos = escalation.no_signal_out_of_scope(
            text, has_sku_shape=_has_sku_shape(text))
        if oos is not None:
            return self._escalate(sid, token, oos)
        n = self._fail_count.get(sid, 0) + 1
        self._fail_count[sid] = n
        if n >= self.MAX_RESOLVE_ATTEMPTS:
            return self._escalate(sid, token, escalation.repeated_failure())
        return TurnResponse(kind='unknown', session_state=st,
                            text="I didn't catch a part there — what are you "
                                 "looking for?")

    def _readback_text(self, sku) -> str:
        return f"Just to confirm — the {sku}?"

    def _disclosure_now(self) -> float:
        try:
            return self.now_fn().timestamp()
        except Exception:
            import time
            return time.time()


    def _decided(self, resp, conv, *, move):
        """Attach the per-turn decision point (the pilot harness's labeling unit):
        the move, the focus part, account state, and the disclosure outcome — all
        from the structured response, never parsed from prose."""
        import dataclasses
        focus = conv.state.parts.get(conv.state.focus)
        meta = dict(getattr(resp, 'meta', None) or {})
        meta['decision'] = {
            'move': move,
            'focus': conv.state.focus,
            # resolution outcome (for RESOLUTION-correctness labeling — phrase->SKU):
            'resolved_sku': (focus.identity.sku
                             if focus and focus.identity.is_identified else None),
            'resolution': (focus.identity.kind.value if focus else 'none'),
            'candidates': [c.sku for c in (getattr(resp, 'candidates', None) or ())],
            # behavioral outcome (for BEHAVIORAL-correctness labeling — the move):
            'account_established': conv.state.account.is_established,
            'disclosed': bool(getattr(resp, 'availability', None)
                              or getattr(resp, 'price', None)),
            'refused': resp.refused,
            'escalated': resp.kind == 'escalate',
            'caller_intent_complete': conv.state.caller_intent_complete,
        }
        return dataclasses.replace(resp, meta=meta)        # TurnResponse is frozen

    # -- escalation (graceful degradation) -------------------------------------

    def _escalate(self, session_id, token, esc) -> TurnResponse:
        self.journal.record(EventType.ESCALATED, session_id,
                            reason=esc.reason, summary=esc.summary)
        self._fail_count.pop(session_id, None)
        return TurnResponse(
            kind='escalate',
            text="Let me connect you with someone who can help with that.",
            session_state=self.sessions.state_of(session_id, token).value,
            escalation=esc, refused=esc.reason)

    # -- internals -------------------------------------------------------------

    def _resolve_text(self, session_id: str, token: str, text: str):
        """Resolve, applying anaphora context (#14) if the text is a reference."""
        if looks_like_anaphora(text):
            recent = self.sessions.recent_skus(session_id, token)
            if recent:
                return recent[0], 'anaphora'
        return None, None

    def _handle_availability(self, session_id, token, text, channel) -> TurnResponse:
        anaphora_sku, _ = self._resolve_text(session_id, token, text)
        if anaphora_sku is not None:
            return self._availability_for(session_id, token, anaphora_sku,
                                          channel, confirm_first=True)
        outcome = identify(text, channel=channel, service=self.service,
                           catalog=self.catalog)
        identified = outcome.identified  # state implies non-None; guard narrows it
        if outcome.state == 'identified' and identified is not None:
            sku = identified.sku
            self.sessions.remember_sku(session_id, token, sku)
            self._fail_count.pop(session_id, None)            # success resets
            self.journal.record(EventType.IDENTIFY, session_id, sku=sku,
                                confirmed=True, source=identified.source)
            return self._availability_answer(session_id, token, sku)
        if outcome.state == 'needs_confirmation' and identified is not None:
            self._pending_sku[session_id] = identified.sku
            self.journal.record(EventType.IDENTIFY, session_id,
                                sku=identified.sku, confirmed=False)
            return TurnResponse(
                kind='identify', text=outcome.readback or '',
                session_state=self.sessions.state_of(session_id, token).value,
                needs_confirmation=True,
                meta={'surfaced_sku': identified.sku})
        if outcome.state == 'candidates':
            # A candidate list is progress, but a caller who keeps landing
            # here without converging should be handed off — count it as a
            # non-answer toward the repeated-failure threshold.
            n = self._fail_count.get(session_id, 0) + 1
            self._fail_count[session_id] = n
            if n >= self.MAX_RESOLVE_ATTEMPTS:
                return self._escalate(session_id, token,
                                      escalation.repeated_failure(
                                          'caller could not narrow to one part'))
            # Informed disambiguation: ask what the resolver already knows is
            # missing/distinguishing, not a bare "which one?".
            q = escalation.informed_question(outcome.open_questions,
                                             outcome.candidates)
            return TurnResponse(
                kind='identify', text=q,
                session_state=self.sessions.state_of(session_id, token).value,
                candidates=outcome.candidates, needs_confirmation=True)

        # A bare affirmation / filler ("yes", "okay", "thanks") is conversational
        # glue, not a failed part attempt — don't count it toward escalation
        # (bug: a stray "Yes" used to tip the caller into repeated_failure).
        if _is_filler(text):
            return TurnResponse(
                kind='unknown', text="Sure — what part can I help you with?",
                session_state=self.sessions.state_of(session_id, token).value)

        # Unresolvable. No-part-signal -> escalate now; otherwise count the
        # attempt and escalate once we've clearly failed to help.
        oos = escalation.no_signal_out_of_scope(
            text, has_sku_shape=_has_sku_shape(text))
        if oos is not None:
            return self._escalate(session_id, token, oos)
        n = self._fail_count.get(session_id, 0) + 1
        self._fail_count[session_id] = n
        if n >= self.MAX_RESOLVE_ATTEMPTS:
            return self._escalate(session_id, token, escalation.repeated_failure())
        return TurnResponse(
            kind='unknown',
            text="I couldn't match that to a part. Can you give the part "
                 "number or describe it (size, finish, family)?",
            session_state=self.sessions.state_of(session_id, token).value,
            refused='unresolvable')

    def _availability_for(self, session_id, token, sku, channel,
                          confirm_first) -> TurnResponse:
        if confirm_first:
            self._pending_sku[session_id] = sku
            row = self.catalog.lookup(sku)
            spoken = spoken_description(row)
            desc = spoken or to_spoken((row.description if row else '') or sku)
            return TurnResponse(
                kind='identify',
                text=f"You mean {sku} — that's {desc}. Is that right?",
                session_state=self.sessions.state_of(session_id, token).value,
                needs_confirmation=True, meta={'surfaced_sku': sku})
        return self._availability_answer(session_id, token, sku)

    def _availability_answer(self, session_id, token, sku) -> TurnResponse:
        ans = availability(sku, inventory=self.inventory,
                           received_at=self.now_fn(),
                           catalog_version=self.catalog_version)
        if ans is None:
            return TurnResponse(kind='availability',
                                text=f"I don't have {sku} in the catalog.",
                                session_state=self.sessions.state_of(session_id, token).value,
                                refused='not_in_catalog')
        return TurnResponse(kind='availability', text=ans.plain,
                            session_state=self.sessions.state_of(session_id, token).value,
                            availability=ans)

    def _handle_confirmation(self, session_id, token, text, sku,
                             channel) -> TurnResponse:
        strength = classify_confirmation(text, expected_sku=sku,
                                         catalog=self.catalog)
        self.journal.record(EventType.CONFIRM, session_id, sku=sku,
                            strength=strength.value, reply=text)
        if strength is ConfirmationStrength.NONE:
            # Denied / unclear -> drop to candidates, preserve the denied pick.
            self._pending_sku.pop(session_id, None)
            outcome = identify(text, channel=channel, service=self.service,
                               catalog=self.catalog)
            cands = outcome.candidates or (Candidate(sku, 'previously offered'),)
            return TurnResponse(
                kind='identify',
                text="Got it, not that one. Which of these — or describe it?",
                session_state=self.sessions.state_of(session_id, token).value,
                candidates=cands, needs_confirmation=True)
        # WEAK or DISCRIMINATING confirm the identity for availability.
        self._pending_sku.pop(session_id, None)
        self.sessions.remember_sku(session_id, token, sku)
        return self._availability_answer(session_id, token, sku)

    def _handle_verify(self, session_id, token, text) -> TurnResponse:
        # On a verify turn, a bare 3-12 digit run is the account number
        # (handles "account number is 1001", "acct 1001", "1001"). Name only
        # when no digit run is present.
        m = re.search(r'\b(\d{3,12})\b', text)
        account_no = m.group(1) if m else None
        name = None if account_no else _extract_name(text)
        result, accounts = self.sessions.verify(
            session_id, token, account_no=account_no, name=name)
        self.journal.record(EventType.VERIFY_ATTEMPT, session_id,
                            by='number' if account_no else 'name',
                            result=result.value)
        st = self.sessions.state_of(session_id, token).value
        if result is VerificationResult.VERIFIED:
            return TurnResponse(kind='verify',
                                text="You're verified — I can share pricing now.",
                                session_state=st)
        if result is VerificationResult.NEEDS_DISAMBIGUATION:
            names = '; '.join(a.name for a in accounts)
            return TurnResponse(kind='verify',
                                text=f"A few accounts match — which one? {names}",
                                session_state=st, needs_confirmation=True)
        if result is VerificationResult.LOCKED:
            self.journal.record(EventType.VERIFY_LOCKED, session_id)
            return TurnResponse(kind='verify',
                                text="Too many attempts — verification is locked "
                                     "for this session. Availability is still open.",
                                session_state=st, refused='verification_locked')
        return TurnResponse(kind='verify', text=NEUTRAL_REFUSAL,
                            session_state=st, refused='verification_failed')

    def _handle_pricing(self, session_id, token, text, channel,
                        state) -> TurnResponse:
        # Identify the SKU: anaphora ("that one") -> a part named in THIS turn ->
        # otherwise the part we were just discussing (a bare "what's the price?"
        # after confirming a part means THAT part — bug: it used to re-identify
        # the priceless text, fail, and escalate).
        anaphora_sku, _ = self._resolve_text(session_id, token, text)
        sku = anaphora_sku
        # Only try to identify a NEW part when the turn actually names one (a SKU
        # shape). A bare "what's the price?" names no part, so don't let identify
        # spuriously match — price the part we were just discussing instead.
        if sku is None and _has_sku_shape(text):
            outcome = identify(text, channel=channel, service=self.service,
                               catalog=self.catalog)
            if outcome.state == 'identified' and outcome.identified is not None:
                sku = outcome.identified.sku
            else:
                # a named-but-ambiguous part -> disambiguate via availability
                return self._handle_availability(session_id, token, text, channel)
        if sku is None:
            recent = self.sessions.recent_skus(session_id, token)
            if recent:
                sku = recent[0]              # the part in context
            else:
                return self._handle_availability(session_id, token, text, channel)

        # #10: authorization is separate from identity. Mint a decision for the
        # caller's OWN verified account; refuse otherwise — loudly + journaled.
        own = _verified_account(self.sessions, session_id, token)
        auth = self.sessions.issue_authorization(session_id, token, own) if own \
            else self.sessions.issue_authorization(session_id, token, '__none__')
        try:
            ans = pricing(sku, auth, pricebook=self.pricebook,
                          account_tier_of=self.account_tier_of)
        except PricingRefused as e:
            self.journal.record(EventType.PRICING_REFUSED, session_id, sku=sku,
                                reason=str(e))
            return TurnResponse(
                kind='pricing',
                text="I can pull pricing up as soon as I verify the account — "
                     "what's the account number, or the name it's under?",
                session_state=self.sessions.state_of(session_id, token).value,
                refused='pricing_unauthorized')
        self.journal.record(EventType.PRICING_DISCLOSED, session_id, sku=sku,
                            account_id=ans.account_id, price=ans.unit_price,
                            source=ans.source)
        return TurnResponse(kind='pricing', text=ans.plain,
                            session_state=self.sessions.state_of(session_id, token).value,
                            price=ans)


# -- small deterministic helpers ----------------------------------------------

def _has_sku_shape(text: str) -> bool:
    return bool(re.search(r'\b[A-Za-z]{1,4}\d', text))


_FILLER_RE = re.compile(
    r'^\s*(yes|yeah|yep|yup|ok|okay|sure|thanks|thank you|great|got it|'
    r'right|correct|uh huh|mm hmm|please|hello|hi|hey)[\s.!,]*$', re.I)


def _is_filler(text: str) -> bool:
    """A short affirmation/greeting with no part content — conversational glue,
    not a failed resolution attempt."""
    return bool(_FILLER_RE.match(text or ''))


# An AFFIRMATIVE completion signal (§6) — "no, that's it". Conservative: requires
# an explicit completion phrase, so a bare readback "no" (a different part) or a
# part request never trips it (done-the-moment-they-say-so, but only when they do).
_COMPLETION_RE = re.compile(
    r"\b(that'?s (it|all|everything|all i need)"
    r"|no(,?\s*(that'?s (it|all|everything)|i'?m (good|all set|done)|thanks?))"
    r"|all set|i'?m all set|we'?re all set|nothing else|no thank you)\b", re.I)


def _is_completion_signal(text: str) -> bool:
    return bool(_COMPLETION_RE.search(text or ''))


def _wants_verify(text: str) -> bool:
    return bool(re.search(r'\b(verify|my account|account (number|no|#|name)|'
                          r'here\'?s my account)\b', text, re.I)) \
        or bool(_ACCT_NO_RE.search(text))


def _looks_like_new_request(text: str) -> bool:
    return bool(_PRICE_RE.search(text) or _AVAIL_RE.search(text)
                or _wants_verify(text))


def _extract_name(text: str) -> str | None:
    m = re.search(r'(?:account (?:name )?(?:is )?|for )\s*([A-Za-z][A-Za-z0-9 &\.\-]{2,})',
                  text, re.I)
    return m.group(1).strip() if m else None


def _verified_account(sessions: SessionManager, session_id: str,
                      token: str) -> str | None:
    s = sessions._get_live(session_id, token)   # internal: own-account id
    return s.account_id if s and s.account_id else None
