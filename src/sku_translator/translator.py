"""Orchestrator — single ``translate()`` entry point for the full pipeline.

Downstream agents (OMA, MIA, PA, Cue) call this. It hides the wiring of:

    normalize → extract → [parser-passthrough | construct | fuzzy | memory
                          | disambiguate] → TranslationResult

Pipeline (happy-path)
---------------------
1. **Normalize** the input into typed tokens.
2. **Extract** a PartSpec from the tokens.
3. **Parser passthrough**: if the parser produced canonical_sku and that
   SKU is in the catalog, return RESOLVED.
4. **Construct**: if the spec is fully populated, build a canonical SKU
   and verify catalog membership. If both succeed, return RESOLVED.
5. **Fuzzy match**: for input that looks like a single SKU but didn't
   resolve via parser/construct, search the catalog for typo-class
   matches.
6. **Memory replay**: if the spec is partial but we've seen this exact
   signature before from this customer with high confidence, replay.
7. **Disambiguate**: rank catalog candidates consistent with the spec.
   If one candidate dominates, RESOLVE; otherwise return PENDING.
8. If nothing fits, return UNRESOLVABLE — never invent a SKU.

Customer policy enforcement
---------------------------
When ``customer`` is provided, the orchestrator enforces proprietary-SKU
policy: if a candidate (resolved or pending) is marked proprietary AND
its proprietary_customer doesn't match ``customer``, the candidate is
filtered or downgraded. The orchestrator surfaces this as a
proprietary_violation field in the result. Candidates without an
attributed customer (proprietary=True, customer=None) are kept and
flagged for manual review rather than silently discarded.

Failure modes
-------------
- ``unresolved_pending``: spec is partial; rep needs to clarify
- ``unresolvable``: input doesn't match anything in the catalog
- ``resolved`` with ``confidence='medium'`` or ``'low'``: a SKU was
  produced but the orchestrator is flagging that something is unusual
  (e.g. constructed but not in catalog, single-customer policy concern)
"""
from __future__ import annotations

from dataclasses import dataclass, field

try:
    from sku_translator.catalog_index import CatalogIndex, ParsedRow
    from sku_translator.constructor import (
        ConstructionError,
        InsufficientSpecError,
        construct_sku,
    )
    from sku_translator.disambiguator import Candidate, disambiguate
    from sku_translator.extractor import Ambiguity, PartSpec, extract_spec
    from sku_translator.fuzzy_matcher import FuzzyMatch, fuzzy_match
    from sku_translator.memory import (
        MemoryStore,
        consult_memory,
        record_choice,
    )
except ImportError:
    from catalog_index import CatalogIndex, ParsedRow
    from constructor import (
        ConstructionError,
        InsufficientSpecError,
        construct_sku,
    )
    from disambiguator import Candidate, disambiguate
    from extractor import Ambiguity, PartSpec, extract_spec
    from fuzzy_matcher import FuzzyMatch, fuzzy_match
    from memory import (
        MemoryStore,
        consult_memory,
        record_choice,
    )


# Resolution states (mirror the data contract from the architecture docs)
RESOLVED = 'resolved'
PENDING_DISAMBIGUATION = 'pending_disambiguation'
UNRESOLVABLE = 'unresolvable'


@dataclass
class TranslationResult:
    """Unified result type for the full pipeline."""
    state: str
    """One of RESOLVED / PENDING_DISAMBIGUATION / UNRESOLVABLE."""

    sku: str | None = None
    """Canonical SKU when state == RESOLVED."""

    spec: PartSpec | None = None
    """The PartSpec produced by the extractor (always present for
    non-empty inputs)."""

    candidates: list[Candidate] = field(default_factory=list)
    """Ranked alternatives when state == PENDING_DISAMBIGUATION."""

    open_questions: list[Ambiguity] = field(default_factory=list)
    """Questions for the rep when state == PENDING_DISAMBIGUATION."""

    fuzzy_matches: list[FuzzyMatch] = field(default_factory=list)
    """Catalog matches found by fuzzy_matcher (typo / near-canonical input).
    Populated when fuzzy was tried, regardless of whether a fuzzy match
    drove the final state."""

    constructed_sku: str | None = None
    """The SKU the construct path built from the spec (diagnostic; also threaded
    from the construct path to the fuzzy fallback as its query)."""

    source: str = ''
    """How the SKU was determined: 'parser' / 'construct' / 'fuzzy' /
    'memory_replay' / 'disambiguator'."""

    confidence: str = 'medium'
    """'high' / 'medium' / 'low'."""

    reasoning: str = ''
    """One-line audit trail."""

    raw_input: str = ''
    """The original text the caller passed in."""

    proprietary_violation: bool = False
    """True iff the resolved/pending result violates proprietary-customer
    policy for the given customer parameter. Set when a SKU is flagged
    proprietary AND its attributed customer doesn't match the request."""

    proprietary_warning: str | None = None
    """Human-readable warning when proprietary_violation=True or when
    a proprietary SKU has no customer attribution."""


