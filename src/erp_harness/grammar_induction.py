"""Tenant-agnostic SKU grammar induction — learn a NEW catalog's nomenclature.

C4's `analyze_items` runs the *known* the catalog grammar (`part_number_parser`)
over an items entity. That only works for a catalog whose grammar we already
wrote. This module is the other half: pointed at an UNKNOWN tenant's items, it
*infers* the SKU grammar from the strings themselves — the first result that
decodes a large fraction of an unfamiliar catalog before any human effort, then
documents every inference as a reviewable assumption and asks targeted
questions about what it could not resolve.

It deliberately reuses the techniques proven on the catalog decoder, applied
generically rather than with hardcoded patterns:

  - segmentation into typed runs (alpha / digit / separator) ....... part_number_parser
  - family-by-leading-prefix grouping (the K / BH / M / L / VB / CP idea) .. part_number_parser
  - per-family regex induction (one structural template per family) ...... part_number_parser
  - positional role semantics — size / finish / length / sequence ....... constructor (inverse)
  - separator/case normalization before grouping ........................ normalizer

Architecture spine (same as the rest of the harness): this module PROPOSES.
Nothing it emits is a fact — every family, every segment role is an
`Assumption(status='proposed')` carrying its evidence and a confidence, for a
human SME to confirm or correct. Rules + human review bind; induction only
nominates. An optional LLM `RoleProposer` can label segments the deterministic
correlations leave unknown, but it too only proposes — the evidence and the
human gate are unchanged.

The decode runs as an iterative loop: round 0 is pure structural induction;
later rounds propagate high-confidence clues to families that share a shape
("segment 2 was a diameter in family K, so test that hypothesis in family M
which has the same shape"). The loop stops at diminishing returns and hands the
residual — the alphanumeric families it could not crack — to manual
decodification, ranked by how many SKUs each remaining family would unlock.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from erp_harness.catalog_decode import SMEQuestion

# --- tunables (named, not magic) ------------------------------------------------
MIN_FAMILY_MEMBERS = 5        # below this a prefix is too sparse to call a family
MASK_DOMINANCE = 0.60         # dominant shape must cover this share of a family
STRONG_MEMBERS = 8            # member count at which family confidence saturates
CORRELATION_MIN = 0.60        # min co-occurrence share to assign a role directly
PROPAGATION_DISCOUNT = 0.70   # confidence kept when a role is borrowed cross-family
DIMINISHING_GAIN = 0.01       # stop iterating when a round adds less than this
MAX_ROUNDS = 5
CLASSIFIER_MIN_PURITY = 0.70  # a classifier segment's value->evidence-token purity
CLASSIFIER_MAX_VALUES = 20    # a classifier partitions into few groups, not many
DOMAIN_CAP = 40               # enumerate a segment's value set up to this many distinct
# Generic tokens that are never the discriminating term in evidence text.
_EVIDENCE_STOP = frozenset({
    'FITS', 'FIT', 'SERIES', 'SMALL', 'BIG', 'CAM', 'AND', 'WITH', 'FOR', 'THE',
    'PART', 'PARTS', 'STYLE', 'TYPE', 'MODEL', 'NEW', 'ASSEMBLY', 'KIT', 'SET',
    'DIRECT', 'REPLACEMENT', 'REPLACES', 'CONTINUED', 'APPLICATION',
    'INFORMATION', 'WORLD', 'AMERICAN', 'GENUINE', 'OEM',
})
_EVIDENCE_WORD = re.compile(r'[A-Z][A-Z0-9]{3,}')

_TOKEN = re.compile(r'[A-Za-z]+|[0-9]+|[^A-Za-z0-9]+')
_SIZE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:"|inch|in\b|od\b|id\b|mm\b|dia\b)')
_LENGTH_CUE = re.compile(r'\b(?:long|length|lg|overall)\b')
_FINISH_WORDS = {
    'chrome': 'C', 'black': 'B', 'aluminized': 'A', 'aluminised': 'A',
    'raw': 'R', 'stainless': 'S', 'polished': 'P', 'zinc': 'Z', 'galvanized': 'G',
}


# --- data model (everything an SME reviews) -------------------------------------

@dataclass(frozen=True)
class Segment:
    text: str
    kind: str   # 'alpha' | 'digit' | 'sep'


@dataclass(frozen=True)
class SegmentRole:
    position: int          # index into the family's dominant-shape segments
    kind: str              # 'alpha' | 'digit'
    role: str              # family|diameter|length|finish|sequence|classifier|code|unknown
    confidence: float
    evidence: str
    proposed_by: str = 'correlation'   # correlation | propagation | llm
    # For a 'classifier' role: the decoded value->meaning map mined from an
    # evidence field (e.g. 902->CUMMINS, 901->CATERPILLAR). Empty otherwise.
    mapping: tuple = ()
    # The observed value set for a low-cardinality segment (sorted, capped) — so
    # an unresolved code segment surfaces its enumerated domain for the SME
    # ('pos 3 takes {05, 08, 17, ...}') instead of an open-ended question.
    value_domain: tuple = ()


@dataclass(frozen=True)
class FamilyHypothesis:
    family_code: str
    shape_mask: str               # the grammar signature, e.g. 'AN-NA'
    regex: str                    # induced, anchored
    member_count: int
    structural_coverage: float    # share of family members the regex matches
    segment_roles: tuple[SegmentRole, ...]
    example_skus: tuple[str, ...]
    confidence: float

    @property
    def fully_roled(self) -> bool:
        """True when every non-family segment has a non-unknown role."""
        return all(r.role != 'unknown' for r in self.segment_roles
                   if r.role != 'family')


@dataclass(frozen=True)
class Assumption:
    kind: str            # 'family' | 'segment_role'
    statement: str
    evidence: str
    confidence: float
    status: str = 'proposed'   # never 'confirmed' until a human says so


@dataclass(frozen=True)
class DecodeRound:
    index: int
    structured_share: float    # rows in a confident family
    roled_share: float         # rows in a family that is also fully role-labelled
    gain: float                # roled_share delta vs previous round


@dataclass(frozen=True)
class CatalogGrammarReport:
    total_items: int
    families: tuple[FamilyHypothesis, ...]
    assumptions: tuple[Assumption, ...]
    sme_questions: tuple[SMEQuestion, ...]
    rounds: tuple[DecodeRound, ...]
    structured_share: float
    roled_share: float            # share of SKUs in a FULLY role-labelled family
    diminishing_returns: bool
    residual_recommendation: str
    segment_coverage: float = 0.0  # share of (member-weighted) segments role-labelled
    # Nested family codes (parent prefix, child) — e.g. ('A','AR'), ('R','RW') —
    # surfacing a likely family hierarchy for the SME to organize.
    family_hierarchy: tuple = ()


# --- the LLM seam (proposes only; deterministic default is a no-op) -------------

class RoleProposer(Protocol):
    name: str
    def propose(self, family_code: str, shape_mask: str,
                unknown_positions: tuple[int, ...],
                samples: tuple[tuple[str, str], ...]) -> dict: ...


class NoRoleProposer:
    """Default: the deterministic correlation pass is the whole story; no model.
    Returns no extra labels, so CI runs the induction with zero model calls."""
    name = 'none'

    def propose(self, family_code, shape_mask, unknown_positions, samples):
        return {}


_ROLE_SCHEMA = {
    'type': 'object',
    'properties': {'roles': {'type': 'array', 'items': {
        'type': 'object',
        'properties': {
            'position': {'type': 'integer'},
            'role': {'enum': ['diameter', 'length', 'finish', 'sequence',
                              'code', 'variant']},
        },
        'required': ['position', 'role'],
        'additionalProperties': False}}},
    'required': ['roles'],
    'additionalProperties': False,
}


class LLMRoleProposer:
    """Production seam: an LLM labels segments the correlation pass left
    'unknown', reading family example SKUs + their descriptions. It only
    PROPOSES — `_llm_fill` floors the confidence and marks the role
    `proposed_by='llm'`, and the human SME gate still binds. Drop-in for messy
    real tenants; CI runs `NoRoleProposer` and never calls a model.

    Routed at the 'catalog_decode_role' task tier (see model_provider.routing).
    """
    name = 'llm_v1'

    def __init__(self, llm) -> None:
        self._llm = llm

    def propose(self, family_code: str, shape_mask: str,
                unknown_positions: tuple, samples: tuple) -> dict:
        from model_provider import ModelUnavailable
        examples = '\n'.join(f'  {sku}  ::  {desc}' for sku, desc in samples)
        try:
            resp = self._llm.propose(
                task='catalog_decode_role',
                system=('You label positions in a SKU grammar. The SKU shape is '
                        'given as a mask (A=letters, N=digits, separators '
                        'literal). For each unknown position, propose what it '
                        'encodes using ONLY the evidence in the examples. If '
                        'unsure, omit it.'),
                user=(f'Family {family_code!r}, shape {shape_mask!r}.\n'
                      f'Unknown positions: {list(unknown_positions)}\n'
                      f'Examples (sku :: description):\n{examples}'),
                json_schema=_ROLE_SCHEMA, max_tokens=512)
        except ModelUnavailable:
            return {}
        data = resp.data or {}
        return {r['position']: r['role'] for r in data.get('roles', [])
                if r.get('position') in unknown_positions}


# --- primitives (segmentation / shape / regex) ----------------------------------

def normalize_sku(sku: str) -> str:
    """Canonicalize before grouping (normalizer's job, generically): uppercase,
    strip surrounding whitespace. Internal separators are preserved — they are
    part of the grammar we are trying to learn, not noise to discard."""
    return str(sku or '').strip().upper()


def segment(sku: str) -> tuple[Segment, ...]:
    """Split a SKU into maximal typed runs: alpha, digit, or separator."""
    out: list[Segment] = []
    for m in _TOKEN.finditer(normalize_sku(sku)):
        t = m.group()
        kind = ('alpha' if t[0].isalpha()
                else 'digit' if t[0].isdigit() else 'sep')
        out.append(Segment(text=t, kind=kind))
    return tuple(out)


def shape_mask(segs: tuple[Segment, ...]) -> str:
    """The grammar signature: alpha->A, digit->N, separators kept literally.
    Two SKUs with the same mask share a structural template."""
    parts = []
    for s in segs:
        parts.append('A' if s.kind == 'alpha'
                     else 'N' if s.kind == 'digit' else s.text)
    return ''.join(parts)


def family_code(segs: tuple[Segment, ...]) -> str | None:
    """The leading alpha run is the family code (the K / BH / M / L idea)."""
    return segs[0].text if segs and segs[0].kind == 'alpha' else None


def _induce_regex(members: list[tuple[Segment, ...]]) -> str:
    """Build an anchored regex from same-shape members using observed per-
    position length ranges. Reuses the per-family-template idea generically."""
    by_pos: dict[int, list[Segment]] = defaultdict(list)
    for segs in members:
        for i, s in enumerate(segs):
            by_pos[i].append(s)
    parts = ['^']
    for i in sorted(by_pos):
        col = by_pos[i]
        kind = col[0].kind
        if kind == 'sep':
            parts.append(re.escape(col[0].text))
            continue
        lengths = [len(s.text) for s in col]
        lo, hi = min(lengths), max(lengths)
        cls = '[A-Z]' if kind == 'alpha' else r'\d'
        parts.append(f'{cls}{{{lo}}}' if lo == hi else f'{cls}{{{lo},{hi}}}')
    parts.append('$')
    return ''.join(parts)


# --- role inference (the analysis phase) ----------------------------------------

def _digit_role(values_descs: list[tuple[str, str]]) -> tuple[str, float, str]:
    """Hypothesize a digit segment's role by correlating its value with the
    description: a number echoed as a size ('4 inch', '5\"') is a dimension;
    high-cardinality with no echo is a running sequence."""
    n = len(values_descs)
    size_hits = length_hits = 0
    for val, desc in values_descs:
        d = desc.lower()
        nums = set(_SIZE_RE.findall(d))
        try:
            as_num = str(int(val))
        except ValueError:
            as_num = val
        if any(num.lstrip('0') == as_num.lstrip('0') or num == val
               for num in nums):
            if _LENGTH_CUE.search(d):
                length_hits += 1
            else:
                size_hits += 1
    if n and size_hits / n >= CORRELATION_MIN:
        return ('diameter', round(size_hits / n, 3),
                f'value echoed as a size in {size_hits}/{n} rows')
    if n and length_hits / n >= CORRELATION_MIN:
        return ('length', round(length_hits / n, 3),
                f'value echoed near a length cue in {length_hits}/{n} rows')
    cardinality = len({v for v, _ in values_descs})
    if n and cardinality / n >= 0.8:
        return ('sequence', round(cardinality / n, 3),
                f'high cardinality ({cardinality} distinct in {n}), no '
                f'description echo — looks like a running number')
    return ('code', 0.3 if n else 0.0,
            f'low-cardinality digit ({cardinality} distinct), no size echo')


def _alpha_role(values_descs: list[tuple[str, str]]) -> tuple[str, float, str]:
    """Hypothesize a non-prefix alpha segment's role: a single letter that
    tracks a finish word in the description ('C' with 'chrome') is a finish
    code; otherwise it is an opaque variant code."""
    n = len(values_descs)
    finish_hits = 0
    for val, desc in values_descs:
        d = desc.lower()
        for word, code in _FINISH_WORDS.items():
            if word in d and val.upper().startswith(code):
                finish_hits += 1
                break
    if n and finish_hits / n >= CORRELATION_MIN:
        return ('finish', round(finish_hits / n, 3),
                f'segment letter matched a finish word in {finish_hits}/{n} rows')
    return ('variant', 0.3 if n else 0.0,
            'alpha segment with no finish-word correlation — opaque variant code')


def _classifier_role(value_evidence: list[tuple[str, str]]
                     ) -> tuple[str, float, str, tuple]:
    """Hypothesize a 'classifier' segment: a low-cardinality code whose value
    PARTITIONS rows into groups that each map consistently to a *discriminating*
    token in an evidence field (e.g. SKU position 902/901/903 ->
    CUMMINS/CATERPILLAR/DETROIT in fitment/section). Resolves segments with no
    clue in the short description but explained by another captured field — the
    WA engine-line / category case. Returns (role, confidence, evidence, mapping).

    Key subtlety: tokens that appear in EVERY group (e.g. 'DIRECT',
    'REPLACEMENT') carry no signal and are filtered by group-document-frequency,
    leaving the actually-discriminating term. Purity = fraction of a group's rows
    whose evidence contains its dominant discriminating token."""
    groups: dict[str, list[set]] = defaultdict(list)
    for val, evidence in value_evidence:
        toks = {t for t in _EVIDENCE_WORD.findall((evidence or '').upper())
                if t not in _EVIDENCE_STOP}
        groups[val].append(toks)
    distinct = len(groups)
    if not (2 <= distinct <= CLASSIFIER_MAX_VALUES):
        return ('', 0.0, '', ())
    # group-document-frequency: in how many groups does a token appear at all.
    gdf: Counter = Counter()
    for rows in groups.values():
        present = set().union(*rows) if rows else set()
        for t in present:
            gdf[t] += 1
    mapping, purities = [], []
    for val, rows in groups.items():
        counts: Counter = Counter()
        for toks in rows:
            for t in toks:
                if gdf[t] < distinct:          # drop tokens common to all groups
                    counts[t] += 1
        if not counts:
            return ('', 0.0, '', ())
        # Deterministic pick: most frequent, then rarest across groups (most
        # discriminating), then alphabetical. (most_common alone is hash-order
        # flaky on ties because the per-row token sets iterate unordered.)
        top = min(counts, key=lambda t: (-counts[t], gdf[t], t))
        purities.append(counts[top] / len(rows))
        mapping.append((val, top))
    # Must be near-injective: most groups map to DISTINCT tokens. A segment
    # where 8/10 values collapse to the same token isn't classifying by it
    # (that's the WA category-vs-brand false positive).
    distinct_tokens = len({t for _, t in mapping})
    if distinct_tokens < max(2, round(0.6 * distinct)):
        return ('', 0.0, '', ())
    purity = sum(purities) / len(purities)
    if purity < CLASSIFIER_MIN_PURITY:
        return ('', 0.0, '', ())
    mapping.sort()
    shown = ', '.join(f'{v}->{t}' for v, t in mapping[:5])
    return ('classifier', round(purity, 3),
            f'value partitions rows by a discriminating evidence token '
            f'(purity {purity:.0%}): {shown}', tuple(mapping))


def _infer_roles(members: list[tuple[Segment, ...]],
                 descs: list[str], evidences: list[str]) -> list[SegmentRole]:
    """Per dominant-shape position, infer a role. Position 0 (the family prefix)
    is 'family'. Each segment first tries description correlation (size/finish);
    if that's inconclusive and evidence fields are present, it tries the
    classifier mode (value -> evidence-token mapping)."""
    roles: list[SegmentRole] = []
    width = len(members[0])
    has_evidence = any(e.strip() for e in evidences)
    for i in range(width):
        kind = members[0][i].kind
        if kind == 'sep':
            continue
        if i == 0 and kind == 'alpha':
            roles.append(SegmentRole(i, kind, 'family', 1.0,
                                     'leading alpha run = family code'))
            continue
        vd = [(members[r][i].text, descs[r]) for r in range(len(members))]
        if kind == 'digit':
            role, conf, ev = _digit_role(vd)
        else:
            role, conf, ev = _alpha_role(vd)
        mapping: tuple = ()
        if conf < CORRELATION_MIN and has_evidence:
            ve = [(members[r][i].text, evidences[r])
                  for r in range(len(members))]
            crole, cconf, cev, cmap = _classifier_role(ve)
            if crole:
                role, conf, ev, mapping = crole, cconf, cev, cmap
        if conf < CORRELATION_MIN:
            role = 'unknown'
        # Enumerate the value domain for low-cardinality segments — turns an
        # opaque code into a documented finite set the SME can annotate.
        values = sorted({members[r][i].text for r in range(len(members))})
        domain = tuple(values) if 1 < len(values) <= DOMAIN_CAP else ()
        roles.append(SegmentRole(i, kind, role, conf, ev, mapping=mapping,
                                 value_domain=domain))
    return roles


# --- family induction -----------------------------------------------------------

def _induce_family(code: str, rows: list[tuple[str, str, str]]
                   ) -> FamilyHypothesis | None:
    """rows = [(sku, desc, evidence)] all sharing the family prefix `code`."""
    segged = [(segment(sku), desc, ev) for sku, desc, ev in rows]
    mask_hist: Counter[str] = Counter(shape_mask(s) for s, _, _ in segged)
    dominant, dom_n = mask_hist.most_common(1)[0]
    dominance = dom_n / len(segged)
    members = [s for s, _, _ in segged if shape_mask(s) == dominant]
    member_descs = [d for s, d, _ in segged if shape_mask(s) == dominant]
    member_evid = [e for s, _, e in segged if shape_mask(s) == dominant]
    regex = _induce_regex(members)
    rx = re.compile(regex)
    matched = sum(1 for s in members
                  if rx.match(''.join(seg.text for seg in s)))
    structural_coverage = matched / len(members)
    roles = _infer_roles(members, member_descs, member_evid)
    conf = round(dominance * min(1.0, dom_n / STRONG_MEMBERS), 3)
    return FamilyHypothesis(
        family_code=code, shape_mask=dominant, regex=regex,
        member_count=dom_n, structural_coverage=round(structural_coverage, 3),
        segment_roles=tuple(roles),
        example_skus=tuple(''.join(seg.text for seg in m) for m in members[:5]),
        confidence=conf)


def _is_confident(fam: FamilyHypothesis) -> bool:
    return (fam.member_count >= MIN_FAMILY_MEMBERS
            and fam.confidence >= MASK_DOMINANCE * (MIN_FAMILY_MEMBERS
                                                    / STRONG_MEMBERS))


# --- clue propagation (earlier phases feed later phases) ------------------------

def _propagate(families: list[FamilyHypothesis]) -> list[FamilyHypothesis]:
    """Borrow high-confidence role hypotheses to same-shape families that left a
    position 'unknown'. A diameter learned in one family is a testable clue for
    every family sharing that shape — the value of decoding incrementally."""
    learned: dict[tuple[str, int], tuple[str, float, str]] = {}
    for fam in families:
        for r in fam.segment_roles:
            if r.role not in ('unknown', 'family') and r.confidence >= CORRELATION_MIN:
                key = (fam.shape_mask, r.position)
                if key not in learned or r.confidence > learned[key][1]:
                    learned[key] = (r.role, r.confidence, fam.family_code)
    out: list[FamilyHypothesis] = []
    for fam in families:
        new_roles = []
        changed = False
        for r in fam.segment_roles:
            key = (fam.shape_mask, r.position)
            if r.role == 'unknown' and key in learned:
                role, src_conf, src_fam = learned[key]
                new_roles.append(SegmentRole(
                    r.position, r.kind, role,
                    round(src_conf * PROPAGATION_DISCOUNT, 3),
                    f'propagated from family {src_fam!r} sharing shape '
                    f'{fam.shape_mask!r}', proposed_by='propagation'))
                changed = True
            else:
                new_roles.append(r)
        out.append(fam if not changed
                   else _replace_roles(fam, tuple(new_roles)))
    return out


def _replace_roles(fam: FamilyHypothesis,
                   roles: tuple[SegmentRole, ...]) -> FamilyHypothesis:
    return FamilyHypothesis(
        family_code=fam.family_code, shape_mask=fam.shape_mask, regex=fam.regex,
        member_count=fam.member_count,
        structural_coverage=fam.structural_coverage, segment_roles=roles,
        example_skus=fam.example_skus, confidence=fam.confidence)


# --- assumptions, questions, residual -------------------------------------------

def _assumptions(families: list[FamilyHypothesis]) -> list[Assumption]:
    out: list[Assumption] = []
    for fam in families:
        out.append(Assumption(
            kind='family',
            statement=(f'Family {fam.family_code!r}: SKUs follow shape '
                       f'{fam.shape_mask!r} (regex {fam.regex}).'),
            evidence=(f'{fam.member_count} members, '
                      f'{fam.structural_coverage:.0%} match the induced regex; '
                      f'e.g. {", ".join(fam.example_skus[:3])}'),
            confidence=fam.confidence))
        for r in fam.segment_roles:
            if r.role == 'family':
                continue
            out.append(Assumption(
                kind='segment_role',
                statement=(f'Family {fam.family_code!r} position {r.position} '
                           f'({r.kind}) encodes {r.role!r}.'),
                evidence=f'{r.evidence} [{r.proposed_by}]',
                confidence=r.confidence))
    return out


def _sme_questions(confident: list[FamilyHypothesis],
                   residual: list[tuple[str, int, list[str]]]) -> list[SMEQuestion]:
    """One question per thing a human must resolve, ranked by SKUs unlocked:
    (1) residual families with no confident structure, (2) confidently-shaped
    families that still have an 'unknown' segment after propagation."""
    qs: list[SMEQuestion] = []
    for code, count, examples in residual:
        qs.append(SMEQuestion(
            question=(f'Family {code!r} ({count} SKUs) has no dominant structure '
                      f'the decoder could fit. How should {code!r} part numbers '
                      f'be read? Answering resolves {count} SKUs.'),
            skus_resolved=count, example_skus=tuple(examples[:5])))
    for fam in confident:
        for r in fam.segment_roles:
            if r.role == 'unknown':
                domain = (f' It takes {len(r.value_domain)} values '
                          f'{{{", ".join(r.value_domain[:12])}'
                          f'{", ..." if len(r.value_domain) > 12 else ""}}} — '
                          f'what does each mean?' if r.value_domain else '')
                qs.append(SMEQuestion(
                    question=(f'In family {fam.family_code!r} (shape '
                              f'{fam.shape_mask!r}), the {r.kind} at position '
                              f'{r.position} (e.g. {fam.example_skus[0]}) does '
                              f'not correlate with the description.{domain} What does it '
                              f'encode? Answering resolves {fam.member_count} '
                              f'SKUs.'),
                    skus_resolved=fam.member_count,
                    example_skus=fam.example_skus))
    qs.sort(key=lambda q: (-q.skus_resolved, q.question))
    return qs


# --- the iterative decode -------------------------------------------------------

def decode_catalog(rows: list[dict], *, sku_field: str, description_field: str,
                   evidence_fields: list[str] | None = None,
                   role_proposer: RoleProposer | None = None
                   ) -> CatalogGrammarReport:
    """Infer the SKU grammar of an unknown catalog and report it as reviewable
    assumptions + ranked SME questions, iterating until diminishing returns.

    `evidence_fields` are extra row keys (e.g. 'fitment', 'section', 'oem') whose
    text is used as correlation evidence beyond the short description — this is
    what lets a code segment resolve as a 'classifier' (value -> evidence-token
    mapping, e.g. WA 902->CUMMINS) instead of an unanswerable SME question."""
    proposer = role_proposer or NoRoleProposer()
    evidence_fields = evidence_fields or []
    total = len(rows)
    by_family: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    unfamiliar: list[str] = []   # SKUs with no leading alpha prefix
    for row in rows:
        sku = normalize_sku(row.get(sku_field))
        desc = str(row.get(description_field) or '')
        evidence = ' '.join(str(row.get(f) or '') for f in evidence_fields)
        code = family_code(segment(sku))
        if code is None:
            unfamiliar.append(sku)
        else:
            by_family[code].append((sku, desc, evidence))

    # Round 0 — structural induction (the first result).
    families = [f for f in (_induce_family(c, r) for c, r in by_family.items())
                if f is not None]
    confident0 = [f for f in families if _is_confident(f)]
    structured_rows = sum(f.member_count for f in confident0)
    structured_share = structured_rows / total if total else 0.0

    def roled_share(fams: list[FamilyHypothesis]) -> float:
        confident = [f for f in fams if _is_confident(f)]
        roled = sum(f.member_count for f in confident if f.fully_roled)
        return roled / total if total else 0.0

    rounds: list[DecodeRound] = []
    prev = roled_share(families)
    rounds.append(DecodeRound(0, round(structured_share, 3), round(prev, 3),
                              round(prev, 3)))

    diminishing = False
    for i in range(1, MAX_ROUNDS):
        families = _propagate(families)
        # Optional LLM pass: label segments still unknown after propagation.
        families = _llm_fill(families, by_family, proposer,
                             description_field, sku_field)
        cur = roled_share(families)
        gain = cur - prev
        rounds.append(DecodeRound(i, round(structured_share, 3),
                                  round(cur, 3), round(gain, 3)))
        prev = cur
        if gain < DIMINISHING_GAIN:
            diminishing = True
            break

    confident = [f for f in families if _is_confident(f)]
    confident.sort(key=lambda f: -f.member_count)
    residual = sorted(
        ((c, len(r), [s for s, _, _ in r])
         for c, r in by_family.items()
         if not any(f.family_code == c and _is_confident(f) for f in families)),
        key=lambda t: -t[1])

    final_roled = roled_share(families)

    # Finer than roled_share: member-weighted fraction of non-family segments
    # that got a role (so a family with 2 of 3 segments decoded still counts).
    seg_num = seg_den = 0
    for f in confident:
        nonfam = [r for r in f.segment_roles if r.role != 'family']
        seg_den += f.member_count * len(nonfam)
        seg_num += f.member_count * sum(1 for r in nonfam if r.role != 'unknown')
    segment_coverage = seg_num / seg_den if seg_den else 0.0

    recommendation = _recommendation(structured_share, final_roled, residual,
                                     unfamiliar)

    # Family hierarchy: a confident family code that prefixes another (K -> KW).
    codes = sorted(f.family_code for f in confident)
    hierarchy = tuple((a, b) for a in codes for b in codes
                      if a != b and b.startswith(a))

    return CatalogGrammarReport(
        total_items=total,
        families=tuple(confident),
        assumptions=tuple(_assumptions(confident)),
        sme_questions=tuple(_sme_questions(confident, residual)),
        rounds=tuple(rounds),
        structured_share=round(structured_share, 3),
        roled_share=round(final_roled, 3),
        diminishing_returns=diminishing,
        residual_recommendation=recommendation,
        segment_coverage=round(segment_coverage, 3),
        family_hierarchy=hierarchy)


def _llm_fill(families, by_family, proposer, description_field, sku_field):
    """Let an LLM RoleProposer label still-unknown segments (proposes only;
    confidence is floored and the human gate is unchanged). No-op by default."""
    if getattr(proposer, 'name', 'none') == 'none':
        return families
    out = []
    for fam in families:
        unknown_pos = tuple(r.position for r in fam.segment_roles
                            if r.role == 'unknown')
        if not unknown_pos:
            out.append(fam)
            continue
        samples = tuple((s, d) for s, d, _ in by_family.get(fam.family_code, [])[:12])
        try:
            labels = proposer.propose(fam.family_code, fam.shape_mask,
                                      unknown_pos, samples) or {}
        except Exception:
            labels = {}
        if not labels:
            out.append(fam)
            continue
        new_roles = []
        for r in fam.segment_roles:
            lab = labels.get(r.position) or labels.get(str(r.position))
            if r.role == 'unknown' and lab:
                new_roles.append(SegmentRole(
                    r.position, r.kind, str(lab),
                    min(0.5, CORRELATION_MIN), 'LLM proposal (unverified)',
                    proposed_by='llm'))
            else:
                new_roles.append(r)
        out.append(_replace_roles(fam, tuple(new_roles)))
    return out


def _recommendation(structured: float, roled: float,
                    residual: list, unfamiliar: list) -> str:
    if structured >= 0.99 and roled >= 0.99:
        return ('Grammar fully decoded by induction; route assumptions to SME '
                'for confirmation, no manual decodification needed.')
    resid_skus = sum(c for _, c, _ in residual)
    top = ', '.join(f'{code} ({n})' for code, n, _ in residual[:5])
    parts = [
        f'Induction decoded {structured:.0%} of SKUs structurally and '
        f'{roled:.0%} fully (all segments role-labelled).']
    if residual:
        parts.append(
            f'Diminishing returns reached with {len(residual)} families '
            f'({resid_skus} SKUs) unresolved — hand these to manual '
            f'decodification, largest first: {top}.')
    if unfamiliar:
        parts.append(f'{len(unfamiliar)} SKUs have no alpha prefix and need a '
                     f'separate convention review.')
    return ' '.join(parts)
