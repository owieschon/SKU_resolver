#!/usr/bin/env python3
"""Generate the synthetic demo catalog by enumerating the part-number grammar.

This catalog is SYNTHETIC BY CONSTRUCTION: every parametric SKU is produced by
enumerating valid (family, diameter, length, body, finish) combinations and the
elbow / muffler / clamp builders, then kept only if it round-trips
(``construct(extract(sku)) == sku``). Nothing is copied from any real catalog.
A small set of deliberately-invented opaque accessory codes is added so the
verbatim resolution path (non-grammar SKUs) is still exercised.

Columns are the eight the engine reads. Prices/sales/quantities are random
(seeded) and carry no real commercial meaning. Run from the repo root:

    python scripts/generate_catalog.py

Writes data/catalog.csv. Regenerate inventory and the round-trip baseline after:

    python scripts/generate_inventory.py
    python scripts/roundtrip_audit.py
"""
from __future__ import annotations

import csv
import random
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src'))

from sku_translator.extractor import PartSpec, extract_spec
from sku_translator.constructor import construct_sku
from sku_translator.part_number_parser import parse

# Enumeration grids. Generous candidate sets; only combos that round-trip
# through the grammar survive, so invalid family/finish/body mixes drop out.
PARAMETRIC = ['K', 'BH', 'BR', 'A', 'SS', 'SK', 'D', 'S', 'SP', 'M',
              'ZP', 'ZM', 'T', 'CP', 'CN', 'Y', 'SL', 'CSP', 'BT']
DIAMETERS = [3, 3.5, 4, 5, 6, 7, 8]
LENGTHS = [12, 18, 24, 30, 36, 42, 48, 54, 60, 72, 84, 96]
BODIES = ['SB', 'EX', 'XB']
FINISHES = ['C', 'A', 'SC', 'S3', 'S']

# Every angle the extractor's ELBOW_ANGLE_TO_PREFIX map knows, so the catalog
# covers the full mapping (no angle in the constant that the catalog lacks).
ELBOW_ANGLES = [15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 120]
ELBOW_DIAMS = [4, 5, 6]

# SKUs the docs/tests reference: always present, customer-facing, priced, not
# obsolete (so demos resolve deterministically regardless of random category
# assignment).
DEMO_CLEAN = {
    'K5-24SBC', 'K5-30SBC', 'K5-36SBC', 'K5-24EXC', 'BH5-36SBC',
    'L590-1515SC', 'R5-4C', 'R6-5C', 'VB-5C', 'VB-6C', 'A5-36SBC',
    'RC-2', 'RC-3', 'RC-400',
}
# (17,15) deliberately omitted so L590-1715SC stays a "phantom" (parses but not
# stocked) for the fuzzy-correction demo/test.
ELBOW_LEGS = [(12, 12), (15, 15), (18, 18), (22, 18), (24, 24)]

REDUCER_PAIRS = [(4, 3), (5, 4), (6, 5), (7, 5), (7, 6), (8, 6)]

# Private-label / customer-program SKUs (parser decodes a proprietary_customer
# from these patterns). Synthetic instances of the recognised program shapes.
PROPRIETARY_CANDIDATES = [
    '548CPL', '548CPU', '548DP', '888EXC', '888SBC',
    'SW-K5C', 'SW-K6C', 'SW-S5EXC', 'SW-BH6C',
    'ES-430PC', 'ES-431PC', 'ES-432PLC',
    'UCS524ENCP', 'UCS624PECP', 'UCS525ENCP', 'UCS626ENCP',
]

# Category labels (not sensitive — generic group codes). A few OBSOLETE/BATTERY
# rows exercise the exclusion filter; PROPRIETAR exercises the proprietary flag.
CUSTOMER_PGC = ['PIPE', 'CHROME', 'CLAMP', 'ELBOW', 'MUFFLER', 'HARDWARE']


def roundtrips(sku: str) -> bool:
    try:
        return construct_sku(extract_spec(sku)) == sku
    except Exception:
        return False


def _num(v) -> str:
    """5.0 -> '5', 3.5 -> '3.5'."""
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


def finish_word(fin: str) -> str:
    return {'C': 'chrome', 'A': 'aluminized', 'SC': 'aluminized',
            'S3': '304 stainless', 'S': 'stainless'}.get(fin, fin.lower())


def describe(sku: str) -> str:
    p = parse(sku)
    fam = p.get('family_meaning') or 'exhaust component'
    bits = []
    if p.get('diameter'):
        # "<n> inch" (not <n>") so the grammar-induction co-occurrence engine
        # can tie the diameter segment to a number echoed in the description.
        bits.append(f'{_num(p["diameter"])} inch')
    if p.get('finish'):
        bits.append(finish_word(str(p['finish'])))
    bits.append(str(fam).lower())
    if p.get('length'):
        bits.append(f'{_num(p["length"])} inch long')
    s = ' '.join(b for b in bits if b).strip()
    return (s[:1].upper() + s[1:]) if s else 'Industrial exhaust component'


