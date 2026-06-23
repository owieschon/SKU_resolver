"""Agent brain — fabrication containment by owning the LLM seam (Phase 5).

The decision that closes the HO2503170 class isn't "catch fabrications," it's
"the model is never in a position to author a fact." This module is what runs at
the custom-LLM endpoint (the seam ElevenLabs hands us): it routes each turn and,
on any turn driven by a tool result, SUBSTITUTES the gateway's `say` verbatim —
the model is not invoked, so it cannot fabricate (token OR tokenless). The model
generates only on no-tool turns (small talk / deciding to call the tool), where a
deterministic filter is the backstop.

Invariants (every one a redline that was argued for):
  * ROUTER, default-to-fact: a turn responding to a tool result -> substitute the
    tool's `say`, model not in the path (relay-integrity: substitute, don't trust
    the model to copy). The model runs only when the last message is NOT a tool
    result.
  * NO MIXED TURNS: a turn is either a pure substituted say or pure model content.
    Never both — locating a verbatim span inside model-authored text is the
    prose-classification problem reopened.
  * FAIL-CLOSED: model error/timeout, or filter error -> the deterministic
    fallback utterance. No path emits unscanned/partial/unfiltered text.
  * TWO-TIER ALLOWLIST, role-typed: tier1 (tool-surfaced, authoritative) from tool
    messages; tier2 (caller-spoken, confirmable/echo-only) from user messages;
    ASSISTANT messages contribute NOTHING — by type — so a prior fabrication can't
    self-launder into the allowlist (the deterministic twin of self-training
    collapse).
  * FILTER ON RAW, STORE SCRUBBED: classify on pre-redaction text; the trace is
    post-`scrub_pii`.
  * The per-decision TRACE is the precondition for the containment test to mean
    anything (a green "no unlisted SKU spoken" is vacuous without proof the filter
    ran and blocked).

Residual, named honestly: a TOKENLESS fact asserted on a NO-TOOL turn (the model
skips the tool and free-forms "those are in stock") is not deterministically
catchable here — there's no token and no provenance to compare. It's mitigated by
the prompt rule (call the tool for any part fact) + the probabilistic ElevenLabs
guardrail, not closed. It is a strictly smaller surface than the original finding,
which was a tool-result turn and is now closed by substitution.

A message is {'role': 'user'|'assistant'|'tool', 'content': str,
              'result': {say, surfaced_skus, surfaced_values, kind}}  # tool only
`model_fn(messages) -> str | {'tool_call': ...}` is injected (real model live,
fake in tests). `now`/wall-clock is not needed here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

try:
    from observability.telemetry import scrub_pii
except Exception:                                  # pragma: no cover - telemetry optional
    def scrub_pii(t):
        return t

# Two fallbacks with DIFFERENT coherence domains — the distinction is tonal
# safety, not containment (both are safe non-fact lines):
#   FALLBACK — used only when the model PRODUCED output we then blocked for
#   containment (a fabricated id/value, an ungrounded tool key, a filter error).
#   filter_free only ever BLOCKs on an invented id or fabricated value, so the
#   caller was demonstrably talking about a part — "get a rep to confirm that part
#   number" is coherent.
#   SERVICE_FALLBACK — used when the model produced NOTHING usable (error,
#   over-budget, unmappable request, key unavailable). The turn's topic is unknown
#   (it may be small talk), so a part-number line would be a non-sequitur — e.g. a
#   caller saying "thanks, you've been helpful" must not hear "let me get a rep to
#   confirm that part number." This line is coherent for small talk OR a part turn.
FALLBACK = "Let me get a rep to confirm that exact part number — one moment."
SERVICE_FALLBACK = "Sorry, I didn't catch that — could you say it again?"
GROUNDING_FALLBACK = ("I want to make sure I pull the right part — could you give "
                      "me the part number, or describe what you're after?")

# Identifier detectors. BROAD on the model's output (a miss ships a fabrication):
# any token mixing letters and digits, or a hyphen-joined alnum group containing
# both. TIGHT on caller input (over-detect inflates the echo set; bare-digit
# account numbers must NOT enter tier2): require both a letter and a digit.
_TOKEN = re.compile(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*')


def _has_alpha_and_digit(t: str) -> bool:
    return bool(re.search('[A-Za-z]', t)) and bool(re.search(r'\d', t))


def detect_ids_broad(text: str) -> list[str]:
    """Any part-number-shaped token in any format (incl. foreign OEM like
    HO2503170), plus reassembled spelled sequences ('H O 2 5 0 3 1 7 0')."""
    out = [t for t in _TOKEN.findall(text or '')
           if _has_alpha_and_digit(t) and len(t.replace('-', '')) >= 4]
    # spelled-sequence reassembly: >=4 single alnum chars in a row -> one token
    for m in re.finditer(r'(?:\b[A-Za-z0-9]\b[ ,.]*){4,}', text or ''):
        joined = re.sub(r'[ ,.]', '', m.group())
        if _has_alpha_and_digit(joined):
            out.append(joined)
    return out


def detect_ids_tight(text: str) -> list[str]:
    """Caller-input path: only tokens with BOTH a letter and a digit (excludes
    bare-digit account numbers, which are PII and not parts)."""
    return [t for t in _TOKEN.findall(text or '')
            if _has_alpha_and_digit(t) and len(t.replace('-', '')) >= 4]


def normalize_id(s: str) -> str:
    return re.sub(r'[^A-Za-z0-9]', '', s or '').upper()


def tool_call_identifiers(out: dict) -> list[str]:
    """Identifiers the model put in a tool_call's `text` argument (OpenAI shape:
    tool_calls[].function.arguments is a JSON string). These are the INBOUND
    surface — what the model REQUESTS, distinct from what it says."""
    ids: list[str] = []
    for tc in (out.get('tool_call') or []):
        fn = tc.get('function') if isinstance(tc, dict) else None
        args = (fn or tc or {}).get('arguments') if isinstance(fn or tc, dict) else None
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        text = args.get('text', '') if isinstance(args, dict) else ''
        ids += detect_ids_broad(text)
    return ids


# Typed binding-value detection on model output (free-turn backstop). Each kind
# is matched separately so a stale QUANTITY can't launder a fabricated PRICE.
_V_PRICE = re.compile(r'\$\s?(\d[\d,]*(?:\.\d{1,2})?)|\b(\d+(?:\.\d{1,2})?)\s*(?:dollars?|cents?)\b', re.I)
_V_QTY = re.compile(r'\b(\d+)\s*(?:on hand|in stock|available|left)\b', re.I)
_V_DATE = re.compile(r'\b((?:january|february|march|april|may|june|july|august|'
                     r'september|october|november|december)\s+\d{1,2}|\d{4}-\d{2}-\d{2})\b', re.I)


def detect_values_typed(text: str) -> list[tuple[str, str]]:
    """-> [('unit_price','187.71'), ('qty','58'), ('ship_by','june 9')] (typed)."""
    out = []
    for m in _V_PRICE.finditer(text or ''):
        out.append(('unit_price', (m.group(1) or m.group(2)).replace(',', '')))
    for m in _V_QTY.finditer(text or ''):
        out.append(('qty', m.group(1)))
    for m in _V_DATE.finditer(text or ''):
        out.append(('ship_by', m.group(1).lower()))
    return out


def _norm_val(kind: str, v) -> str:
    s = str(v).strip().lower()
    if kind in ('unit_price',):
        try:
            return f'{float(s):.2f}'
        except ValueError:
            return s
    if kind == 'ship_by':
        return s[:10]
    return re.sub(r'[^0-9]', '', s)            # qty


# -- allowlist reconstruction (role-typed; the self-laundering boundary) -----

@dataclass
class Allowlist:
    tier1: set = field(default_factory=set)            # tool-surfaced (authoritative)
    tier2: set = field(default_factory=set)            # caller-spoken (echo-only)
    values: set = field(default_factory=set)           # (kind, normalized-value) seen, typed


def reconstruct(messages: list[dict]) -> Allowlist:
    a = Allowlist()
    for m in messages:
        role = m.get('role')
        if role == 'tool':
            res = m.get('result') or {}
            for s in (res.get('surfaced_skus') or ()):
                a.tier1.add(normalize_id(s))
            for k, v in (res.get('surfaced_values') or {}).items():
                if k in ('unit_price', 'qty', 'ship_by'):
                    a.values.add((k, _norm_val(k, v)))
        elif role == 'user':
            for t in detect_ids_tight(m.get('content') or ''):
                a.tier2.add(normalize_id(t))
        # role == 'assistant': contributes NOTHING. by type. (self-laundering boundary)
    return a


# -- the free-turn filter (backstop; tool-result turns never reach it) -------

@dataclass
class Verdict:
    allow: bool
    reason: str
    trace: dict


def filter_free(content: str, allow: Allowlist) -> Verdict:
    """Classify a model-authored free turn. Raises nothing — a thrown filter is
    handled as fail-closed by the caller."""
    spoken_ids = detect_ids_broad(content)
    spoken_vals = detect_values_typed(content)
    id_class = {}
    for sid in spoken_ids:
        n = normalize_id(sid)
        if n in allow.tier1:
            id_class[sid] = 'tier1'
        elif n in allow.tier2:
            id_class[sid] = 'tier2_echo'
        else:
            id_class[sid] = 'INVENTED'
    val_class = {}
    for kind, v in spoken_vals:
        # typed + must have been surfaced (same kind) somewhere this conversation
        ok = (kind, _norm_val(kind, v)) in allow.values
        val_class[f'{kind}:{v}'] = 'surfaced' if ok else 'FABRICATED'
    blocked_ids = [k for k, c in id_class.items() if c == 'INVENTED']
    blocked_vals = [k for k, c in val_class.items() if c == 'FABRICATED']
    allow_ok = not blocked_ids and not blocked_vals
    trace = {
        'route': 'free', 'model_invoked': True,
        'tier1': sorted(allow.tier1), 'tier2': sorted(allow.tier2),
        'values_seen': sorted(f'{k}:{v}' for k, v in allow.values),
        'spoken_ids': id_class, 'spoken_values': val_class,
        'blocked_ids': blocked_ids, 'blocked_values': blocked_vals,
        'decision': 'ALLOW' if allow_ok else 'BLOCK',
        # filter-on-raw, STORE-scrubbed:
        'content_scrubbed': scrub_pii(content),
    }
    reason = ('ok' if allow_ok else
              f'invented={blocked_ids} fabricated_values={blocked_vals}')
    return Verdict(allow_ok, reason, trace)


# -- the router (default-to-fact; substitute say; fail-closed) ---------------

def is_substitution_turn(messages: list[dict]) -> bool:
    return bool(messages) and messages[-1].get('role') == 'tool'


def substitution(messages: list[dict]) -> tuple:
    """The gateway say, verbatim. Model out of the fact path entirely — no
    authoring, no paraphrase, no fabrication (token or tokenless).

    Fail-closed on a DEGRADED-but-parseable tool result: an empty say would make
    the agent speak nothing (dead air on a fact turn), so an empty/whitespace say
    becomes the service fallback rather than silence."""
    res = (messages[-1].get('result') or {}) if messages else {}
    say = res.get('say', '')
    if not (say and str(say).strip()):
        return SERVICE_FALLBACK, {
            'route': 'substitute_empty', 'model_invoked': False,
            'tool_kind': res.get('kind'), 'decision': 'BLOCK',
            'fallback_used': True}
    return say, {
        'route': 'substitute_say', 'model_invoked': False,
        'tool_kind': res.get('kind'), 'surfaced_skus': res.get('surfaced_skus'),
        'decision': 'ALLOW'}


def _malformed_tool_call(out) -> bool:
    """A tool_call the gateway could not act on: arguments not JSON, or missing a
    non-empty `text`. Forwarding it to ElevenLabs yields an un-executable tool
    call (effectively a hang/garbage turn), so we treat it as produced-nothing-
    usable and fail closed."""
    for tc in (out.get('tool_call') or []):
        fn = tc.get('function') or {}
        try:
            args = json.loads(fn.get('arguments') or '{}')
        except (json.JSONDecodeError, TypeError):
            return True
        if not isinstance(args, dict) or not str(args.get('text', '')).strip():
            return True
    return not (out.get('tool_call'))                  # empty tool_call list


def apply_model_output(messages: list[dict], out, fallback: str = FALLBACK) -> tuple:
    """Post-model routing (sync OR async path share this): tool_call inbound
    containment, then free-turn filter. Fail-closed on a filter error."""
    if isinstance(out, dict) and 'tool_call' in out:
        # Fail-closed on a malformed tool_call (unparseable args / no text) —
        # don't forward an un-executable call to ElevenLabs (S4 fault).
        if _malformed_tool_call(out):
            return SERVICE_FALLBACK, {
                'route': 'malformed_tool_call', 'model_invoked': True,
                'decision': 'BLOCK', 'fallback_used': True}
        # INBOUND containment (the self-laundering boundary's inbound half): a
        # tool_call argument may reference only GROUNDED identifiers — tier1
        # (tool-surfaced) or tier2 (caller-spoken). A model-invented lookup key
        # (e.g. an exact in-catalog SKU the caller never said) would make the
        # gateway surface real facts for a part nobody asked about. Block it and
        # ground the request. A description with no identifier passes (the normal
        # path); only an ungrounded IDENTIFIER is suspect.
        allow = reconstruct(messages)
        ungrounded = [i for i in tool_call_identifiers(out)
                      if normalize_id(i) not in allow.tier1
                      and normalize_id(i) not in allow.tier2]
        if ungrounded:
            return GROUNDING_FALLBACK, {
                'route': 'tool_call_ungrounded', 'model_invoked': True,
                'decision': 'BLOCK', 'ungrounded_ids': ungrounded,
                'fallback_used': True}
        return out, {'route': 'tool_call', 'model_invoked': True,
                     'decision': 'ALLOW'}           # grounded; let it call the tool
    try:
        verdict = filter_free(str(out), reconstruct(messages))
    except Exception as e:                          # fail-closed
        return fallback, {'route': 'filter_error', 'model_invoked': True,
                          'decision': 'BLOCK', 'reason': str(e)[:120],
                          'fallback_used': True}
    if verdict.allow:
        return str(out), verdict.trace
    t = dict(verdict.trace)
    t['fallback_used'] = True
    return fallback, t


def decide_turn(messages: list[dict], *, model_fn, fallback: str = FALLBACK) -> tuple:
    """Sync composition (CI path): substitute on tool turns; else call the model
    (fail-closed) and apply_model_output. The live async path races the model call
    against a real deadline instead — see custom_llm.handle_async."""
    if is_substitution_turn(messages):
        return substitution(messages)
    try:
        out = model_fn(messages)
    except Exception as e:                          # fail-closed
        # model produced NOTHING — topic unknown -> service fallback, not the
        # part-number line (which would be a non-sequitur on a small-talk turn).
        return SERVICE_FALLBACK, {'route': 'model_error', 'model_invoked': True,
                                  'decision': 'BLOCK', 'reason': str(e)[:120],
                                  'fallback_used': True}
    return apply_model_output(messages, out, fallback)
