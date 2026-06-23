"""Unified resolution service: deterministic translator first, BM25 candidate
fallback second, one response envelope always.

The two guarantees this module exists to make checkable from the outside
(tests/test_resolution_adversarial.py attacks both):

  NEVER-INVENT — a RESOLVED result references a real row in THIS tenant's
  catalog, and every fallback candidate does too. The translator enforces
  this by design (round-trip-audited); the retrieval layer by
  construction AND by adversarial fuzzing.

  TENANT ISOLATION — a service instance is bound at construction to one
  CatalogIndex + one memory store. There is no cross-tenant lookup path to
  misuse: isolation is the shape of the object graph, and the adversarial
  suite proves it behaviorally (tenant A can never resolve or surface
  tenant B's SKUs).

`needs_review` (scope seam, decision D6): the production-validated
structural flag rule — "top-1 is customer-novel AND an in-history candidate
sits in top-5" — requires per-customer purchase history, which this dataset
does not contain, and its measured F1 collapsed to 0.15-0.33 at production
accuracy anyway (locked readout, 2026-05-02; redesign = milestone M2,
deliberately out of scope). This slice derives needs_review from resolver
STATE instead: anything other than a high-confidence RESOLVED, or any
proprietary-policy hit, needs eyes. Crude, accurate, and stated here rather
than smoothed over.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from observability import set_attr, tracer
from observability.telemetry import register_structured
from resolution.retrieval import BM25CatalogRetriever
from sku_translator import (
    PENDING_DISAMBIGUATION,
    RESOLVED,
    UNRESOLVABLE,
    translate,
)
from sku_translator.catalog_index import CatalogIndex

# Declare the resolution-layer scalar attributes as structured (pass-through,
# not scrubbed) — they are labels we own, never free text.
register_structured('svc.source', 'svc.confidence', 'svc.entity_count',
                    'svc.proposed', 'svc.verified')


@dataclass(frozen=True)
class OpenQuestion:
    """A still-unresolved ambiguity the caller could answer to narrow further
    (e.g. 'Family K needs a finish code'). Surfaced so the conversation layer
    can ask an INFORMED question instead of a generic 'which one?'."""
    field: str                     # the ambiguity type, e.g. 'finish_unspecified'
    reason: str
    options: tuple[str, ...]       # candidate resolutions, when enumerable


@dataclass(frozen=True)
class Candidate:
    sku: str
    reason: str                    # INTERNAL diagnostic (carries scores/sources);
                                   # never spoken — the say layer must use `description`
    source: str  # 'translator' | 'retrieval:bm25'
    differing_fields: tuple[str, ...] = ()   # fields this candidate fills in
    description: str = ''           # caller-safe catalog text for a spoken readback


@dataclass(frozen=True)
class Resolution:
    """The complete response envelope — every field, every time (M3 DoD)."""
    state: str                     # resolved | pending_disambiguation | unresolvable
    sku: str | None
    source: str                    # e.g. 'translator:verbatim', 'retrieval:bm25'
    confidence: str                # high | medium | low | none
    flags: tuple[str, ...]
    needs_review: bool
    candidates: tuple[Candidate, ...]
    catalog_version: str           # content hash of the catalog the answer is valid against
    tenant_id: str
    raw_input: str
    proprietary_warning: str | None = None
    reasoning: str | None = None
    open_questions: tuple[OpenQuestion, ...] = ()   # informed-disambiguation surface


def catalog_content_version(xlsx_path: str | Path) -> str:
    """Catalog-level content hash (sha256, 12 hex). The export has no
    per-row versioning, so the catalog hash is the row-version surrogate —
    a Resolution is auditable to exactly the catalog bytes it answered
    from (decision D7)."""
    return hashlib.sha256(Path(xlsx_path).read_bytes()).hexdigest()[:12]


class ResolutionService:
    def __init__(self, catalog: CatalogIndex, memory, *,
                 catalog_version: str, retriever_top_k: int = 10,
                 chooser=None, learned_aliases=None) -> None:
        self._catalog = catalog
        self._memory = memory
        self._version = catalog_version
        self._retriever = BM25CatalogRetriever(catalog, top_k=retriever_top_k)
        # Optional learned-alias layer (SME-confirmed phrase->real-SKU
        # corrections from the continuous-improvement loop). Duck-typed:
        # anything with `.alias_for(text) -> sku | None`. Consulted ONLY when
        # the deterministic engine can't resolve — corrections fill gaps, never
        # override deterministic correctness.
        self._aliases = learned_aliases
        # P2 seam: an optional LLM chooser collapses the fallback candidate
        # list to one verified pick. Default NoChooser = D5 propose-only.
        from resolution.chooser import NoChooser
        self._chooser = chooser if chooser is not None else NoChooser()

    def resolve(self, text: str, *, customer: str | None = None) -> Resolution:
        """Spanning wrapper around the resolution body. The span is no-op
        unless tracing is initialized; it records only structured outcome
        attributes (state/source/confidence/tenant/catalog-version), never
        the raw input text — that is content and would be redacted anyway."""
        with tracer.start_as_current_span('resolve.turn') as sp:
            set_attr(sp, 'svc.task', 'resolve')
            set_attr(sp, 'svc.tenant_id', self._catalog.tenant_id())
            set_attr(sp, 'svc.catalog_version', self._version)
            res = self._resolve(text, customer=customer)
            set_attr(sp, 'svc.outcome', res.state)
            set_attr(sp, 'svc.source', res.source)
            set_attr(sp, 'svc.confidence', res.confidence)
            return res

    def _resolve(self, text: str, *, customer: str | None = None) -> Resolution:
        result = translate(text, catalog=self._catalog, memory=self._memory,
                           customer=customer)
        flags: list[str] = []
        if result.proprietary_violation:
            flags.append('proprietary_violation')

        if result.state == RESOLVED:
            needs_review = result.confidence != 'high' or bool(flags)
            if needs_review and result.confidence != 'high':
                flags.append(f'non_high_confidence:{result.confidence}')
            return Resolution(
                state=RESOLVED, sku=result.sku,
                source=f'translator:{result.source}',
                confidence=result.confidence, flags=tuple(flags),
                needs_review=needs_review, candidates=(),
                catalog_version=self._version,
                tenant_id=self._catalog.tenant_id(), raw_input=text,
                proprietary_warning=result.proprietary_warning,
                reasoning=result.reasoning,
            )

        if result.state == PENDING_DISAMBIGUATION:
            cands = tuple(
                Candidate(sku=c.sku, reason=c.reason, source='translator',
                          differing_fields=tuple(getattr(c, 'differing_fields', ())),
                          description=str(getattr(c, 'description', '') or ''))
                for c in (result.candidates or [])
            )
            oq = tuple(
                OpenQuestion(field=a.type, reason=a.reason,
                             options=tuple(str(x) for x in (a.candidates or [])))
                for a in (result.open_questions or [])
            )
            flags.append('pending_disambiguation')
            return Resolution(
                state=PENDING_DISAMBIGUATION, sku=None,
                source=f'translator:{result.source}',
                confidence=result.confidence or 'medium', flags=tuple(flags),
                needs_review=True, candidates=cands,
                catalog_version=self._version,
                tenant_id=self._catalog.tenant_id(), raw_input=text,
                proprietary_warning=result.proprietary_warning,
                reasoning=result.reasoning, open_questions=oq,
            )

        # Translator says UNRESOLVABLE. First consult learned aliases (gated
        # corrections from the continuous-improvement loop). alias_for returns
        # ACTIVE aliases only with their resolution_mode. Never-invent: the
        # alias target is re-validated as a real catalog row before it resolves.
        if self._aliases is not None:
            alias_result = self._aliases.alias_for(text)
            if alias_result is not None:
                alias_sku, alias_mode = alias_result
                if self._catalog.is_canonical(alias_sku):
                    conf = 'high' if alias_mode == 'auto_silent' else 'medium'
                    review = alias_mode != 'auto_silent'
                    return Resolution(
                        state=RESOLVED, sku=alias_sku, source='learned_alias',
                        confidence=conf,
                        flags=('learned_alias',) if not review else ('learned_alias', 'needs_readback'),
                        needs_review=review, candidates=(),
                        catalog_version=self._version,
                        tenant_id=self._catalog.tenant_id(), raw_input=text)

        # -> retrieval fallback.
        retrieved = self._retriever.candidates(text)
        if retrieved:
            flags.append('retrieval_fallback')
            # P2: an LLM chooser may collapse the candidates to one pick. The
            # pick is bind-guarded to the candidate set (chooser.py), so the
            # never-invent guarantee holds even through a hallucinating model.
            # Default NoChooser returns None -> D5 propose-only behavior.
            picked = self._chooser.choose(text, retrieved)
            if picked is not None:
                chosen = next(c for c in retrieved if c.sku == picked)
                return Resolution(
                    state=RESOLVED, sku=picked,
                    source='retrieval:llm_chooser', confidence='medium',
                    flags=tuple(flags + ['llm_chosen']), needs_review=True,
                    candidates=(), catalog_version=self._version,
                    tenant_id=self._catalog.tenant_id(), raw_input=text,
                    reasoning=f'chooser selected {picked} from {len(retrieved)} '
                              f'candidates: {chosen.description}',
                )
            return Resolution(
                state=PENDING_DISAMBIGUATION, sku=None,
                source='retrieval:bm25', confidence='low',
                flags=tuple(flags), needs_review=True,
                candidates=tuple(
                    Candidate(sku=c.sku,
                              reason=f'bm25 score {c.score}: {c.description}',
                              source='retrieval:bm25',
                              description=c.description)
                    for c in retrieved
                ),
                catalog_version=self._version,
                tenant_id=self._catalog.tenant_id(), raw_input=text,
            )

        flags.append('no_candidates')
        return Resolution(
            state=UNRESOLVABLE, sku=None, source='none', confidence='none',
            flags=tuple(flags), needs_review=True, candidates=(),
            catalog_version=self._version,
            tenant_id=self._catalog.tenant_id(), raw_input=text,
        )
