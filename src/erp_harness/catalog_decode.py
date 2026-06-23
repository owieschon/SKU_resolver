"""C4 — Item-Master Catalog Decode Module.

The agent-executed form of the catalog-decoding methodology: run the SKU
grammar over the discovered items entity, report pattern-family structure,
surface candidate family-word vocabulary from descriptions, and emit the
SME question list ordered by volume-of-SKUs-resolved.

Output shape maps onto the vocabulary-spec pipeline (FAMILY_WORD_ALIASES):
each vocabulary candidate is (phrase, family_code, evidence). The planted-
fault E2E seeds the twin with a synthetic family and asserts the candidate
surfaces — the module must *find* vocabulary, not echo known aliases.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from sku_translator.part_number_parser import parse as parse_sku

_WORD = re.compile(r'[a-z]{3,}')
# Generic catalog words that can never be family-discriminating vocabulary.
_STOPWORDS = frozenset({
    'the', 'and', 'with', 'for', 'inch', 'long', 'chrome', 'steel',
    'stainless', 'black', 'pack', 'each', 'rev',
})
_NON_STRUCTURAL = frozenset({
    'unstructured', 'empty', 'legacy_undocumented', 'freetext_or_admin',
    'family_numeric',
})


@dataclass(frozen=True)
class VocabularyCandidate:
    phrase: str
    family_code: str
    support: int          # rows where phrase co-occurs with the family
    distinctiveness: float  # share of the phrase's occurrences in this family


@dataclass(frozen=True)
class SMEQuestion:
    question: str
    skus_resolved: int
    example_skus: tuple[str, ...]


@dataclass(frozen=True)
class GrammarReadinessReport:
    total_items: int
    decoded: int
    family_histogram: dict[str, int]
    vocabulary_candidates: tuple[VocabularyCandidate, ...]
    sme_questions: tuple[SMEQuestion, ...]


def analyze_items(rows: list[dict], *, sku_field: str,
                  description_field: str) -> GrammarReadinessReport:
    family_hist: Counter[str] = Counter()
    fam_words: dict[str, Counter[str]] = defaultdict(Counter)
    word_totals: Counter[str] = Counter()
    undecoded: list[str] = []
    decoded = 0

    for row in rows:
        sku = str(row.get(sku_field) or '')
        desc = str(row.get(description_field) or '')
        result = parse_sku(sku)
        pattern = result.get('pattern')
        if pattern in _NON_STRUCTURAL or not result.get('family'):
            undecoded.append(sku)
            continue
        decoded += 1
        fam = result['family']
        family_hist[fam] += 1
        for w in set(_WORD.findall(desc.lower())) - _STOPWORDS:
            fam_words[fam][w] += 1
            word_totals[w] += 1

    candidates = []
    for fam, words in fam_words.items():
        for w, n in words.most_common(5):
            distinct = n / word_totals[w]
            # Vocabulary must be family-discriminating, not catalog-generic.
            if n >= 3 and distinct >= 0.8:
                candidates.append(VocabularyCandidate(
                    phrase=w, family_code=fam, support=n,
                    distinctiveness=round(distinct, 3)))
    candidates.sort(key=lambda c: (-c.support, c.phrase))

    questions = []
    if undecoded:
        # Cluster undecoded SKUs by leading token; one SME question each,
        # ordered by the volume of SKUs an answer would resolve.
        clusters: dict[str, list[str]] = defaultdict(list)
        for sku in undecoded:
            clusters[re.split(r"[\s-]", sku, maxsplit=1)[0][:6] or '?'].append(sku)
        for prefix, members in sorted(clusters.items(),
                                      key=lambda kv: -len(kv[1])):
            questions.append(SMEQuestion(
                question=(f'SKUs starting {prefix!r} do not decode under the '
                          f'current grammar. What does this prefix mean, and '
                          f'do these share structure? Confirming resolves '
                          f'{len(members)} SKUs.'),
                skus_resolved=len(members),
                example_skus=tuple(members[:5])))

    return GrammarReadinessReport(
        total_items=len(rows), decoded=decoded,
        family_histogram=dict(family_hist),
        vocabulary_candidates=tuple(candidates),
        sme_questions=tuple(questions))