def main() -> int:
    rng = random.Random(20260622)
    skus: list[str] = []
    seen: set[str] = set()

    def add(sku: str):
        if sku and sku not in seen:
            seen.add(sku)
            skus.append(sku)

    # Families kept fully (demo/disambiguation tests depend on dense coverage);
    # the rest are sampled so the catalog has organic gaps like a real one.
    KEEP_FULL = {'K', 'A'}  # kept full so K disambiguation + 'aussie' (A) demos resolve

    # Parametric families
    for fam in PARAMETRIC:
        for d in DIAMETERS:
            for ln in LENGTHS:
                for body in BODIES:
                    for fin in FINISHES:
                        if fam not in KEEP_FULL and rng.random() > 0.38:
                            continue
                        spec = PartSpec(family=fam, diameter=d, length=ln,
                                        body=body, finish=fin)
                        try:
                            sku = construct_sku(spec)
                        except Exception:
                            continue
                        if roundtrips(sku):
                            add(sku)

    # Elbows
    for d in ELBOW_DIAMS:
        for ang in ELBOW_ANGLES:
            for l1, l2 in ELBOW_LEGS:
                for fin in ['C', 'A']:
                    for body in ['SB']:
                        spec = PartSpec(family='L', diameter=d, angle=ang,
                                        leg1=l1, leg2=l2, body=body, finish=fin)
                        try:
                            sku = construct_sku(spec)
                        except Exception:
                            continue
                        if roundtrips(sku):
                            add(sku)

    # CM mufflers
    for ln in LENGTHS:
        for fin in ['C', 'A']:
            spec = PartSpec(family='CM', length=ln, finish=fin)
            try:
                sku = construct_sku(spec)
            except Exception:
                continue
            if roundtrips(sku):
                add(sku)

    # Reducers
    for din, dout in REDUCER_PAIRS:
        for fin in ['C', 'A', 'S3']:
            spec = PartSpec(family='R', inlet_diameter=din,
                            outlet_diameter=dout, finish=fin)
            try:
                sku = construct_sku(spec)
            except Exception:
                continue
            if roundtrips(sku):
                add(sku)

    grammar_count = len(skus)

    # Private-label SKUs (verbatim path; carry a decoded proprietary_customer)
    proprietary = set()
    for cand in PROPRIETARY_CANDIDATES:
        if parse(cand).get('proprietary_customer'):
            add(cand)
            proprietary.add(cand)

    # Deliberately-invented opaque accessory codes (verbatim path only — these
    # do NOT decode to the grammar, mirroring the messy non-parametric rows any
    # real ERP carries). Invented, not copied.
    for i in range(300):
        add(f'AX{rng.randint(1000, 9999)}-{rng.randint(1, 9)}')
    for i in range(120):
        add(f'HW-{rng.randint(10000, 99999)}')

    # Named accessories with free-text descriptions (no grammar): these exercise
    # the BM25 description-retrieval fallback for queries the grammar can't parse
    # (e.g. "rain cap", "vee band clamp").
    NAMED_ACCESSORIES = {
        'RC-2': '5" rain cap exhaust', 'RC-3': '6" rain cap exhaust',
        'RC-400': '4" rain cap stainless', 'VB-5C': '5" vee band clamp stainless',
        'VB-6C': '6" vee band clamp stainless', 'VB-4C': '4" vee band clamp',
        'HCLAMP-5': '5" exhaust clamp', 'HCLAMP-6': '6" exhaust clamp',
        'BRKT-AHS': 'exhaust hanger bracket', 'GASKET-5': '5" exhaust gasket',
    }
    for s in NAMED_ACCESSORIES:
        add(s)

    # 'K5-24SBC' is the catalog flagship in the docs/demos — make it the
    # clear sales leader so popularity-ranked disambiguation is deterministic.
    FLAGSHIP = 'K5-24SBC'

    # Candidates for "K 5 chrome SB" (K family, 5", SB body, C finish, any
    # length). Give them a healthy, bounded sales band so the flagship leads but
    # never dominates 3:1 (which would auto-resolve instead of pend).
    k5sbc = re.compile(r'^K5-\d+SBC$')

    # Build rows
    rows = []
    for sku in skus:
        if sku in DEMO_CLEAN:
            pgc, ipg = (rng.choice(CUSTOMER_PGC), 'PIPE')
        elif sku in NAMED_ACCESSORIES:
            pgc, ipg = 'CLAMP', 'PIPE'
        elif sku in proprietary:
            pgc, ipg = 'PROPRIETAR', 'PIPE'
        else:
            # ~3% proprietary, ~3% obsolete/battery (exercise filters)
            roll = rng.random()
            if roll < 0.03:
                pgc, ipg = rng.choice(['PROPRIETAR', 'CHROME']), 'OBSOLETE'
            elif roll < 0.05:
                pgc, ipg = 'BATTERY', 'BATTERY'
            elif roll < 0.08:
                pgc, ipg = 'PROPRIETAR', 'PIPE'
            else:
                pgc, ipg = rng.choice(CUSTOMER_PGC), 'PIPE'
        priced = sku in DEMO_CLEAN or sku in NAMED_ACCESSORIES or rng.random() > 0.08
        if sku == FLAGSHIP:
            sales = 900
        elif k5sbc.match(sku):
            sales = rng.randint(350, 600)
        else:
            sales = rng.randint(1, 600) if rng.random() > 0.18 else 0
        rows.append({
            'No.': sku,
            'Description': NAMED_ACCESSORIES.get(sku) or describe(sku),
            'Product Group Code': pgc,
            'Inventory Posting Group': ipg,
            'Sales (Qty.)': sales,
            'Sales (Qty.) - Year': rng.randint(0, 120),
            'Quantity on Hand': rng.randint(0, 400),
            'Unit Price': round(rng.uniform(12, 480), 2) if priced else 0.0,
        })

    out = REPO / 'data' / 'catalog.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f'wrote {out.relative_to(REPO)}: {len(rows)} rows '
          f'({grammar_count} grammar, {len(rows) - grammar_count} opaque)')
    # demo SKUs the docs/tests rely on
    for s in ['K5-24SBC', 'K5-36SBC', 'BH5-36SBC', 'BH7-32EXA', 'CM-56C']:
        print(f'  {s:10} present={s in seen}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
