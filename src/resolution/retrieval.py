"""BM25 candidate retrieval over a tenant's catalog — the fallback layer.

Scope decision D5 (docs/DECISION_LOG.md): in this slice the retrieval layer
PROPOSES candidates only; it can never produce a RESOLVED result. The
production-validated architecture puts an LLM chooser over a K=25 hybrid
(BM25 + dense) pool — 88.2% conditional selection accuracy in the locked
2026-05-02 experiment record. The chooser and the dense retriever are
deliberately absent here (no model calls in this repo's CI), so the fallback
surfaces a ranked human-disambiguation list instead. BM25 carries the
exact-token recall that made the hybrid work (it is the component that
recovered K4-12SBA when dense embeddings conflated close SKU variants).

Every candidate is, by design, a row from the tenant's own catalog —
the index is built from catalog rows and returns only what it was built
from. The adversarial suite verifies this from the outside anyway.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

from sku_translator.catalog_index import CatalogIndex

_TOKEN = re.compile(r'[a-z0-9]+')

# Conversational function words. A caller's prose ("you just told me about
# availability and lead time") was matching product descriptions on incidental
# words ("and" -> "MUFFLER AND CONNECTOR", "time" -> "NOT SELLING AT THIS TIME"),
# surfacing nonsense candidates. Dropping these from BOTH the query and the
# corpus means retrieval keys on part-bearing tokens, not English filler.
_STOPWORDS = frozenset("""
a an the and or but so of to in on at for with from by is are was were be been
being do does did have has had i me my we our you your it its this that these
those he she they them his her their there here what when where which who whom
why how can could would should will shall may might must not no yes ok okay
about any some all just now already still yet too very really please thanks
thank hi hey hello looking want need get got tell told said say also like one
""".split())

def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


@dataclass(frozen=True)
class RetrievedCandidate:
    sku: str
    description: str
    score: float


class BM25CatalogRetriever:
    """One retriever per tenant catalog — built from, and answering only
    from, a single CatalogIndex instance (tenant isolation lives at this
    boundary, same as the translator's)."""

    def __init__(self, catalog: CatalogIndex, *, top_k: int = 10) -> None:
        self._tenant_id = catalog.tenant_id()
        self._top_k = top_k
        self._skus: list[str] = []
        self._descriptions: list[str] = []
        corpus: list[list[str]] = []
        all_rows = list(catalog.parsed_rows())
        # Only SUGGEST customer-facing catalog parts (tenant-001 ~3,000-SKU scope:
        # real, priced, non-proprietary, non-custom products). Other rows stay in
        # the catalog for an exact lookup but are never offered. Fallback: a
        # catalog that carries NO customer-facing metadata (e.g. a synthetic test
        # tenant or an alternate loader) indexes all its rows rather than nothing.
        scoped = [r for r in all_rows if r.is_customer_facing]
        index_rows = scoped if scoped else all_rows
        for row in index_rows:
            self._skus.append(row.sku)
            self._descriptions.append(row.description or '')
            corpus.append(_tokenize(f'{row.sku} {row.description or ""}'))
        self._bm25 = BM25Okapi(corpus)

    def tenant_id(self) -> str:
        return self._tenant_id

    def candidates(self, text: str) -> list[RetrievedCandidate]:
        tokens = _tokenize(text)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(range(len(scores)), key=lambda i: (-scores[i], self._skus[i]))
        out = []
        for i in ranked[: self._top_k]:
            if scores[i] <= 0.0:
                break  # below this point BM25 has no token overlap at all
            out.append(RetrievedCandidate(
                sku=self._skus[i],
                description=self._descriptions[i],
                score=round(float(scores[i]), 4),
            ))
        return out