# ============================================================================
# Orchestrator
# ============================================================================

def translate(
    text: str,
    catalog: CatalogIndex | None = None,
    *,
    memory: MemoryStore | None = None,
    customer: str | None = None,
) -> TranslationResult:
    """Translate free-form text into a canonical SKU (or candidates).

    Parameters
    ----------
    text : raw input from a rep, customer email, voice transcript, etc.
    catalog : the tenant's CatalogIndex. If None, the orchestrator runs
        in 'parse-only' mode: returns parser/construct results without
        catalog verification. Production code paths always pass a catalog.
    memory : optional MemoryStore for replay of prior rep choices.
    customer : optional customer id. Scopes memory replay AND enforces
        proprietary-customer policy on the resolved candidate.

    Returns
    -------
    TranslationResult with state set to RESOLVED, PENDING_DISAMBIGUATION,
    or UNRESOLVABLE.
    """
    if not text or not str(text).strip():
        return TranslationResult(
            state=UNRESOLVABLE, raw_input=text or '', reasoning='Empty input')

    raw = str(text)

    # Verbatim catalog match wins over any extraction.
    hit = _resolve_verbatim(raw, catalog, customer)
    if hit is not None:
        return hit

    # Normalize + extract, then walk the resolution paths in priority order.
    # Each path mutates `result` and returns it (resolved) or None (fall through).
    spec = extract_spec(raw)
    result = TranslationResult(
        state=PENDING_DISAMBIGUATION, spec=spec, raw_input=raw)

    # Every _resolve_* shares one signature (result, spec, raw, catalog, memory,
    # customer) so this dispatch can call them uniformly; some ignore params they
    # don't need (e.g. _resolve_parser uses neither raw nor memory). The uniform
    # protocol is deliberate — it keeps the priority order a readable table here
    # rather than a chain of bespoke call sites.
    for path in (_resolve_parser, _resolve_construct, _resolve_fuzzy,
                 _resolve_memory):
        hit = path(result, spec, raw, catalog, memory, customer)
        if hit is not None:
            return hit

    return _finalize(result, spec, catalog, customer)


# ============================================================================
# Resolution paths — each returns a resolved TranslationResult or None.
# Order and behavior are exactly the pipeline translate() walks.
# ============================================================================

def _resolve_verbatim(
    raw: str, catalog: CatalogIndex | None, customer: str | None,
) -> TranslationResult | None:
    """Step 0: if the raw input IS a catalog SKU (modulo whitespace/case),
    return it. This stops the extractor from 'improving' a real SKU like
    'BOLT FOR H-BBP3' into 'H-BBP3' — a different real product."""
    if catalog is None or not catalog.is_canonical(raw.strip()):
        return None
    row = catalog.lookup(raw.strip())
    assert row is not None  # is_canonical() above guarantees it
    result = TranslationResult(
        state=RESOLVED, sku=row.sku, spec=extract_spec(raw), source='parser',
        confidence='high', reasoning=f'Verbatim catalog match: {row.sku}',
        raw_input=raw)
    return _apply_proprietary_policy(result, catalog, customer, parsed_row=row)


def _resolve_parser(
    result: TranslationResult, spec, raw: str,
    catalog: CatalogIndex | None, memory, customer: str | None,
) -> TranslationResult | None:
    """Step 3: the parser produced a canonical SKU; verify catalog membership."""
    if not spec.canonical_sku:
        return None
    if catalog is None:
        result.state = RESOLVED
        result.sku = spec.canonical_sku
        result.source = 'parser'
        result.confidence = 'medium'
        result.reasoning = 'Parser produced canonical SKU; no catalog provided to verify'
        return _apply_proprietary_policy(result, catalog, customer)
    if catalog.is_canonical(spec.canonical_sku):
        row = catalog.lookup(spec.canonical_sku)
        assert row is not None
        result.state = RESOLVED
        result.sku = row.sku  # use catalog's exact casing
        result.source = 'parser'
        result.confidence = 'high'
        result.reasoning = f'Exact catalog match: {result.sku}'
        return _apply_proprietary_policy(result, catalog, customer, parsed_row=row)
    return None


