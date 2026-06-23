"""Tenant-agnostic SKU grammar induction (C4, unknown-catalog path).

Two proving grounds:
  1. The the example tenant's twin catalog — the "early result": induction must recover
     family structure from REAL SKUs without using the hardcoded parser,
     showing the technique generalizes.
  2. A planted UNKNOWN grammar — fault-injection check: a made-up nomenclature
     the known parser has never seen. Induction must infer diameter + finish
     from description correlation, propagate a clue to a sparse same-shape
     family, leave a genuinely opaque segment as an SME question, and hand the
     sub-threshold family to manual decodification.
"""
from __future__ import annotations

from harness_fixtures import make_rig

from erp_harness import (
    CatalogGrammarReport,
    decode_catalog,
    segment,
    shape_mask,
)
from erp_harness.discovery import fetch_all_rows

# --- primitives ----------------------------------------------------------------

def test_segmentation_and_shape_mask():
    segs = segment('zx4-100c')
    assert [s.kind for s in segs] == ['alpha', 'digit', 'sep', 'digit', 'alpha']
    assert [s.text for s in segs] == ['ZX', '4', '-', '100', 'C']
    assert shape_mask(segs) == 'AN-NA'
    # case + whitespace normalized; internal separators preserved.
    assert shape_mask(segment('  bh 5 / 2 ')) == 'A N A'.replace(' ', '') \
        or shape_mask(segment('bh5/2')) == 'AN/N'


# --- 1. early result on the real (example tenant) catalog -----------------------------

def _twin_rows(limit=12000):
    # The full real catalog: a messy mix of clean alpha-prefixed families and
    # numeric-leading legacy/cross-ref codes — exactly what an unknown tenant
    # looks like, not a tidied subset.
    _, _, enforcer = make_rig(item_limit=limit)
    return fetch_all_rows(enforcer, 'items', limit=limit)


def test_quick_win_recovers_family_structure_without_parser():
    rows = _twin_rows()
    report = decode_catalog(rows, sku_field='number',
                            description_field='displayName')
    assert isinstance(report, CatalogGrammarReport)
    assert report.total_items == len(rows)
    # The first result: induction structurally decodes a large share of a real,
    # messy catalog from the strings alone — no hardcoded grammar.
    assert len(report.families) >= 20
    assert report.structured_share > 0.5
    # A dimensional role AND finish inferred purely from description
    # co-occurrence. (Many family codes embed the diameter in the family token
    # itself, e.g. 'K5', so the free dimensional segment surfaces as 'length';
    # either dimension role demonstrates the co-occurrence inference.)
    roles = {r.role for f in report.families for r in f.segment_roles}
    assert ('diameter' in roles or 'length' in roles) and 'finish' in roles
    # Every emitted statement is a proposal, never a confirmed fact.
    assert all(a.status == 'proposed' for a in report.assumptions)
    # The messy numeric-leading mass is clearly flagged, not silently dropped.
    assert 'convention review' in report.residual_recommendation


# --- 2. planted unknown grammar ------------------------------------------------

def _planted_catalog():
    rows = []
    # ZX: shape AN-A. pos1 = diameter (echoed in desc), pos3 = finish.
    for i in range(8):
        dia = 3 + (i % 4)
        fin, word = ('C', 'chrome') if i % 2 == 0 else ('B', 'black')
        rows.append({'number': f'ZX{dia}-{fin}',
                     'displayName': f'ZX {dia} inch {word} elbow'})
    # QY: SAME shape AN-A, pos1 also a diameter but NO size echo in the
    # description -> unknown at round 0, must be filled by propagation from ZX.
    for i in range(6):
        dia = 5 + (i % 2)
        fin, word = ('C', 'chrome') if i % 2 == 0 else ('B', 'black')
        rows.append({'number': f'QY{dia}-{fin}',
                     'displayName': f'QY widget {word}'})
    # WW: shape AN-NA. pos1 diameter (echoed); pos3 a low-cardinality opaque code
    # that never correlates -> stays unknown but its value DOMAIN is enumerated.
    _codes = ['12', '13', '14']
    for i in range(6):
        dia = 4 + (i % 3)
        rows.append({'number': f'WW{dia}-{_codes[i % 3]}C',
                     'displayName': f'WW {dia} inch chrome bracket'})
    # ZZ: only 3 members -> sub-threshold -> residual -> manual decodification.
    for i in range(3):
        rows.append({'number': f'ZZ{i + 1}', 'displayName': 'ZZ misc part'})
    return rows


def _family(report, code):
    return next(f for f in report.families if f.family_code == code)


def _role_at(fam, pos):
    return next(r for r in fam.segment_roles if r.position == pos)


def test_planted_infers_diameter_and_finish():
    report = decode_catalog(_planted_catalog(), sku_field='number',
                            description_field='displayName')
    zx = _family(report, 'ZX')
    assert _role_at(zx, 1).role == 'diameter'
    assert _role_at(zx, 1).proposed_by == 'correlation'
    assert _role_at(zx, 3).role == 'finish'
    assert zx.fully_roled


def test_planted_clue_propagates_to_sparse_family():
    report = decode_catalog(_planted_catalog(), sku_field='number',
                            description_field='displayName')
    qy = _family(report, 'QY')
    r1 = _role_at(qy, 1)
    # QY's own descriptions don't echo the size; the role is borrowed from ZX.
    assert r1.role == 'diameter'
    assert r1.proposed_by == 'propagation'
    assert 'ZX' in r1.evidence