def _resolve_construct(
    result: TranslationResult, spec, raw: str,
    catalog: CatalogIndex | None, memory, customer: str | None,
) -> TranslationResult | None:
    """Step 4: build a SKU from the free-text spec, then verify membership.
    Stashes the constructed string on the result for the fuzzy fallback."""
    constructed: str | None = None
    try:
        constructed = construct_sku(spec)
    except InsufficientSpecError:
        pass  # fall through to fuzzy / memory / disambiguation
    except ConstructionError as e:
        result.reasoning = f'Construction error: {e}'
    result.constructed_sku = constructed  # threaded to _resolve_fuzzy
    if not constructed:
        return None
    if catalog is None:
        result.state = RESOLVED
        result.sku = constructed
        result.source = 'construct'
        result.confidence = 'medium'
        result.reasoning = f'Constructed {constructed}; no catalog provided to verify'
        return _apply_proprietary_policy(result, catalog, customer)
    if catalog.is_canonical(constructed):
        row = catalog.lookup(constructed)
        assert row is not None
        result.state = RESOLVED
        result.sku = row.sku
        result.source = 'construct'
        result.confidence = 'high'
        result.reasoning = f'Constructed and verified: {result.sku}'
        return _apply_proprietary_policy(result, catalog, customer, parsed_row=row)
    result.reasoning = (
        f'Constructed {constructed} but not in catalog; '
        'falling back to fuzzy/disambiguation')
    return None


def _resolve_fuzzy(
    result: TranslationResult, spec, raw: str,
    catalog: CatalogIndex | None, memory, customer: str | None,
) -> TranslationResult | None:
    """Step 5: bucket-scoped fuzzy match on a single-SKU-looking query."""
    if catalog is None:
        return None
    fuzzy_query = (spec.canonical_sku or result.constructed_sku or raw).strip()
    if not fuzzy_query or ' ' in fuzzy_query:
        return None
    matches = fuzzy_match(fuzzy_query, catalog)
    if not matches:
        return None
    result.fuzzy_matches = matches
    top = matches[0]
    if top.distance == 0:
        row = catalog.lookup(top.sku)
        result.state = RESOLVED
        result.sku = top.sku
        result.source = 'fuzzy'
        result.confidence = 'high'
        result.reasoning = f'Fuzzy match: {top.sku} ({top.match_kind})'
        return _apply_proprietary_policy(result, catalog, customer, parsed_row=row)
    runner_up_dist = matches[1].distance if len(matches) > 1 else 999
    if top.distance <= 1 and runner_up_dist >= top.distance + 1:
        row = catalog.lookup(top.sku)
        result.state = RESOLVED
        result.sku = top.sku
        result.source = 'fuzzy'
        # A distance>=1 correction CHANGED what the customer typed (e.g.
        # K5-24SPC -> K5-24SBC, a one-char substitution to a DIFFERENT part).
        # That is a hypothesis, not a certainty: mark it 'medium' so the
        # conversational layer reads it back for confirmation instead of
        # asserting it. Only a distance==0 normalization stays 'high' (above).
        result.confidence = 'medium'
        result.reasoning = (
            f'Fuzzy match (typo): {top.sku} '
            f'(d={top.distance}, runner-up d={runner_up_dist})')
        return _apply_proprietary_policy(result, catalog, customer, parsed_row=row)
    return None  # multiple plausible at similar distance — fall through


def _resolve_memory(
    result: TranslationResult, spec, raw: str,
    catalog: CatalogIndex | None, memory, customer: str | None,
) -> TranslationResult | None:
    """Step 6: replay a dominant prior rep choice (still catalog-verified)."""
    if memory is None:
        return None
    replay = consult_memory(spec, memory, customer=customer)
    if not (replay.replay and replay.chosen_sku):
        return None
    if catalog is not None and not catalog.is_canonical(replay.chosen_sku):
        return None  # never replay to a SKU that left the catalog
    row = catalog.lookup(replay.chosen_sku) if catalog else None
    result.state = RESOLVED
    result.sku = replay.chosen_sku
    result.source = 'memory_replay'
    result.confidence = 'high' if replay.dominance >= 0.9 else 'medium'
    result.reasoning = f'Memory replay: {replay.reason}'
    return _apply_proprietary_policy(result, catalog, customer, parsed_row=row)


def _finalize(
    result: TranslationResult, spec, catalog: CatalogIndex | None,
    customer: str | None,
) -> TranslationResult:
    """Step 7: disambiguate against the catalog, or surface what we have."""
    if catalog is not None:
        disamb = disambiguate(spec, catalog)
        result.candidates = disamb.candidates
        result.open_questions = disamb.open_questions
        if customer:
            result.candidates = _filter_proprietary_candidates(
                result.candidates, customer)
        if disamb.confidence == 'high' and result.candidates:
            top_cand = result.candidates[0]
            result.state = RESOLVED
            result.sku = top_cand.sku
            result.source = 'disambiguator'
            result.confidence = 'high'
            result.reasoning = disamb.reasoning
            return _apply_proprietary_policy(
                result, catalog, customer, parsed_row=top_cand.parsed)
        if result.candidates:
            result.state = PENDING_DISAMBIGUATION
            result.source = 'disambiguator'
            result.confidence = disamb.confidence
            result.reasoning = disamb.reasoning
            return result
        result.state = UNRESOLVABLE
        result.confidence = 'low'
        result.reasoning = disamb.reasoning
        return result

    # No catalog and we got here — surface what we have.
    if result.fuzzy_matches:
        result.state = PENDING_DISAMBIGUATION
        result.confidence = 'low'
        result.reasoning = (
            f'{len(result.fuzzy_matches)} fuzzy candidates; '
            'no disambiguator available to rank further')
        return result
    result.state = UNRESOLVABLE
    result.confidence = 'low'
    result.reasoning = result.reasoning or 'Spec is partial and no catalog was provided'
    return result