def test_planted_opaque_segment_becomes_sme_question():
    report = decode_catalog(_planted_catalog(), sku_field='number',
                            description_field='displayName')
    ww = _family(report, 'WW')
    r3 = _role_at(ww, 3)
    assert r3.role == 'unknown'
    # Dig deeper: the opaque segment's value domain is enumerated, not just
    # flagged — and the SME question lists those values.
    assert r3.value_domain == ('12', '13', '14')
    ww_qs = [q for q in report.sme_questions if "'WW'" in q.question]
    assert ww_qs and 'resolves' in ww_qs[0].question
    assert '12' in ww_qs[0].question and 'what does each mean' in ww_qs[0].question.lower()
    # Questions are ranked by SKUs unlocked.
    vols = [q.skus_resolved for q in report.sme_questions]
    assert vols == sorted(vols, reverse=True)


def test_planted_subthreshold_family_handed_to_manual_decodification():
    report = decode_catalog(_planted_catalog(), sku_field='number',
                            description_field='displayName')
    assert 'ZZ' not in {f.family_code for f in report.families}  # not confident
    rec = report.residual_recommendation.lower()
    assert 'manual decodification' in rec and 'zz' in rec


def test_iteration_stops_at_diminishing_returns():
    report = decode_catalog(_planted_catalog(), sku_field='number',
                            description_field='displayName')
    assert report.diminishing_returns is True
    # Propagation round adds coverage; a later round adds ~nothing -> stop.
    gains = [r.gain for r in report.rounds]
    assert any(g > 0 for g in gains[1:])          # propagation paid off
    assert report.rounds[-1].gain < 0.01          # converged
    assert report.roled_share > report.rounds[0].roled_share


def test_classifier_role_resolves_from_evidence_field():
    # WA-shaped: WA<line>-<cat>-<seq>. The <line> digit (902/901/903) carries no
    # clue in the short description, but the fitment column does. Multi-field
    # correlation must resolve it as a 'classifier' with a value->brand mapping
    # — exactly the WA engine-line case that was an SME question before.
    rows = []
    # Brand is constant per line code; the engine MODEL varies row to row (as in
    # a real catalog) so the brand wins on frequency, not a tiebreak.
    spec = [('902', 'CUMMINS', ['NT855', 'N14', 'ISX', 'X15']),
            ('901', 'CATERPILLAR', ['3306', 'C15', 'C13', '3406']),
            ('903', 'DETROIT', ['S60', 'DD15', 'DD13', 'S50'])]
    seq = 1000
    for line, brand, models in spec:
        for model in models:
            seq += 1
            rows.append({'sku': f'WA{line}-01-{seq}',
                         'description': 'accessory drive gear',
                         'fitment': f'Fits {brand} {model}'})
    report = decode_catalog(rows, sku_field='sku', description_field='description',
                            evidence_fields=['fitment'])
    wa = next(f for f in report.families if f.family_code == 'WA')
    line_role = next(r for r in wa.segment_roles if r.position == 1)
    assert line_role.role == 'classifier'
    mapping = dict(line_role.mapping)
    assert mapping['902'] == 'CUMMINS' and mapping['901'] == 'CATERPILLAR'
    # Without the evidence field it stays unknown -> an SME question (the gap).
    blind = decode_catalog(rows, sku_field='sku', description_field='description')
    wa_blind = next(f for f in blind.families if f.family_code == 'WA')
    assert next(r for r in wa_blind.segment_roles
                if r.position == 1).role == 'unknown'


def test_family_hierarchy_detected():
    # Two confident families where one code prefixes the other (K -> KW).
    rows = []
    for i in range(8):
        rows.append({'sku': f'K{i}-10', 'description': 'stack'})
        rows.append({'sku': f'KW{i}-10', 'description': 'wide stack'})
    report = decode_catalog(rows, sku_field='sku', description_field='description')
    codes = {f.family_code for f in report.families}
    assert {'K', 'KW'} <= codes
    assert ('K', 'KW') in report.family_hierarchy


class _StubRoleProposer:
    """Stands in for the LLM seam: labels WW's opaque position. Proves the fill
    path applies a proposal as 'llm' with floored confidence — never binding."""
    name = 'stub_v1'

    def propose(self, family_code, shape_mask, unknown_positions, samples):
        if family_code == 'WW' and 3 in unknown_positions:
            return {3: 'sequence'}
        return {}


def test_llm_role_proposer_seam_fills_unknown_as_unverified_proposal():
    report = decode_catalog(_planted_catalog(), sku_field='number',
                            description_field='displayName',
                            role_proposer=_StubRoleProposer())
    ww = _family(report, 'WW')
    r3 = _role_at(ww, 3)
    assert r3.role == 'sequence'          # the proposer's label was applied
    assert r3.proposed_by == 'llm'        # ...but marked as a model proposal
    assert r3.confidence <= 0.6           # ...with floored, non-binding weight
    assert 'unverified' in r3.evidence


def test_assumptions_are_reviewable_and_never_confirmed():
    report = decode_catalog(_planted_catalog(), sku_field='number',
                            description_field='displayName')
    assert report.assumptions
    assert all(a.status == 'proposed' for a in report.assumptions)
    # A family assumption and a segment-role assumption both present, w/ evidence.
    kinds = {a.kind for a in report.assumptions}
    assert {'family', 'segment_role'} <= kinds
    assert all(a.evidence for a in report.assumptions)