# ============================================================================
# Proprietary policy enforcement
# ============================================================================

def _apply_proprietary_policy(
    result: TranslationResult,
    catalog: CatalogIndex | None,
    customer: str | None,
    parsed_row: ParsedRow | None = None,
) -> TranslationResult:
    """Apply proprietary-customer policy to a resolved result.

    Cases:
      - SKU is not proprietary: no-op
      - SKU is proprietary, customer matches attribution: no-op
      - SKU is proprietary, customer doesn't match: set proprietary_violation=True
      - SKU is proprietary, no customer attribution available: set warning
        (don't block — let the rep decide; we don't have enough info to be
        certain it's a violation)
      - No customer provided: surface a warning if proprietary, but don't
        downgrade confidence (the caller is operating in customer-agnostic mode)
    """
    if result.state != RESOLVED or not result.sku:
        return result

    # Resolve the parsed row if not passed in
    if parsed_row is None and catalog is not None:
        parsed_row = catalog.lookup(result.sku)
    if parsed_row is None or not parsed_row.is_proprietary:
        return result

    # SKU is proprietary
    attributed = parsed_row.proprietary_customer

    if not customer:
        # Customer-agnostic mode — surface warning but don't violate
        result.proprietary_warning = (
            f'{result.sku} is a proprietary SKU'
            + (f' (attributed to {attributed})' if attributed else ' (customer attribution unknown)')
            + '; verify customer eligibility before quoting'
        )
        return result

    if not attributed:
        # Proprietary but unattributed — warn, don't block
        result.proprietary_warning = (
            f'{result.sku} is flagged proprietary but has no customer '
            f'attribution; verify {customer} is the eligible customer'
        )
        return result

    # We have both customer and attribution — compare
    if customer.upper().strip() == attributed.upper().strip():
        # Match — no warning needed
        return result

    # Mismatch — proprietary violation
    result.proprietary_violation = True
    result.proprietary_warning = (
        f'{result.sku} is proprietary to {attributed}; '
        f'cannot quote to {customer}'
    )
    # Downgrade confidence — caller should re-route to disambiguation or escalate
    result.confidence = 'low'
    return result


def _filter_proprietary_candidates(
    candidates: list[Candidate],
    customer: str,
) -> list[Candidate]:
    """Remove candidates that violate proprietary-customer policy for ``customer``.

    Keeps:
      - Non-proprietary SKUs (always)
      - Proprietary SKUs whose attribution matches the customer
      - Proprietary SKUs with no attribution (rep needs to confirm)

    Removes:
      - Proprietary SKUs whose attribution explicitly doesn't match the customer
    """
    customer_upper = customer.upper().strip()
    out: list[Candidate] = []
    for c in candidates:
        if not c.is_proprietary:
            out.append(c)
            continue
        attributed = c.proprietary_customer
        if not attributed:
            # Unattributed proprietary — keep, but callers should treat as warning
            out.append(c)
            continue
        if attributed.upper().strip() == customer_upper:
            out.append(c)
            continue
        # Mismatch — drop
    return out


# ============================================================================
# Recording rep choices (writes to memory)
# ============================================================================

def record_translation_choice(
    spec: PartSpec,
    chosen_sku: str,
    memory: MemoryStore,
    *,
    customer: str | None = None,
    rep_id: str | None = None,
    candidates_shown: list[Candidate] | None = None,
    confidence_seen: str = 'medium',
) -> None:
    """Persist a rep's chosen SKU after disambiguation.

    Call this AFTER a rep selects from a PENDING_DISAMBIGUATION result.
    The orchestrator deliberately separates ``translate`` (read-only) from
    ``record_translation_choice`` (write) so speculative or unfinished
    translations don't pollute memory.
    """
    record_choice(
        spec=spec,
        chosen_sku=chosen_sku,
        store=memory,
        customer=customer,
        rep_id=rep_id,
        candidates_shown=[c.sku for c in (candidates_shown or [])],
        confidence_seen=confidence_seen,
    )
