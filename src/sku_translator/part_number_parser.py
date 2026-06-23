"""Industrial part-number parser (v3.5).

Decodes canonical SKUs into structured fields. The translator's extractor
uses this for full-SKU pass-through; the constructor uses the same grammar
in reverse. The two together must satisfy the round-trip property:

    parse(construct(extract(sku))) == sku    for any catalog SKU.

v3.5 changes from v3.4:
    - Elbow regex: finish accepts S3/S4/S4S; angle list expanded to all
      multiples of 5 from 5° to 180° plus 96°; decimal legs (17.02) handled;
      diameter no longer greedy.
    - custom_review pattern tolerates trailing "REV A" / "REV. B".
    - Pattern definitions reordered for correct precedence on edge cases.

The parser is intentionally permissive about what it returns. Each pattern
emits a dict with whatever fields it could extract; the extractor maps these
into PartSpec attributes. Patterns that can't decode return None.
"""
from __future__ import annotations

import re
from typing import Any

# ============================================================================
# Lookup tables
# ============================================================================

FAMILY_MEANINGS = {
    'K':   'Curved-top stack',
    'BH':  'Bullhorn stack',
    'BR':  'Brute stack',
    'A':   'Aussie-style stack',
    'WCK': 'West Coast Curve stack',
    'SS':  'Straight stack',
    'SP':  'Spool / mitre',
    'SK':  'Curved variant (chrome)',
    'S':   'Straight tube / raw stock',
    'M':   'Muffler',
    'ZP':  'Modern pipe series',
    'ZM':  'Modern muffler series',
    'D':   'Dump stack',
    'L':   'Elbow',
    'P':   'Pipe',
    'R':   'Reducer',
    'CM':  'Chrome Muffler',
    'PG':  'Pipe Guard',
    'MG':  'Muffler Guard',
    'AHS': 'Aerodynamic Heat Shield',
    'UHS': 'Universal Heat Shield',
    'SMB': 'Stainless Mounting Bracket',
    'DSS': 'Durable Stainless Steel flex hose',
    'H':   'Hanger',
    'PRK': 'Peterbilt Retrofit Kit',
    'EZ':  'EZ Seal clamp',
    'GRIEZ': 'EZ Seal clamp',
    '2K':  '2K bulk-pack tube',
    '50':  'Custom 50-series',
    '548': 'Apex Diesel 548 program',
    '777': 'Apex Diesel 777 series',
    '888': 'Apex Diesel 888 series',
    'MARMON': 'Marmon flange/flare',
    'CBS': 'Freightliner kit (CBS)',
}

FINISH_MEANINGS = {
    'A':   'Aluminized',
    'C':   'Chrome',
    'P':   'At Plater (WIP)',
    'S':   'Stainless / black',
    'S3':  '304 stainless steel',
    'S4':  '409 stainless steel',
    'S4S': '409 stainless steel (modifier S)',
    'BS':  'Black Series',
    'R':   'Raw / Cold-rolled',
}

BODY_MEANINGS = {
    'SB': 'Straight Bottom (OD-fit)',
    'EX': 'Expanded (ID-fit)',
    'XB': 'Variant',
}

OEM_MEANINGS = {
    'KW': 'Kenworth', 'PB': 'Peterbilt', 'FL': 'Freightliner',
    'IH': 'International', 'MK': 'Mack', 'VG': 'Volvo',
    'WS': 'Western Star', 'FT': 'Ford Truck', 'GM': 'General Motors',
    'PETE': 'Peterbilt',
}

PARAMETRIC_FAMILIES = {
    'K', 'BH', 'BR', 'A', 'WCK', 'D', 'CSP', 'BT', 'DTS', 'EXS',
    'S', 'M', 'ZP', 'ZM', 'T', 'CP', 'CN', 'Y', 'SL', 'P',
    # Note: SS, SK, SP, SBR, SBH, SA, SWCK are NOT atomic. They're
    # S-prefix reducer + base family. See PAT_S_REDUCER below.
}

# Base families that can appear with S-prefix as a reducer.
# Catalog confirms: SA (68), SBH (58), SBR (54), SK (128), SS (84),
# SP (142), SL (35), SWCK (41) — all describe the part as reduced.
S_REDUCIBLE_FAMILIES = ['SWCK', 'WCK', 'BH', 'BR', 'K', 'A', 'D', 'M', 'P',
                        'ZP', 'ZM', 'L', 'S']
# Sort longest-first for greedy regex matching (SWCK before WCK before K)
S_REDUCIBLE_FAMILIES.sort(key=len, reverse=True)


# ============================================================================
# Pattern registry
# ============================================================================

# Each PATTERN entry: (name, regex, decoder_fn). Decoder takes match → dict.
# Order matters: more specific patterns first.

# --- Parametric (the workhorse) -------------------------------------------
# {family}{diameter}-{length}{body}{finish}
# Family: 1-3 letters; Diameter: 1-3 digits with optional .5 etc;
# Length: 1-4 digits with optional decimal; Body: SB/EX/XB; Finish: A/C/P/S/S3/S4/BS
_PARAMETRIC_FAMILY_GROUP = '|'.join(sorted(PARAMETRIC_FAMILIES, key=len, reverse=True))

# --- S-prefix reducer (stack families only, not elbow) --------------------
# {S}{base_family}{inlet_diameter}-{length}{body}{finish}{?-outlet_diameter}
# Examples:
#   SBR6-108EXC      = Brute reducer 6→5 (5 implicit), 108", ID, chrome
#   SBR6-108EXC-5    = same with explicit -5
#   SBR5-14SBC-4     = Brute reducer 5→4, SB OD, chrome
#   SK4-48EXC-3      = Curved reducer 4→3, ID, chrome
#   SS5-48EXC4       = Straight reducer 5→4 (no dash before outlet)
#   SS5-48EXC3.75    = Straight reducer 5→3.75 (decimal outlet)
_S_REDUCIBLE_STACK = sorted(
    [f for f in S_REDUCIBLE_FAMILIES if f != 'L'],
    key=len, reverse=True
)
PAT_S_REDUCER = re.compile(
    rf'^S(?P<base_family>{"|".join(_S_REDUCIBLE_STACK)})'
    r'(?P<inlet>\d+(?:\.\d+)?)'
    r'-'
    r'(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB)'
    # Finish: BBC=Black Chrome, BC=Black Chrome short, BS=Black Series, S3, S4, [ACPS]; or absent (raw)
    r'(?P<finish>BBC|BS|S3|S4|BC|[ACPS])?'
    r'(?:-?(?P<outlet>\d+(?:\.\d+)?)(?P<outlet_unit>ID|OD)?)?$'
)

# --- SL elbow reducer ------------------------------------------------------
# SL{diameter}{angle}-{leg1}{leg2}{?S}{finish}{?-outlet}
PAT_SL_ELBOW_REDUCER = re.compile(
    r'^SL'
    r'(?P<diameter>\d(?:\.\d{1,2})?)'
    r'(?P<angle>180|175|170|165|160|155|150|145|140|135|130|125|120|115|110|105|100|96|95|90|85|80|75|70|65|60|55|50|45|40|35|30|25|20|15|10|5)'
    r'-'
    r'(?P<legs>\d{2,3}\.\d{1,2}\d{2,3}(?:\.\d{1,2})?|\d{2,3}\d{2,3}(?:\.\d{1,2})?|\d{4,6})'
    r'(?P<od_marker>S)?'
    r'(?P<finish>S4S|S3|S4|[ACPSR])'
    r'(?:-?(?P<outlet>\d+(?:\.\d+)?))?$'
)

PAT_PARAMETRIC = re.compile(
    rf'^(?P<family>{_PARAMETRIC_FAMILY_GROUP})'
    r'(?P<diameter>\d+(?:\.\d+)?)'
    r'-'
    r'(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB)'
    # Finish: BBC=Black Chrome (compound), BC=Black Chrome short,
    # SBBC=SB+Black Chrome, BS=Black Series, EXSS=expanded SS,
    # SS=304SS, S3, S4, [ACPRS]; or absent (raw)
    r'(?P<finish>BBC|BS|EXSS|SBBC|SS|S3|S4|BC|[ACPRS])?'
    r'(?:-(?P<modifier>[A-Z0-9.]+))?$'
)

# --- Elbow (with v3.5 fixes) ----------------------------------------------
# L{diameter}{angle}-{leg1}{leg2}{?S-OD-marker}{finish}
# diameter: 1-2 digit, optional .5
# angle: any multiple of 5 from 5 to 180, plus 96
# legs: each is digits, optionally with decimal (17.02)
# finish: S3, S4, S4S, or single letter
#
# v3.4 bugs fixed:
#   - finish only [ACPS]: now accepts S3/S4/S4S
#   - angle restricted to 8 values: now any multiple of 5
#   - decimal leg case (17.02): explicit handling
#   - diameter \d+ greedy: now bounded to 1-2 digit + optional .decimal
PAT_ELBOW = re.compile(
    r'^L'
    # Diameter: catalog uses 1-9 or fractional like 1.5, 1.75, 2.5, 3.5
    r'(?P<diameter>\d(?:\.\d{1,2})?)'
    # Angle: any multiple of 5 from 5 to 180, plus 96 (longest first for greedy)
    r'(?P<angle>180|175|170|165|160|155|150|145|140|135|130|125|120|115|110|105|100|96|95|90|85|80|75|70|65|60|55|50|45|40|35|30|25|20|15|10|5)'
    r'-'
    # Legs: capture as one group; decoder splits.
    # Either 2 decimal-legs concatenated (17.02|15) — handled by alt.
    # Or 4-6 integer digits (1212, 17150, 211204, etc.)
    r'(?P<legs>\d{2,3}\.\d{1,2}\d{2,3}(?:\.\d{1,2})?|\d{2,3}\d{2,3}(?:\.\d{1,2})?|\d{4,6})'
    r'(?P<od_marker>S)?'
    # Finish: S4S, S3, S4, or single letter (A/C/P/S/R)
    r'(?P<finish>S4S|S3|S4|[ACPSR])'
    r'$'
)

# Elbow has a tricky ambiguity: leg1+leg2 are concatenated digits with no
# separator. We need to split them. Strategy: try equal split first
# (most catalog elbows have leg1==leg2 in length), then unequal splits.

# --- Reducer ---------------------------------------------------------------
# R{inlet}-{outlet}{?fit}{finish}
PAT_REDUCER = re.compile(
    r'^R'
    r'(?P<inlet>\d+(?:\.\d+)?)'
    r'-'
    r'(?P<outlet>\d+(?:\.\d+)?)'
    r'(?P<fit>IDID|IDOD|ODID|ODOD)?'
    r'(?P<finish>S3|S4|BS|[ACPS])$'
)

# --- 2ND cosmetic-second prefix --------------------------------------------
# 2ND followed by a parametric SKU; B-grade variant of parent.
PAT_2ND = re.compile(r'^2ND(?P<inner>.+)$')

# --- Hanger ----------------------------------------------------------------
# H-{OEM}{component}{seq}{?finish-or-suffix}
PAT_HANGER = re.compile(
    r'^H-(?P<oem>[A-Z]{2,3})'
    r'(?P<component>[A-Z]?)'
    r'(?P<seq>\d{1,4})'
    r'(?P<suffix>BRKT|RD|[A-Z]?)?$'
)

# --- Apex Diesel (548, 777, 888) -----------------------------------
PAT_PERF_DIESEL = re.compile(
    r'^(?P<program>548|777|888)'
    r'(?P<config>[A-Z]+|\d+)$'
)

PERF_DIESEL_CONFIGS = {
    '548': {
        'CPL': 'Charger Pipe Lower',
        'CPU': 'Charger Pipe Upper',
        'DP':  'Dump Pipe',
    },
}

# --- Marmon flange/flare ---------------------------------------------------
# Restricted to Marmon-specific suffixes (L for long, S for short, DSS for
# Durable Stainless Steel hose, FL for flare, B/WFF/PLT/PLATE variants).
# Generic BOM-child suffixes (-ASSY, -COMPONENT, -MNT, -BRKT) handled by
# PAT_BOM_CHILD instead.
PAT_MARMON = re.compile(
    r'^(?P<base>\d{5,6})'
    r'-(?P<suffix>L|S|DSS|FL|B|WFF|PLT|PLATE)$'
)

# --- Custom 50-series customer parts ---------------------------------------
PAT_CUSTOM_50 = re.compile(
    r'^50(?P<customer>[A-Z]{2,3})(?P<seq>\d{3,4})$'
)

# --- 2K bulk pack ----------------------------------------------------------
PAT_2K = re.compile(r'^2K-(?P<length>\d+)$')

# --- EZ Seal clamps --------------------------------------------------------
PAT_EZ = re.compile(
    r'^EZ-(?P<diameter>\d+)SS(?P<bulk>BK)?(?P<ext>EX)?$'
)

PAT_GRIEZ = re.compile(
    r'^GRIEZ-(?P<diameter>\d+)SS$'
)

# --- CM Chrome Muffler -----------------------------------------------------
PAT_CM = re.compile(
    r'^CM-(?P<length>\d{2,3})(?P<finish>[ACPRS])$'
)

# --- SMB Stainless Mounting Bracket ---------------------------------------
PAT_SMB = re.compile(
    r'^SMB(?:-(?P<finish>[ACR]))?$'
)

# --- DSS flex hose ---------------------------------------------------------
PAT_DSS = re.compile(
    r'^DSS-(?P<diameter>\d)(?P<length>\d{2})$'
)

# --- Guards (PG, MG, AHS, UHS) --------------------------------------------
PAT_GUARD = re.compile(
    r'^(?P<family>PG|MG|AHS|UHS)'
    r'(?:-(?P<config>[A-Z0-9]+))?$'
)

# --- Custom review (21DD, 24DD, 44DD, etc.) -------------------------------
# v3.5 fix: tolerate "REV A" / "REV. B" suffix
PAT_CUSTOM_REVIEW = re.compile(
    r'^(?P<prefix>\d{2})(?P<customer>[A-Z]{2})(?P<seq>\d{3,4})'
    r'(?:\s+REV\.?\s*[A-Z])?$'
)

# --- PRK Peterbilt Retrofit Kit -------------------------------------------
PAT_PRK = re.compile(
    r'^PRK-(?P<inner>.+)$'
)

# --- Hardware passthroughs (McMaster-Carr, 3M, etc.) ----------------------
# Supplier part numbers used directly. Don't decode internals — flag and
# route. acquisition_method='purchased' is auto-set.
PAT_HARDWARE_PASSTHROUGH = re.compile(
    r'^(?P<supplier_prefix>4513K|5245N|7631A|90107A|90108A|9010\dA)'
    r'(?P<supplier_seq>\d+[A-Z]?)$'
)

HARDWARE_SUPPLIERS = {
    '4513K':  'McMaster-Carr (fittings)',
    '5245N':  'McMaster-Carr (plugs)',
    '7631A':  '3M (tape)',
    '90107A': 'McMaster-Carr (washers)',
    '90108A': 'McMaster-Carr (washers)',
}

# --- EXPANDER tools --------------------------------------------------------
PAT_EXPANDER = re.compile(r'^EXPANDER-(?P<diameter>\d+(?:\.\d+)?)$')

# --- MB Mounting Bracket ---------------------------------------------------
# Permissive grammar: catalog has at least 4 format variants
#   MB-{D}{OEM}{material}      MB-7WSS, MB-6WSS
#   MB{width}-{D}{OEM}{material}  MB3-8KWS
#   MB-{OEM}{model}{finish}    MB-PB389C, MB-WS
#   MB-{customer-program}      MB-LL2-...
PAT_MB = re.compile(r'^MB(?P<inner>[-\d][-A-Z0-9.]*)$')

# --- Customer-mirror SKUs (description embeds the canonical SKU) ----------
# 128xxx.304 family — opaque customer SKU + .304 = 304SS suffix.
# The description carries the actual a catalog SKU.
PAT_CUSTOMER_MIRROR_304 = re.compile(
    r'^(?P<base>\d{5,6})\.304$'
)

# Variant: {5digit}E{3digit} — same trick, different customer
PAT_CUSTOMER_MIRROR_E = re.compile(
    r'^(?P<base>\d{5})E(?P<seq>\d{3})$'
)

# --- Brightwater proprietary BOM components ------------------------------------
# 28xxxx with -MF/L/M suffix = machined flange / legs / muffler component.
PAT_BRIGHTWATER_COMPONENT = re.compile(
    r'^(?P<base>28\d{4})(?P<component>MF|M|L)$'
)

BRIGHTWATER_COMPONENT_MEANINGS = {
    'MF': 'Machined Flange',
    'L':  'Legs',
    'M':  'Muffler',
}

# --- Ford engine programs --------------------------------------------------
# 82F = Ford 8.2L; 3208 = Caterpillar 3208 (used in Ford trucks).
PAT_FORD_ENGINE = re.compile(
    r'^(?P<engine>82F|3208)(?P<rest>[A-Z0-9-]*)$'
)

# --- CH customer-specific (proprietary, requires review) ------------------
PAT_CH_CUSTOMER = re.compile(r'^CH-P(?P<seq>\d{3,4})$')

# --- Surplus Reducer (SPR + inlet/outlet ID/OD) ---------------------------
# SPRII, SPRIO, SPROI, SPROO — production overrun, sold from leftover stock
PAT_SURPLUS_REDUCER = re.compile(
    r'^SPR(?P<inlet>[IO])(?P<outlet>[IO])$'
)

# --- Gasket (G-{OEM}{seq}) ------------------------------------------------
# Disambiguator from G{D}-{L} galvanized flex hose: dash position.
PAT_GASKET = re.compile(
    r'^G-(?P<oem>BB|GM|IH|FL|KW|PB)(?P<seq>\d{1,3})$'
)

# --- PowerFlow flex hose (Z02.xxx) ----------------------------------------
# Z{diameter}SS{construction}{?-length}
PAT_POWERFLOW_FLEX = re.compile(
    r'^Z(?P<diameter>\d+\.\d+)SS(?P<construction>PFW|PFHW|[A-Z]+)'
    r'(?:-(?P<length>\d+(?:\.\d+)?)"?)?$'
)

# --- ASD stock display (disregard) ----------------------------------------
PAT_ASD = re.compile(r'^ASD-(?P<rest>.+)$')

# --- PWFL Powerflow apparel/merch (disregard) -----------------------------
PAT_PWFL = re.compile(r'^PWFL-(?P<rest>.+)$')

# --- GR/GRE/GRDPFG branded merch (disregard) ------------------------------
PAT_GR_MERCH = re.compile(
    r'^(?P<line>GR|GRE|GRI|GRDPFG)-(?P<rest>.+)$'
)

# --- Year-range emissions parts (94-97PSCP-3) -----------------------------
PAT_YEAR_RANGE = re.compile(
    r'^(?P<year_start>\d{2})-(?P<year_end>\d{2})'
    r'(?P<application>[A-Z]+)'
    r'(?:-(?P<variant>\d+))?$'
)

# --- Marmon length-suffix variants (41968-L4 etc.) ------------------------
# Extends Marmon to handle -L{N} where N is the length in inches.
PAT_MARMON_L_LENGTH = re.compile(
    r'^(?P<base>\d{5,6})-L(?P<length>\d{1,2})$'
)

# --- Riverton Welding (SW-{embedded-parametric}) --------------------------
# SW-K5C, SW-K5EXC, SW-5, SW-5C — customer prefix + parametric inner
PAT_SW_RIVERTON = re.compile(r'^SW-(?P<inner>.+)$')

# --- Complete Kit ({D}CK-{angle}R{reduction}{OEM}{finish}) ----------------
# 6CK-90R5PBC = 6" complete kit, 90°, R5" reduction, PB Pete, C chrome
PAT_COMPLETE_KIT = re.compile(
    r'^(?P<diameter>\d)CK-(?P<rest>\d+R\d+[A-Z]+)$'
)

# --- Numeric-first parametric ({D}{family}-{L}{body}{finish}) -------------
# Alternative form for SP and NG families: 7SP-55SBC
PAT_PARAMETRIC_NF = re.compile(
    r'^(?P<diameter>\d)(?P<family>SP|NG)-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB)'
    r'(?P<finish>S3|S4|BS|[ACPS])$'
)

# --- Material spec (raw stock) --------------------------------------------
# "1.5 16GA. 304SS" — the SKU is literally the material spec.
PAT_MATERIAL = re.compile(
    r'^(?P<diameter>\d+(?:\.\d+)?)\s*(?P<gauge>\d{1,2})\s*GA\.?\s*'
    r'(?P<alloy>304\s*S\.?S\.?|409\s*S\.?S\.?|ALZ|304SS|409SS)$',
    re.IGNORECASE
)

# --- MISC. BEND placeholder (custom one-off bend orders) ------------------
PAT_MISC_BEND = re.compile(
    r'^MISC\.?\s*BENDS?(?:\s+(?P<diameter>\d+(?:\.\d+)?))?$',
    re.IGNORECASE
)

# --- BOM-child suffixes (-ASSY, -COMPONENT, -MNT, -BRKT) ------------------
# Generic pattern: any parent SKU + -ASSY/-COMPONENT/-MNT/-BRKT suffix.
# Per SME: "-ASSY = kit version of parent, -COMPONENT = component-only,
# -MNT = mounting parts, -BRKT = bracket". Recursively classifies parent.
PAT_BOM_CHILD = re.compile(
    r'^(?P<parent>.+)-(?P<bom_role>ASSY|COMPONENT|MNT|BRKT)$'
)

# --- CBS Freightliner kit -------------------------------------------------
# CBS-FL-ODS-S4 is the kit parent; CBS-FL-ODS-S4{N} are kit components.
# Structurally identical to ZP/ZM kit pattern (parent + numeric component
# index). FL = Freightliner, S4 = 409 stainless. CBS and ODS semantics
# unconfirmed (school-bus IPG suggests school-bus-specific kit).
PAT_CBS_KIT = re.compile(
    r'^CBS-(?P<oem>FL)-(?P<ods>ODS)-S4(?P<component_idx>\d?)$'
)

# --- ZP/ZM kit-component (parent + numeric index) -------------------------
# ZP8536-2 (parent) and ZP8536-21, ZP8536-22 (components).
# Distinguishable from standard parametric ZP12-100EXA: kit form has no
# body+finish trailing letters, just numeric component index.
PAT_Z_KIT = re.compile(
    r'^(?P<family>ZP|ZM)(?P<base>\d{4,5})-(?P<component_idx>\d{1,2})'
    r'(?:\s+REV\.?\s*[A-Z])?$'
)

# --- OEM compound (acronyms, not just numerics) ---------------------------
# KW-ACUP (Kenworth A-Cup crossover), IH-CUP (International Cup), etc.
# Distinguishable from OEM-mirror SKUs by acronym suffix (no leading digit).
PAT_OEM_COMPOUND = re.compile(
    r'^(?P<oem>KW|PB|FL|IH|MK|VG|WS|FT|GM)-'
    r'(?P<compound>[A-Z][A-Z0-9]+)$'
)

# OEM-compound vocabulary (per SME-confirmed cases)
OEM_COMPOUND_MEANINGS = {
    ('KW', 'ACUP'):  'Kenworth A-cup shaped crossover pipe',
    ('IH', 'CUP'):   'International cup shaped crossover pipe',
    ('KW', 'HE18'):  'Kenworth elbow (legacy customer part, deprecated)',
}

# --- Apex Diesel 888EX/777EX (extended Perf Diesel) ----------------
# Already handled by PAT_PERF_DIESEL but make sure 888EX-style with body
# code variants flow through correctly.

# --- Explicit disregard list ----------------------------------------------
# SKUs the SME explicitly said to flag for review with no decoding.
EXPLICIT_DISREGARD_REVIEW = {
    '29772A':       '50 Series EGR DOC/Muffler — needs customer ID',
    '1759SS':       'Special 9" clamp variant — needs customer ID',
    'TCADAPT':      'Auto Jet SPX reducer — third-party',
    '23401-3.5':    '3.5"-to-5" SS flange — supplier code unknown',
}

# --- Freetext / admin SKUs (non-product line items, disregard) ------------
# Pricing adjustments, restocking fees, marketing materials, samples, etc.
FREETEXT_PREFIXES = (
    'PA-',           # Pricing Adjustment
    'RESTOCK',
    'ADD.',
    'ADD ',
    'NPI-',          # NPI test
    'WINDOWSTICKER',
    'CATALOG',
    'BANNER',
    'PROTOTYPE',
    'SIZING',
    'PIPECUT-',
    'SHAPER-',
    'CUTWHEEL',
    'BOLT FOR',
    '12OZ-',
    '2OZ-',
    'POLISH-',
    'HAT',
    'PENS',
    'T-SHIRT',
    'VK-MANUAL',
    'SCHOOLBUSCATALOG',
    'CD\'S',
    'CD ',
    'DISPLAY',
    'CEMENT',
    'EMIS',
)


# ============================================================================
# Decoder helpers
# ============================================================================

def _decode_s_reducer(m: re.Match) -> dict[str, Any]:
    """Decode S{base_family}... reducer SKUs.

    Per SME and catalog: S-prefix means 'reducer'. Inlet diameter is the
    captured number; outlet diameter defaults to 5" if omitted (5" is the
    a standard truck-side exhaust diameter).
    """
    base = m.group('base_family')
    inlet = float(m.group('inlet'))
    outlet_raw = m.group('outlet')

    if outlet_raw is None:
        outlet = 5.0
        outlet_implicit = True
    else:
        outlet = float(outlet_raw)
        # Catalog convention: 35 = 3.5", 45 = 4.5" (compressed decimals)
        # Detect: integer ≥ 10 and < 100 with no decimal point in raw → /10
        if '.' not in outlet_raw and 10 <= outlet <= 99:
            outlet = outlet / 10
        outlet_implicit = False

    return {
        'pattern': 's_reducer',
        'family': base,
        'family_meaning': FAMILY_MEANINGS.get(base, f'{base} (reducing)'),
        'is_reducer': True,
        'inlet_diameter': inlet,
        'diameter': inlet,                # back-compat for consumers
        'outlet_diameter': outlet,
        'outlet_implicit': outlet_implicit,
        'length': float(m.group('length')),
        'body': m.group('body'),
        'body_meaning': BODY_MEANINGS.get(m.group('body')),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
    }


def _decode_sl_elbow_reducer(m: re.Match) -> dict[str, Any]:
    """Decode SL elbow reducer (S-prefix on L family)."""
    legs_raw = m.group('legs')
    leg1, leg2 = _split_legs(legs_raw)

    outlet_raw = m.group('outlet')
    if outlet_raw is None:
        outlet = 5.0
        outlet_implicit = True
    else:
        outlet = float(outlet_raw)
        if '.' not in outlet_raw and 10 <= outlet <= 99:
            outlet = outlet / 10
        outlet_implicit = False

    od = m.group('od_marker') == 'S'
    return {
        'pattern': 'sl_elbow_reducer',
        'family': 'L',
        'family_meaning': 'Elbow (reducing)',
        'is_reducer': True,
        'diameter': float(m.group('diameter')),
        'inlet_diameter': float(m.group('diameter')),
        'outlet_diameter': outlet,
        'outlet_implicit': outlet_implicit,
        'angle': int(m.group('angle')),
        'leg1': leg1,
        'leg2': leg2,
        'body': 'SB' if od else 'EX',
        'body_meaning': BODY_MEANINGS.get('SB' if od else 'EX'),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
    }


def _decode_parametric(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'parametric',
        'family': family,
        'family_meaning': FAMILY_MEANINGS.get(family),
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'body': m.group('body'),
        'body_meaning': BODY_MEANINGS.get(m.group('body')),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
        'modifier': m.group('modifier'),
    }


def _decode_elbow(m: re.Match) -> dict[str, Any]:
    legs_raw = m.group('legs')

    # Split the legs string. Three cases:
    #   1. Decimal in middle: "17.0215" → leg1="17.02", leg2="15"
    #   2. Integer pair: "1212" → leg1=12, leg2=12 (split in half)
    #   3. Asymmetric integer: "21120" or "211204" — try 3+3, 2+4, etc.
    leg1, leg2 = _split_legs(legs_raw)

    od = m.group('od_marker') == 'S'
    return {
        'pattern': 'elbow',
        'family': 'L',
        'family_meaning': 'Elbow',
        'diameter': float(m.group('diameter')),
        'angle': int(m.group('angle')),
        'leg1': leg1,
        'leg2': leg2,
        'body': 'SB' if od else 'EX',
        'body_meaning': BODY_MEANINGS.get('SB' if od else 'EX'),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
    }


def _split_legs(legs: str) -> tuple[float, float]:
    """Split a legs string into (leg1, leg2) floats.

    Handles:
        '1212'      → (12.0, 12.0)
        '1715'      → (17.0, 15.0)
        '2112'      → (21.0, 12.0)
        '17.0215'   → (17.02, 15.0)
        '1715.5'    → (17.0, 15.5)
        '900909'    → (90.0, 909.0) — for unusual catalog edge cases
    """
    # Decimal in middle: '17.0215' or similar
    if '.' in legs:
        # Find the decimal. After the decimal, we have N decimal digits then
        # the start of leg2. Catalog convention: decimals are 1-2 digits.
        dot_idx = legs.index('.')
        # Try 2 decimal digits first (most common: 17.02|15)
        if dot_idx + 3 <= len(legs):
            leg1_str = legs[:dot_idx + 3]
            leg2_str = legs[dot_idx + 3:]
            if leg2_str and not leg2_str.startswith('.'):
                try:
                    return float(leg1_str), float(leg2_str)
                except ValueError:
                    pass
        # Fallback: 1 decimal digit
        if dot_idx + 2 <= len(legs):
            leg1_str = legs[:dot_idx + 2]
            leg2_str = legs[dot_idx + 2:]
            if leg2_str:
                try:
                    return float(leg1_str), float(leg2_str)
                except ValueError:
                    pass
        # Last resort: split at decimal
        return float(legs[:dot_idx]), float(legs[dot_idx + 1:])

    # Pure integer: split in half (catalog convention)
    n = len(legs)
    if n == 2:
        return float(legs[0]), float(legs[1])
    if n == 4:
        return float(legs[:2]), float(legs[2:])
    if n == 6:
        return float(legs[:3]), float(legs[3:])
    # Odd lengths: prefer 2/N-2 split (most catalog elbows have 2-digit leg1)
    return float(legs[:2]), float(legs[2:])


def _decode_reducer(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'reducer',
        'family': 'R',
        'family_meaning': 'Reducer',
        'diameter': float(m.group('inlet')),       # inlet → diameter
        'length': float(m.group('outlet')),        # outlet → length (reused field)
        'fit': m.group('fit'),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
    }


def _decode_2nd(m: re.Match) -> dict[str, Any] | None:
    inner = m.group('inner')
    inner_result = _try_patterns(inner)
    if inner_result is None:
        return None
    inner_result['pattern'] = '2nd_' + inner_result.get('pattern', 'unknown')
    inner_result['cosmetic_second'] = True
    inner_result['parent_sku'] = inner
    return inner_result


def _decode_hanger(m: re.Match) -> dict[str, Any]:
    oem = m.group('oem')
    return {
        'pattern': 'hanger',
        'family': 'H',
        'family_meaning': 'Hanger',
        'oem': oem if oem in OEM_MEANINGS else None,
        'oem_meaning': OEM_MEANINGS.get(oem),
        'hanger_oem': oem,
        'hanger_component': m.group('component'),
        'hanger_seq': m.group('seq'),
        'hanger_suffix': m.group('suffix'),
    }


def _decode_perf_diesel(m: re.Match) -> dict[str, Any]:
    program = m.group('program')
    config = m.group('config')
    config_meaning = PERF_DIESEL_CONFIGS.get(program, {}).get(config)
    return {
        'pattern': 'perf_diesel',
        'family': program,
        'family_meaning': FAMILY_MEANINGS.get(program),
        'config_code': config,
        'config_meaning': config_meaning,
        'is_proprietary': True,
        'proprietary_customer': 'Apex Diesel',
    }


def _decode_marmon(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'marmon',
        'family': 'MARMON',
        'family_meaning': FAMILY_MEANINGS['MARMON'],
        'marmon_base': m.group('base'),
        'marmon_suffix': m.group('suffix'),
    }


def _decode_custom_50(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'custom_50',
        'family': '50',
        'family_meaning': FAMILY_MEANINGS['50'],
        'customer_code': m.group('customer'),
        'seq': m.group('seq'),
        'is_proprietary': True,
    }


def _decode_2k(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'bulk_2k',
        'family': '2K',
        'family_meaning': FAMILY_MEANINGS['2K'],
        'length': float(m.group('length')),
        'disregard': True,
    }


def _decode_ez(m: re.Match) -> dict[str, Any]:
    diameter_raw = m.group('diameter')
    # Diameter encoding: 4 = 4", 35 = 3.5", 225 = 2.25"
    if len(diameter_raw) == 1:
        diameter = float(diameter_raw)
    elif len(diameter_raw) == 2:
        diameter = float(diameter_raw[0]) + 0.5
    else:
        diameter = float(diameter_raw[0]) + float(diameter_raw[1:]) / 100
    return {
        'pattern': 'ez_clamp',
        'family': 'EZ',
        'family_meaning': FAMILY_MEANINGS['EZ'],
        'diameter': diameter,
        'bulk_pack': m.group('bulk') == 'BK',
        'ext_variant': m.group('ext') == 'EX',
    }


def _decode_griez(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'griez_clamp',
        'family': 'GRIEZ',
        'family_meaning': FAMILY_MEANINGS['GRIEZ'],
        'diameter': float(m.group('diameter')),
    }


def _decode_cm(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'cm_muffler',
        'family': 'CM',
        'family_meaning': FAMILY_MEANINGS['CM'],
        'length': float(m.group('length')),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
    }


def _decode_smb(m: re.Match) -> dict[str, Any]:
    finish = m.group('finish') or 'PC'   # default to powder-coat
    return {
        'pattern': 'smb_bracket',
        'family': 'SMB',
        'family_meaning': FAMILY_MEANINGS['SMB'],
        'finish': finish,
    }


def _decode_dss(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dss_flex',
        'family': 'DSS',
        'family_meaning': FAMILY_MEANINGS['DSS'],
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
    }


def _decode_guard(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'guard_variant',
        'family': family,
        'family_meaning': FAMILY_MEANINGS.get(family),
        'config': m.group('config'),
    }


def _decode_custom_review(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'custom_review',
        'family': m.group('customer'),
        'family_meaning': f"Customer-specific ({m.group('customer')})",
        'requires_human_review': True,
        'is_proprietary': True,
        'prefix': m.group('prefix'),
        'customer_code': m.group('customer'),
        'seq': m.group('seq'),
    }


def _decode_prk(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'prk_kit',
        'family': 'PRK',
        'family_meaning': FAMILY_MEANINGS['PRK'],
        'oem': 'PB',
        'oem_meaning': 'Peterbilt',
        'prk_inner': m.group('inner'),
    }


def _decode_hardware_passthrough(m: re.Match) -> dict[str, Any]:
    prefix = m.group('supplier_prefix')
    return {
        'pattern': 'hardware_passthrough',
        'family': 'HW',
        'family_meaning': 'Hardware passthrough',
        'supplier': HARDWARE_SUPPLIERS.get(prefix, 'unknown'),
        'supplier_prefix': prefix,
        'supplier_seq': m.group('supplier_seq'),
        'acquisition_method': 'purchased',
        'disregard_for_decoding': True,
    }


def _decode_expander(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'expander_tool',
        'family': 'EXPANDER',
        'family_meaning': 'Pipe expander tool',
        'diameter': float(m.group('diameter')),
        'is_tool': True,
        'disregard': True,
    }


def _decode_mb(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'mb_bracket',
        'family': 'MB',
        'family_meaning': 'Mounting Bracket',
        'mb_inner': m.group('inner'),
    }


def _decode_customer_mirror_304(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'customer_mirror_304',
        'family': 'CUSTOMER_MIRROR',
        'family_meaning': 'Customer-mirror SKU (description holds canonical)',
        'customer_base': m.group('base'),
        'material': '304 stainless steel',
        'is_proprietary': True,
        'note': 'Description field contains the embedded a catalog SKU',
    }


def _decode_customer_mirror_e(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'customer_mirror_e',
        'family': 'CUSTOMER_MIRROR',
        'family_meaning': 'Customer-mirror SKU (E-suffix variant)',
        'customer_base': m.group('base'),
        'seq': m.group('seq'),
        'is_proprietary': True,
    }


def _decode_brightwater_component(m: re.Match) -> dict[str, Any]:
    component = m.group('component')
    return {
        'pattern': 'brightwater_component',
        'family': 'BRIGHTWATER',
        'family_meaning': 'Brightwater proprietary BOM component',
        'brightwater_base': m.group('base'),
        'component_code': component,
        'component_meaning': BRIGHTWATER_COMPONENT_MEANINGS.get(component),
        'is_proprietary': True,
        'proprietary_customer': 'Brightwater',
    }


def _decode_ford_engine(m: re.Match) -> dict[str, Any]:
    engine = m.group('engine')
    return {
        'pattern': 'ford_engine',
        'family': engine,
        'family_meaning': 'Ford 8.2L engine program' if engine == '82F'
                          else 'Caterpillar 3208 engine (Ford trucks)',
        'oem': 'FT',
        'oem_meaning': 'Ford Truck',
        'engine_code': engine,
        'rest': m.group('rest'),
    }


def _decode_ch_customer(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ch_customer',
        'family': 'CH',
        'family_meaning': 'CH customer-specific (proprietary)',
        'seq': m.group('seq'),
        'is_proprietary': True,
        'requires_human_review': True,
    }


def _decode_surplus_reducer(m: re.Match) -> dict[str, Any]:
    inlet = 'ID' if m.group('inlet') == 'I' else 'OD'
    outlet = 'ID' if m.group('outlet') == 'I' else 'OD'
    return {
        'pattern': 'surplus_reducer',
        'family': 'SPR',
        'family_meaning': 'Surplus Reducer (production overrun)',
        'fit_inlet': inlet,
        'fit_outlet': outlet,
        'is_surplus': True,
    }


def _decode_gasket(m: re.Match) -> dict[str, Any]:
    oem = m.group('oem')
    return {
        'pattern': 'gasket',
        'family': 'G',
        'family_meaning': 'Gasket',
        'oem': oem,
        'oem_meaning': OEM_MEANINGS.get(oem) or {'BB': 'Bluebird'}.get(oem),
        'seq': m.group('seq'),
    }


def _decode_powerflow_flex(m: re.Match) -> dict[str, Any]:
    length = m.group('length')
    return {
        'pattern': 'powerflow_flex',
        'family': 'POWERFLOW',
        'family_meaning': 'PowerFlow flex hose',
        'diameter': float(m.group('diameter')),
        'construction': m.group('construction'),
        'length': float(length) if length else None,
    }


def _decode_asd(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'asd_display',
        'family': 'ASD',
        'family_meaning': 'Stock display item',
        'asd_rest': m.group('rest'),
        'disregard': True,
    }


def _decode_pwfl(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pwfl_merch',
        'family': 'PWFL',
        'family_meaning': 'PowerFlow apparel/merch',
        'pwfl_rest': m.group('rest'),
        'disregard': True,
    }


def _decode_gr_merch(m: re.Match) -> dict[str, Any]:
    line = m.group('line')
    line_meanings = {
        'GR':     'the example tenant branded merch',
        'GRE':    'the example tenant Exhaust line',
        'GRI':    'the example tenant Industries line',
        'GRDPFG': 'the example tenant DPF Gasket',
    }
    return {
        'pattern': 'gr_merch',
        'family': line,
        'family_meaning': line_meanings.get(line),
        'gr_rest': m.group('rest'),
        'disregard': line in ('GR', 'GRE', 'GRI'),
    }


def _decode_year_range(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'year_range',
        'family': m.group('application'),
        'family_meaning': 'Year-range emissions part',
        'year_start': int(m.group('year_start')),
        'year_end': int(m.group('year_end')),
        'application': m.group('application'),
        'variant': m.group('variant'),
    }


def _decode_marmon_l_length(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'marmon_l_length',
        'family': 'MARMON',
        'family_meaning': FAMILY_MEANINGS['MARMON'],
        'marmon_base': m.group('base'),
        'marmon_suffix': 'L' + m.group('length'),
        'length': float(m.group('length')),
    }


def _decode_sw_riverton(m: re.Match) -> dict[str, Any]:
    inner = m.group('inner')
    inner_result = _try_patterns(inner)
    result: dict[str, Any] = {
        'pattern': 'sw_riverton',
        'family': 'SW',
        'family_meaning': 'Riverton Welding (customer)',
        'sw_inner': inner,
        'is_proprietary': True,
        'proprietary_customer': 'Riverton Welding',
    }
    if inner_result:
        for k in ('diameter', 'length', 'body', 'body_meaning',
                  'finish', 'finish_meaning'):
            if k in inner_result:
                result[k] = inner_result[k]
    return result


def _decode_complete_kit(m: re.Match) -> dict[str, Any]:
    rest = m.group('rest')
    inner_match = re.match(
        r'(?P<angle>\d+)R(?P<reduction>\d+)(?P<oem>[A-Z]+?)(?P<finish>[ACPS])$',
        rest
    )
    result: dict[str, Any] = {
        'pattern': 'complete_kit',
        'family': 'CK',
        'family_meaning': 'Complete Kit',
        'diameter': float(m.group('diameter')),
    }
    if inner_match:
        result['angle'] = int(inner_match.group('angle'))
        result['reduction'] = float(inner_match.group('reduction'))
        result['oem'] = inner_match.group('oem')
        result['oem_meaning'] = OEM_MEANINGS.get(inner_match.group('oem'))
        result['finish'] = inner_match.group('finish')
        result['finish_meaning'] = FINISH_MEANINGS.get(inner_match.group('finish'))
    return result


def _decode_parametric_nf(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'parametric_nf',
        'family': family,
        'family_meaning': FAMILY_MEANINGS.get(family),
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'body': m.group('body'),
        'body_meaning': BODY_MEANINGS.get(m.group('body')),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
        'numeric_first': True,
    }


def _decode_material(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'material',
        'family': 'RAW',
        'family_meaning': 'Raw material spec',
        'diameter': float(m.group('diameter')),
        'gauge': int(m.group('gauge')),
        'alloy': m.group('alloy').upper().replace(' ', '').replace('.', ''),
        'is_raw_material': True,
    }


def _decode_misc_bend(m: re.Match) -> dict[str, Any]:
    diam = m.group('diameter')
    return {
        'pattern': 'misc_bend',
        'family': 'MISC_BEND',
        'family_meaning': 'Custom bend placeholder (MTO)',
        'diameter': float(diam) if diam else None,
        'is_placeholder': True,
        'disregard_for_cross_customer': True,
    }


def _decode_bom_child(m: re.Match) -> dict[str, Any] | None:
    parent_sku = m.group('parent')
    role = m.group('bom_role')
    role_meanings = {
        'ASSY':       'Kit/assembly version of parent',
        'COMPONENT':  'Component-only of a kit',
        'MNT':        'Mounting parts',
        'BRKT':       'Bracket',
    }
    # Recursively classify the parent
    parent_result = _try_patterns(parent_sku)
    result: dict[str, Any] = {
        'pattern': 'bom_child',
        'family': 'BOM_CHILD',
        'family_meaning': f'BOM child ({role_meanings.get(role)})',
        'parent_sku': parent_sku,
        'bom_role': role,
        'bom_role_meaning': role_meanings.get(role),
    }
    # Inherit identity hints from the parent
    if parent_result:
        for k in ('family', 'family_meaning', 'is_proprietary',
                  'proprietary_customer', 'oem', 'oem_meaning'):
            if k in parent_result and parent_result[k] is not None:
                result[f'parent_{k}'] = parent_result[k]
    return result


def _decode_cbs_kit(m: re.Match) -> dict[str, Any]:
    component_idx = m.group('component_idx')
    is_parent = not component_idx
    return {
        'pattern': 'cbs_kit',
        'family': 'CBS',
        'family_meaning': FAMILY_MEANINGS['CBS'],
        'oem': 'FL',
        'oem_meaning': 'Freightliner',
        'finish': 'S4',
        'finish_meaning': FINISH_MEANINGS['S4'],
        'is_kit_parent': is_parent,
        'is_kit_component': not is_parent,
        'component_idx': int(component_idx) if component_idx else None,
        'requires_human_review': True,  # CBS / ODS semantics still unconfirmed
    }


def _decode_z_kit(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    component_idx = m.group('component_idx')
    base = m.group('base')
    # Kit components have 2-digit indices (21, 22). Single-digit = parent.
    is_component = len(component_idx) >= 2
    return {
        'pattern': 'z_kit',
        'family': family,
        'family_meaning': FAMILY_MEANINGS.get(family),
        'z_base': base,
        'component_idx': component_idx,
        'is_kit_parent': not is_component,
        'is_kit_component': is_component,
    }


def _decode_oem_compound(m: re.Match) -> dict[str, Any]:
    oem = m.group('oem')
    compound = m.group('compound')
    meaning = OEM_COMPOUND_MEANINGS.get((oem, compound))
    result: dict[str, Any] = {
        'pattern': 'oem_compound',
        'family': oem,
        'family_meaning': OEM_MEANINGS.get(oem),
        'oem': oem,
        'oem_meaning': OEM_MEANINGS.get(oem),
        'compound_code': compound,
        'compound_meaning': meaning,
    }
    # If compound is a long alphanumeric, likely a literal OEM PN we mirror
    if any(ch.isdigit() for ch in compound) and len(compound) >= 5:
        result['is_oem_pn_mirror'] = True
    return result


# ============================================================================
# Catch-all patterns for high-volume families (added after catalog validation)
# ============================================================================

# --- SB School Bus parts (839 SKUs in catalog) ----------------------------
# SB1-0954TH, SB10-0702BB-S4, etc.
# Per v3.4 grammar: SB{seq}-{4-digit-id}{2-letter-customer}{?-finish-suffix}
PAT_SB_SCHOOL_BUS = re.compile(
    r'^SB(?P<seq>\d{1,2})-(?P<id>\d{3,4})'
    r'(?P<customer>[A-Z]{2,3})'
    r'(?P<suffix>-?[A-Z0-9.-]+)?$'
)

def _decode_sb_school_bus(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sb_school_bus',
        'family': 'SB',
        'family_meaning': 'School Bus part',
        'sb_seq': m.group('seq'),
        'sb_id': m.group('id'),
        'sb_customer_code': m.group('customer'),
        'sb_suffix': m.group('suffix'),
    }

# --- ZP/ZM modern series (broad pattern, 741 + 436 SKUs) -----------------
# ZP{4-5 digit seq}{?-component}{?finish}{? REV X}
PAT_Z_SERIES = re.compile(
    r'^(?P<family>ZP|ZM)(?P<seq>\d{3,5})'
    r'(?:-(?P<component>\d{1,2}))?'
    r'(?P<finish>[ACPRS])?'
    r'(?:\s+REV\.?\s*[A-Z])?$'
)

def _decode_z_series(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    component = m.group('component')
    return {
        'pattern': 'z_series',
        'family': family,
        'family_meaning': FAMILY_MEANINGS.get(family),
        'z_seq': m.group('seq'),
        'component_idx': component,
        'is_kit_parent': component is None or len(component) == 1,
        'is_kit_component': component is not None and len(component) >= 2,
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')) if m.group('finish') else None,
    }

# --- OEM-mirror SKUs (KW-, FL-, PB-, MK-, IH-, VG-, WS-, FT-, GM-) -------
# Generic format: {OEM}-{seq}{?suffix}
# Catalog has hundreds of these, e.g., KW-1042S3, FL-09152-022, PB-04055Y
PAT_OEM_MIRROR = re.compile(
    r'^(?P<oem>KW|PB|FL|IH|MK|VG|WS|FT|GM|MAC|CAT)-'
    r'(?P<oem_seq>[A-Z0-9][-A-Z0-9.]*)$'
)

def _decode_oem_mirror(m: re.Match) -> dict[str, Any]:
    oem = m.group('oem')
    return {
        'pattern': 'oem_mirror',
        'family': oem,
        'family_meaning': OEM_MEANINGS.get(oem, f'{oem} OEM-mirror'),
        'oem': oem,
        'oem_meaning': OEM_MEANINGS.get(oem),
        'oem_seq': m.group('oem_seq'),
        'is_oem_pn_mirror': True,
    }

# --- M Muffler family (M-NNNN style, 201 SKUs) ----------------------------
# Distinct from parametric M{D}-{L}... — these are M-{4-digit-seq} catalog SKUs
PAT_M_MUFFLER = re.compile(
    r'^M-(?P<seq>\d{3,5})(?P<suffix>[A-Z0-9-]*)$'
)

def _decode_m_muffler(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'm_muffler',
        'family': 'M',
        'family_meaning': 'Muffler (catalog SKU)',
        'm_seq': m.group('seq'),
        'm_suffix': m.group('suffix'),
    }

# --- SF Stainless Flex hose (121 SKUs) ------------------------------------
PAT_SF_FLEX = re.compile(
    r'^SF-(?P<rest>[\d.]+)$'
)

def _decode_sf_flex(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sf_flex',
        'family': 'SF',
        'family_meaning': 'Stainless Flex hose',
        'sf_spec': m.group('rest'),
    }

# --- G Galvanized flex hose (G{thickness}-{D}{L}, 117 SKUs) ---------------
# G12-4300 = .012 thick, 4" diameter, 300" length
# Distinct from gasket G-{OEM}{seq}; here numeric prefix vs letter prefix
PAT_G_FLEX = re.compile(
    r'^G(?P<thickness>\d{2})-(?P<diameter>\d)(?P<length>\d{3})$'
)

def _decode_g_flex(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'g_flex',
        'family': 'G',
        'family_meaning': 'Galvanized flex hose',
        'thickness_thou': int(m.group('thickness')),
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
    }

# --- L elbow with decimal-prefix diameter (L1.5, L1.75, L2.25 etc.) -------
# Already covered by main elbow regex for single-digit, but L1.590-...
# pattern needs explicit handling for decimal-prefix diameters.
PAT_ELBOW_DECIMAL_D = re.compile(
    r'^L'
    r'(?P<diameter>\d\.\d{1,3})'  # 1.5, 1.75, 2.25, 3.5 etc.
    r'(?P<angle>180|175|170|165|160|155|150|145|140|135|130|125|120|115|110|105|100|96|95|90|85|80|75|70|65|60|55|50|45|40|35|30|25|20|15|10|5)'
    r'-'
    r'(?P<legs>\d{4,6}|\d{2,3}\.\d{1,2}\d{2,3}|\d{2,3}\d{2,3}(?:\.\d{1,2})?)'
    r'(?P<od_marker>S)?'
    r'(?P<finish>S4S|S3|S4|[ACPSR])'
    r'(?P<modifier>R)?$'
)

def _decode_elbow_decimal_d(m: re.Match) -> dict[str, Any]:
    legs_raw = m.group('legs')
    leg1, leg2 = _split_legs(legs_raw)
    od = m.group('od_marker') == 'S'
    return {
        'pattern': 'elbow',
        'family': 'L',
        'family_meaning': 'Elbow',
        'diameter': float(m.group('diameter')),
        'angle': int(m.group('angle')),
        'leg1': leg1,
        'leg2': leg2,
        'body': 'SB' if od else 'EX',
        'body_meaning': BODY_MEANINGS.get('SB' if od else 'EX'),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
        'modifier': m.group('modifier'),
    }

# --- Multi-segment numeric drawing-number SKUs ----------------------------
# 02-04-07-007-02, 04-21455-114, 01-20724-000 etc. Old internal drawing nums.
PAT_DRAWING_NUMBER = re.compile(
    r'^\d{1,3}(?:-\d{2,5}){2,5}$'
)

def _decode_drawing_number(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'drawing_number',
        'family': 'DRAWING',
        'family_meaning': 'Legacy internal drawing number',
        'requires_human_review': True,
    }

# --- Pure numeric SKUs (legacy / pre-system) ------------------------------
# 040493, 03690311270, 00151012 — old SKUs that pre-date the parametric system
PAT_PURE_NUMERIC = re.compile(r'^\d{5,12}$')

def _decode_pure_numeric(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pure_numeric',
        'family': 'NUMERIC',
        'family_meaning': 'Pure-numeric legacy SKU',
        'requires_human_review': True,
    }

# --- HP / LL2 customer-specific drawings (0199-LL2-XXX, 1616-LL2-XXX) -----
PAT_HP_DRAWING = re.compile(
    r'^(?P<program>\d{4})-LL2-(?P<seq>\d{3})'
    r'(?P<suffix>[A-Z]?)'
    r'(?:\s+REV\.?\s*[A-Z])?$'
)

def _decode_hp_drawing(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hp_drawing',
        'family': 'HP',
        'family_meaning': 'Summit Performance customer drawing',
        'hp_program': m.group('program'),
        'hp_seq': m.group('seq'),
        'is_proprietary': True,
        'proprietary_customer': 'Summit Performance',
    }

# --- L elbow with embedded angle (no dash separator before legs) ---------
# L22545-0909SA = L + 2.25 (compressed as 225) + 45° + 09 + 09 + S + A
# L190-5.501SAR = L + 1.9 + 0° (??) - special form
# These don't fit the standard regex; provide a more permissive fallback
PAT_ELBOW_COMPRESSED = re.compile(
    r'^L(?P<diameter>\d{1,3})(?P<angle>180|175|170|165|160|155|150|145|140|135|130|125|120|115|110|105|100|96|95|90|85|80|75|70|65|60|55|50|45|40|35|30|25|20|15|10|5)'
    r'-(?P<legs>\d{4,6}|\d{2,3}\.\d{1,2}\d{2,3})'
    r'(?P<od_marker>S)?'
    r'(?P<finish>S4S|S3|S4|[ACPSR])'
    r'(?P<modifier>R)?$'
)

def _decode_elbow_compressed(m: re.Match) -> dict[str, Any]:
    diam_raw = m.group('diameter')
    # Heuristic: if it's 3 digits with no decimal, it might be a compressed
    # decimal (225 = 2.25, 175 = 1.75)
    if len(diam_raw) == 3 and diam_raw.startswith(('1', '2', '3')):
        diameter = float(diam_raw[0]) + float(diam_raw[1:]) / 100
    elif len(diam_raw) == 2:
        diameter = float(diam_raw[0]) + 0.5
    else:
        diameter = float(diam_raw)
    legs_raw = m.group('legs')
    leg1, leg2 = _split_legs(legs_raw)
    od = m.group('od_marker') == 'S'
    return {
        'pattern': 'elbow',
        'family': 'L',
        'family_meaning': 'Elbow',
        'diameter': diameter,
        'angle': int(m.group('angle')),
        'leg1': leg1,
        'leg2': leg2,
        'body': 'SB' if od else 'EX',
        'body_meaning': BODY_MEANINGS.get('SB' if od else 'EX'),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
        'modifier': m.group('modifier'),
    }

# --- R reducer alternate format (R{D1}{ID|OD}-{D2}{ID|OD}{?-modifier}) ---
# R3.25I-3OA-6 = 3.25" ID, 3" OD, ALZ, 6" length-modifier
# R35I-3IA = 3.5" ID, 3" ID, ALZ
PAT_R_REDUCER_ALT = re.compile(
    r'^R(?P<inlet>\d+(?:\.\d+)?)(?P<inlet_fit>I|O)?'
    r'-?(?P<outlet>\d+(?:\.\d+)?)(?P<outlet_fit>I|O)A'
    r'(?:-(?P<modifier>[A-Z0-9]+))?$'
)

def _decode_r_reducer_alt(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'reducer_alt',
        'family': 'R',
        'family_meaning': 'Reducer (alternate format)',
        'inlet_diameter': float(m.group('inlet')),
        'outlet_diameter': float(m.group('outlet')),
        'inlet_fit': 'ID' if m.group('inlet_fit') == 'I' else ('OD' if m.group('inlet_fit') == 'O' else None),
        'outlet_fit': 'ID' if m.group('outlet_fit') == 'I' else 'OD',
        'finish': 'A',
        'finish_meaning': 'Aluminized',
        'modifier': m.group('modifier'),
    }

# --- ZP/ZM with multi-segment seq and trailing finish/material ------------
# ZP101-5S4, ZP101-6S4 — broader form than basic z_series
PAT_Z_SERIES_EXTENDED = re.compile(
    r'^(?P<family>ZP|ZM)(?P<seq>\d{2,5})'
    r'-(?P<component>\d{1,2})'
    r'(?P<finish>S4|S3|[ACPRS])?'
    r'(?:\s+REV\.?\s*[A-Z])?$'
)

# Already defined; the simple z_series should catch these. Need to verify.

# --- PF Pre-Form clamps ---------------------------------------------------
# PF-225A, PF-225SS, PF-300A, PF-2A, PF-35A, PF-35ABK
PAT_PF_CLAMP = re.compile(
    r'^PF-(?P<diameter>\d{1,3})(?P<finish>SS|S3|S4|ZN|[ACPS])(?P<bulk>BK)?$'
)

def _decode_pf_clamp(m: re.Match) -> dict[str, Any]:
    diam_raw = m.group('diameter')
    if len(diam_raw) == 2:
        diameter = float(diam_raw[0]) + 0.5  # 35 = 3.5
    elif len(diam_raw) == 3:
        diameter = float(diam_raw[0]) + float(diam_raw[1:]) / 100  # 225 = 2.25
    else:
        diameter = float(diam_raw)
    return {
        'pattern': 'pf_clamp',
        'family': 'PF',
        'family_meaning': 'Pre-Form clamp',
        'diameter': diameter,
        'finish': m.group('finish'),
    }

# --- VB V-Band clamps -----------------------------------------------------
# VB-10.0C, VB-10.375C
PAT_VB_CLAMP = re.compile(
    r'^VB-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>[ACPS])?$'
)

def _decode_vb_clamp(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'vb_clamp',
        'family': 'VB',
        'family_meaning': 'V-Band clamp',
        'diameter': float(m.group('diameter')),
        'finish': m.group('finish'),
    }

# --- U-bolt SKUs ----------------------------------------------------------
# U3-10, U3-15 (3" double bend elbow ##°)
# Also U1RS4G battery (already filtered as battery)
PAT_U_BOLT = re.compile(
    r'^U(?P<diameter>\d{1,2}(?:\.\d+)?)-(?P<rest>[\d-]+(?:[A-Z]+)?)$'
)

def _decode_u_bolt(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 2 and d[1] == '5' and '.' not in d:
        diameter = float(d[0]) + 0.5
    else:
        diameter = float(d)
    return {
        'pattern': 'u_bolt',
        'family': 'U',
        'family_meaning': 'U-bolt / double bend elbow',
        'diameter': diameter,
        'u_rest': m.group('rest'),
    }

# --- B-prefix SKUs (B-0954TH stack blanks, etc.) --------------------------
# Per v3.4 grammar discussion: B-{4-digit}{2-letter-customer}
PAT_B_BLANK = re.compile(
    r'^B-(?P<id>\d{3,4})(?P<customer>[A-Z]{2})(?P<suffix>-?[A-Z0-9.-]*)$'
)

def _decode_b_blank(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'b_blank',
        'family': 'B',
        'family_meaning': 'Stack blank / WIP',
        'b_id': m.group('id'),
        'b_customer_code': m.group('customer'),
        'b_suffix': m.group('suffix'),
    }

# --- ST stack/turbo SKUs --------------------------------------------------
# ST-09322-037, ST-17617-000
PAT_ST_SKU = re.compile(
    r'^ST-(?P<seq>\d{4,6})-(?P<sub>\d{3})(?P<finish>[ACPS])?$'
)

def _decode_st_sku(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'st_sku',
        'family': 'ST',
        'family_meaning': 'ST series (stack/turbo)',
        'st_seq': m.group('seq'),
        'st_sub': m.group('sub'),
    }

# --- T turbo/tube SKUs ----------------------------------------------------
# T0574, T313-2, T321-2
PAT_T_SKU = re.compile(
    r'^T(?P<seq>\d{3,5})(?:-(?P<sub>\d{1,3}))?$'
)

def _decode_t_sku(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 't_sku',
        'family': 'T',
        'family_meaning': 'Turbo flare / tube adapter',
        't_seq': m.group('seq'),
        't_sub': m.group('sub'),
    }

# --- HD heavy-duty hangers/accessories -----------------------------------
# HD-10 (hanger 3"-4" tail pipe universal)
PAT_HD_SKU = re.compile(
    r'^HD-(?P<seq>\d{1,4})(?P<suffix>[A-Z]?)$'
)

def _decode_hd_sku(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hd_sku',
        'family': 'HD',
        'family_meaning': 'Heavy-duty hanger/accessory',
        'hd_seq': m.group('seq'),
    }

# --- S-prefix tube SKUs (S10-120ALUM, S1-120SB-IN) ------------------------
# Where the body-finish doesn't fit standard parametric regex
# (e.g., ALUM = aluminum, SB-IN = OD-mating with extension marker)
PAT_S_TUBE = re.compile(
    r'^S(?P<diameter>\d{1,2}(?:\.\d+)?)-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<rest>[A-Z][A-Z0-9-]*)$'
)

def _decode_s_tube(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 's_tube',
        'family': 'S',
        'family_meaning': 'Straight tube',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        's_rest': m.group('rest'),
    }

# --- L compressed-decimal elbow with reduction ----------------------------
# L2590-1212SA-3 = 2.5", 90°, 12x12, OD, ALZ, reduced to 3"
# Diameter is 2 digits where 25 = 2.5, 35 = 3.5, etc.
PAT_ELBOW_HALF_DIAM = re.compile(
    r'^L(?P<diameter>\d{2})'  # 25, 35, 175 etc.
    r'(?P<angle>180|175|170|165|160|155|150|145|140|135|130|125|120|115|110|105|100|96|95|90|85|80|75|70|65|60|55|50|45|40|35|30|25|20|15|10|5)'
    r'-(?P<legs>\d{4,6}|\d{2,3}\.\d{1,2}\d{2,3})'
    r'(?P<od_marker>S)?'
    r'(?P<finish>S4S|S3|S4|[ACPSR])'
    r'(?:-(?P<modifier>\d+(?:\.\d+)?))?$'
)

def _decode_elbow_half_diam(m: re.Match) -> dict[str, Any]:
    diam_raw = m.group('diameter')
    # 2-digit means half-diameter encoded as N5 (where N is full inches and 5 = .5)
    # 25 = 2.5, 35 = 3.5
    if len(diam_raw) == 2 and diam_raw[1] == '5':
        diameter = float(diam_raw[0]) + 0.5
    else:
        diameter = float(diam_raw)
    legs_raw = m.group('legs')
    leg1, leg2 = _split_legs(legs_raw)
    od = m.group('od_marker') == 'S'
    return {
        'pattern': 'elbow',
        'family': 'L',
        'family_meaning': 'Elbow',
        'diameter': diameter,
        'angle': int(m.group('angle')),
        'leg1': leg1,
        'leg2': leg2,
        'body': 'SB' if od else 'EX',
        'body_meaning': BODY_MEANINGS.get('SB' if od else 'EX'),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
        'modifier': m.group('modifier'),
    }

# --- ZP/ZM with finish suffix (ZP101-5S4, ZP101-6S4) ---------------------
PAT_Z_WITH_FINISH = re.compile(
    r'^(?P<family>ZP|ZM)(?P<seq>\d{2,5})-(?P<sub>\d{1,2})(?P<finish>S4|S3|BS|[ACPS])$'
)

def _decode_z_with_finish(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'z_series',
        'family': family,
        'family_meaning': FAMILY_MEANINGS.get(family),
        'z_seq': m.group('seq'),
        'component_idx': m.group('sub'),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
    }

# --- R-reducer alternate form 2 (R35O-3IC, R3.25I-3OA) -------------------
# More permissive: R{D1}{I|O}-{D2}{I|O}{finish}
PAT_R_REDUCER_PERMISSIVE = re.compile(
    r'^R(?P<inlet>\d+(?:\.\d+)?)(?P<inlet_fit>I|O)'
    r'-?(?P<outlet>\d+(?:\.\d+)?)(?P<outlet_fit>I|O)?'
    r'(?P<finish>[ACPS])'
    r'(?:-(?P<modifier>\d+))?$'
)

def _decode_r_reducer_permissive(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'reducer_alt',
        'family': 'R',
        'family_meaning': 'Reducer (alt format)',
        'inlet_diameter': float(m.group('inlet')),
        'outlet_diameter': float(m.group('outlet')),
        'inlet_fit': 'ID' if m.group('inlet_fit') == 'I' else 'OD',
        'outlet_fit': 'ID' if m.group('outlet_fit') == 'I' else ('OD' if m.group('outlet_fit') == 'O' else None),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')),
    }

# --- PS Powerstroke kit (PS-6.0-4SK = Ford 6.0L 4" stack kit) -----------
PAT_PS_KIT = re.compile(
    r'^PS-(?P<engine>[\d.]+)-(?P<diameter>\d+(?:\.\d+)?)(?P<kit_type>[A-Z]+)$'
)

def _decode_ps_kit(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ps_kit',
        'family': 'PS',
        'family_meaning': 'Powerstroke kit (Ford diesel)',
        'engine_displacement': m.group('engine'),
        'diameter': float(m.group('diameter')),
        'kit_type': m.group('kit_type'),
        'oem': 'FT',
    }

# --- DC Dodge Cummins kit (DC-0304-4SK = 03/04 Dodge 4" stack kit) -------
PAT_DC_KIT = re.compile(
    r'^DC-(?P<years>\d{4})-(?P<diameter>\d+(?:\.\d+)?)(?P<kit_type>[A-Z]+)$'
)

def _decode_dc_kit(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dc_kit',
        'family': 'DC',
        'family_meaning': 'Dodge Cummins kit',
        'year_range': m.group('years'),
        'diameter': float(m.group('diameter')),
        'kit_type': m.group('kit_type'),
    }

# --- FB Flat Bolt clamp (FB-35P = 3.5" plain, FB-35SS = 3.5" stainless) --
PAT_FB_CLAMP = re.compile(
    r'^FB-(?P<diameter>\d{1,3})(?P<finish>SS|ZN|S3|S4|[ACPS])$'
)

def _decode_fb_clamp(m: re.Match) -> dict[str, Any]:
    diam_raw = m.group('diameter')
    if len(diam_raw) == 2 and diam_raw[1] == '5':
        diameter = float(diam_raw[0]) + 0.5
    elif len(diam_raw) == 3:
        diameter = float(diam_raw[0]) + float(diam_raw[1:]) / 100
    else:
        diameter = float(diam_raw)
    return {
        'pattern': 'fb_clamp',
        'family': 'FB',
        'family_meaning': 'Flat Bolt clamp',
        'diameter': diameter,
        'finish': m.group('finish'),
    }

# --- RB Round Bolt clamp -------------------------------------------------
PAT_RB_CLAMP = re.compile(
    r'^RB-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>ZN|SS|S3|S4|[ACPSZ]+)?(?P<suffix>EXP)?$'
)

def _decode_rb_clamp(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'rb_clamp',
        'family': 'RB',
        'family_meaning': 'Round Bolt clamp',
        'diameter': float(m.group('diameter')),
        'finish': m.group('finish'),
    }

# --- P-prefix Paragon-style supplier SKU ---------------------------------
# P205976-016-147, P206280, P206285-185-147
PAT_P_SUPPLIER = re.compile(
    r'^P(?P<seq>\d{5,6})(?:-(?P<sub1>\d{2,3})(?:-(?P<sub2>\d{2,3}))?)?$'
)

def _decode_p_supplier(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'p_pipe_legacy',
        'family': 'P',
        'family_meaning': 'Pipe (long-form legacy SKU)',
        'p_seq': m.group('seq'),
        'p_sub': m.group('sub1'),
    }

# --- K-elbow special (K266-101) ------------------------------------------
# Some K SKUs are old-format elbows: K{seq}-{lengths}
PAT_K_ELBOW_SPECIAL = re.compile(
    r'^K(?P<seq>\d{3})-(?P<sub>\d{3})$'
)

def _decode_k_elbow_special(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'k_elbow_special',
        'family': 'K',
        'family_meaning': 'Curved stack (legacy elbow form)',
        'k_seq': m.group('seq'),
        'k_sub': m.group('sub'),
    }

# --- Drawing/legacy SKUs with spaces (040500 BRKT, 111529 REV. C) ---------
# Pure-numeric prefix + space + descriptive suffix
PAT_LEGACY_WITH_SPACE = re.compile(
    r'^(?P<base>\d+(?:-\d+)*)\s+(?P<suffix>[A-Z][A-Z0-9.\s]*)$'
)

def _decode_legacy_with_space(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'legacy_with_space',
        'family': 'LEGACY',
        'family_meaning': 'Legacy SKU with text suffix',
        'base': m.group('base'),
        'suffix': m.group('suffix'),
        'requires_human_review': True,
    }

# --- Multi-segment dash SKUs (01-6611, 04-09152-021, etc.) ---------------
PAT_MULTI_SEGMENT_DASH = re.compile(
    r'^\d{1,3}-\d{2,5}(?:-\d{2,5})?$'
)

def _decode_multi_segment_dash(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'multi_segment_legacy',
        'family': 'LEGACY',
        'family_meaning': 'Multi-segment legacy SKU',
        'requires_human_review': True,
    }

# --- CP standalone coupler (CP-10, CP-2258A, CP-258A) -------------------
# Format: CP-{D}{L}{finish}, e.g., CP-258A = 2.5"x8" coupler ALZ
# Or just CP-{D} for plain steel
PAT_CP_COUPLER = re.compile(
    r'^CP-(?P<rest>\d+[A-Z0-9]*)$'
)

def _decode_cp_coupler(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'cp_coupler',
        'family': 'CP',
        'family_meaning': 'Coupler',
        'cp_rest': m.group('rest'),
    }

# --- RS connector (RS-312A, RS-318A, RS-324A) ---------------------------
# RS-{D}{L}{finish}: 3"x12 ID/ID ALZ etc.
PAT_RS_CONNECTOR = re.compile(
    r'^RS-(?P<diameter>\d{1,3})(?P<length>\d{2,3})?(?P<finish>[ACPS])?$'
)

def _decode_rs_connector(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'rs_connector',
        'family': 'RS',
        'family_meaning': 'Connector',
        'rs_rest': m.group('diameter') + (m.group('length') or '') + (m.group('finish') or ''),
    }

# --- Y Y-pipe (Y-300A, Y-300NPA, Y-350NPA) ------------------------------
PAT_Y_PIPE = re.compile(
    r'^Y-(?P<diameter>\d{2,4})(?P<suffix>[A-Z]+)?$'
)

def _decode_y_pipe(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'y_pipe',
        'family': 'Y',
        'family_meaning': 'Y-pipe',
        'y_rest': m.group('diameter') + (m.group('suffix') or ''),
    }

# --- QPM Quiet Performance Muffler --------------------------------------
PAT_QPM = re.compile(
    r'^QPM-(?P<seq>\d{3,5})(?P<suffix>-?[A-Z0-9-]*)$'
)

def _decode_qpm(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'qpm_muffler',
        'family': 'QPM',
        'family_meaning': 'Quiet Performance Muffler',
        'qpm_seq': m.group('seq'),
    }

# --- More L-elbow forms (L3-10SA = 3"x10" deg cut box elbow) ------------
# Old format: L{D}-{L}{body+finish} (no angle, no leg pair)
PAT_L_ELBOW_OLD = re.compile(
    r'^L(?P<diameter>\d(?:\.\d+)?)-(?P<length>\d{1,3})(?P<rest>[A-Z]+)$'
)

def _decode_l_elbow_old(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'elbow_old',
        'family': 'L',
        'family_meaning': 'Elbow (legacy short form)',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'l_rest': m.group('rest'),
    }

# --- Drop bin / raw stock SKUs (no leading letter) -----------------------
# 2.25" ALZ. DROP, 5" 16 GA. ALZ BIN A
PAT_DROP_BIN = re.compile(
    r'^\d+(?:\.\d+)?["\s]+.+(?:DROP|BIN|GA)\b',
    re.IGNORECASE
)

def _decode_drop_bin(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'drop_bin',
        'family': 'RAW',
        'family_meaning': 'Raw material drop / bin',
        'is_raw_material': True,
    }

# --- 7-digit Kimble pipe SKUs (700065-024 etc.) ------------------------
PAT_KIMBLE_7 = re.compile(
    r'^(?P<base>\d{6,7})-(?P<sub>\d{2,3})$'
)

def _decode_kimble_7(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'legacy_7digit',
        'family': 'LEGACY',
        'family_meaning': 'Legacy 7-digit SKU (e.g., Kimble)',
        'base': m.group('base'),
        'sub': m.group('sub'),
        'requires_human_review': True,
    }

# --- KW four-leg compound (KW6-10742LA) ---------------------------------
# Distinct from KW-NNNN OEM-mirror: leading digit indicates diameter
PAT_KW_DIAM = re.compile(
    r'^(?P<oem>KW|FL|PB|IH|MK|VG|WS|FT|GM)(?P<diameter>\d)-'
    r'(?P<seq>\d{4,6})(?P<suffix>[A-Z0-9]+)?$'
)

def _decode_kw_diam(m: re.Match) -> dict[str, Any]:
    oem = m.group('oem')
    return {
        'pattern': 'oem_mirror_with_diam',
        'family': oem,
        'family_meaning': OEM_MEANINGS.get(oem),
        'oem': oem,
        'oem_meaning': OEM_MEANINGS.get(oem),
        'diameter': float(m.group('diameter')),
        'oem_seq': m.group('seq'),
        'oem_suffix': m.group('suffix'),
    }

# --- ZP/ZM with letter+seq (ZP1280L, ZP1333-1CR, ZP1335-2T) -----------
# Beyond the standard z_series: ZP{seq}{single-letter suffix}
PAT_Z_SHORT = re.compile(
    r'^(?P<family>ZP|ZM)(?P<seq>\d{3,5})(?P<short_suffix>[A-Z]{1,3})$'
)

def _decode_z_short(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'z_series',
        'family': family,
        'family_meaning': FAMILY_MEANINGS.get(family),
        'z_seq': m.group('seq'),
        'short_suffix': m.group('short_suffix'),
    }

# --- SK with ceramic-lined modifier (SK6-24SBC-CL) ----------------------
# These are S-prefix reducer + K base + -CL ceramic-lined modifier
# Already covered by s_reducer regex but the -CL modifier breaks pattern
PAT_SK_CERAMIC = re.compile(
    r'^SK(?P<inlet>\d+(?:\.\d+)?)-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB)'
    r'(?P<finish>BBC|BS|S3|S4|[ACPS])?'
    r'-(?P<modifier>CL)$'
)

def _decode_sk_ceramic(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 's_reducer',
        'family': 'K',
        'family_meaning': 'Curved (reducing, ceramic-lined)',
        'is_reducer': True,
        'inlet_diameter': float(m.group('inlet')),
        'diameter': float(m.group('inlet')),
        'outlet_diameter': 5.0,
        'outlet_implicit': True,
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
        'ceramic_lined': True,
    }

# --- SF flex extended (SF-309R, SF-312EX) ------------------------------
PAT_SF_FLEX_EXT = re.compile(
    r'^SF-(?P<diameter>\d{1,3})(?P<length>\d{1,3})?(?P<suffix>R|EX)?$'
)

def _decode_sf_flex_ext(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sf_flex',
        'family': 'SF',
        'family_meaning': 'Stainless Flex hose',
        'sf_diameter': m.group('diameter'),
        'sf_length': m.group('length'),
        'sf_suffix': m.group('suffix'),
    }

# --- SP standalone spring plate (SP-4, SP-4SS, SP-5) -------------------
PAT_SP_PLATE = re.compile(
    r'^SP-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>SS|S3|S4|[ACPS])?$'
)

def _decode_sp_plate(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sp_plate',
        'family': 'SP',
        'family_meaning': 'Spring plate',
        'diameter': float(m.group('diameter')),
        'finish': m.group('finish'),
    }

# --- G connector / extended-flex (G09990020, G15-15300) ----------------
# Pure-numeric or G{thickness}-{diameter}{length} with 5-digit numbers
PAT_G_EXTENDED = re.compile(
    r'^G(?P<rest>\d+(?:-\d+)?)$'
)

def _decode_g_extended(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'g_extended',
        'family': 'G',
        'family_meaning': 'G-prefix connector / flex',
        'g_rest': m.group('rest'),
    }

# --- T tube/turbo with finish suffix (T321-3ID, T321-3O, T339740) -----
PAT_T_EXTENDED = re.compile(
    r'^T(?P<seq>\d{3,6})(?:-(?P<sub>\d+))?(?P<suffix>I[DC]|O|[A-Z]{1,3})?$'
)

def _decode_t_extended(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 't_sku',
        'family': 'T',
        'family_meaning': 'Turbo flare / tube adapter',
        't_seq': m.group('seq'),
        't_sub': m.group('sub'),
        't_suffix': m.group('suffix'),
    }

# --- DC alternative format (DC-0304-A4, DC-0304-S4SK) -----------------
# Already partly covered; broaden to accept more kit_type formats
PAT_DC_KIT_BROAD = re.compile(
    r'^DC-(?P<years>\d{4})-(?P<rest>[A-Z0-9]+)$'
)

def _decode_dc_kit_broad(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dc_kit',
        'family': 'DC',
        'family_meaning': 'Dodge Cummins kit',
        'year_range': m.group('years'),
        'kit_rest': m.group('rest'),
    }

# --- DPU Diesel PickUp kit ---------------------------------------------
PAT_DPU_KIT = re.compile(
    r'^DPU-(?P<rest>[\d.]+[A-Z]+)$'
)

def _decode_dpu_kit(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dpu_kit',
        'family': 'DPU',
        'family_meaning': 'Diesel pickup kit',
        'dpu_rest': m.group('rest'),
    }

# --- TKW Tapered Kenworth (TKW-14764LC, TKW6-1270LC-5) ----------------
PAT_TKW = re.compile(
    r'^TKW(?P<diameter>\d)?-?(?P<seq>\d{4,6})(?P<suffix>[A-Z0-9-]+)?$'
)

def _decode_tkw(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'tkw_tapered',
        'family': 'TKW',
        'family_meaning': 'Tapered Kenworth pipe',
        'oem': 'KW',
        'oem_meaning': 'Kenworth',
        'tkw_seq': m.group('seq'),
        'diameter': float(m.group('diameter')) if m.group('diameter') else None,
    }

# --- CN connector (CN-2258A, CN-258A) ---------------------------------
PAT_CN_CONNECTOR = re.compile(
    r'^CN-(?P<rest>\d+[A-Z]*)$'
)

def _decode_cn_connector(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'cn_connector',
        'family': 'CN',
        'family_meaning': 'Connector',
        'cn_rest': m.group('rest'),
    }

# --- AS Accuseal clamp (AS-175SS, AS-225A) ----------------------------
PAT_AS_CLAMP = re.compile(
    r'^AS-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>SS|S3|S4|[ACPS])?$'
)

def _decode_as_clamp(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'as_clamp',
        'family': 'AS',
        'family_meaning': 'Accuseal clamp',
        'diameter': float(m.group('diameter')),
        'finish': m.group('finish'),
    }

# --- F-Ford OEM-mirror (F1HZ-5246U etc.) ------------------------------
PAT_F_FORD = re.compile(
    r'^(?P<base>F\d[A-Z]{2})-(?P<seq>\d+[A-Z]*)(?:-(?P<sub>[A-Z0-9]+))?$'
)

def _decode_f_ford(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'f_ford',
        'family': 'FT',
        'family_meaning': 'Ford OEM-mirror',
        'oem': 'FT',
        'oem_meaning': 'Ford Truck',
        'f_base': m.group('base'),
        'f_seq': m.group('seq'),
    }

# --- D-prefix legacy SKU (D18232, D18235) -----------------------------
PAT_D_LEGACY = re.compile(
    r'^D(?P<seq>\d{4,6})$'
)

def _decode_d_legacy(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'd_legacy',
        'family': 'D',
        'family_meaning': 'D-prefix legacy SKU',
        'd_seq': m.group('seq'),
    }

# --- IM Intermediate Muffler (IM-418, IM-424) -------------------------
PAT_IM_MUFFLER = re.compile(
    r'^IM-(?P<rest>\d+(?:\s*[A-Z]+)?)$'
)

def _decode_im_muffler(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'im_muffler',
        'family': 'IM',
        'family_meaning': 'Intermediate Muffler',
        'im_rest': m.group('rest'),
    }

# --- EBK bellows kit (EBK-21428536, EBK-25023-016) --------------------
PAT_EBK_BELLOWS = re.compile(
    r'^EBK-(?P<seq>\d+(?:-\d+)?)$'
)

def _decode_ebk_bellows(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ebk_bellows',
        'family': 'EBK',
        'family_meaning': 'Bellows kit',
        'ebk_seq': m.group('seq'),
    }

# --- BOX shipping/storage box ----------------------------------------
PAT_BOX = re.compile(r'^BOX-.+$')

def _decode_box(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'box',
        'family': 'BOX',
        'family_meaning': 'Shipping/storage box',
        'disregard': True,
    }

# ============================================================================
# Phase 4 expansion: cover the long tail of family-specific patterns
# ============================================================================

# --- L elbow with 3-digit diameter (compressed N.NN like 190 = 1.90) -----
# L190-5.501SAR (1.9" diameter), L315-0404EXEXA (3.15" with 04x04)
# L345-0707SBS3 (3.45" 45° elbow with 7x7 legs; S3 finish)
PAT_ELBOW_3DIGIT = re.compile(
    r'^L(?P<diameter>\d{3})'
    r'-(?P<rest>[\d.]+(?:[A-Z]+)?)$'
)

def _decode_elbow_3digit(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    # 190 -> 1.90, 315 -> 3.15, 345 -> 3.45
    diameter = float(d[0]) + float(d[1:]) / 100
    return {
        'pattern': 'elbow_compressed',
        'family': 'L',
        'family_meaning': 'Elbow (compressed-decimal form)',
        'diameter': diameter,
        'l_rest': m.group('rest'),
        'requires_human_review': False,
    }

# --- ZP/ZM with -X-Sn  (ZP1601-2-S3, ZM10081-3-S3) --------------------
PAT_Z_DASH_FINISH = re.compile(
    r'^(?P<family>ZP|ZM|ZDS|ZE|ZY|ZS|ZT)'
    r'(?P<seq>\d{3,5})'
    r'-(?P<sub>\d{1,2})'
    r'-?(?P<finish>S3|S4|BS|REVA|REV[\sA-Z]*|[ACPS])?'
    r'(?P<extra>[A-Z]+)?$'
)

def _decode_z_dash_finish(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    family_meanings = {
        'ZP': 'Z-pipe', 'ZM': 'Z-muffler', 'ZDS': 'Z dump stack',
        'ZE': 'Z elbow', 'ZY': 'Z Y-pipe', 'ZS': 'Z stack',
        'ZT': 'Z turbo pipe',
    }
    return {
        'pattern': 'z_series',
        'family': family,
        'family_meaning': family_meanings.get(family, f'{family} series'),
        'z_seq': m.group('seq'),
        'component_idx': m.group('sub'),
        'finish': m.group('finish'),
        'finish_meaning': FINISH_MEANINGS.get(m.group('finish')) if m.group('finish') else None,
    }

# --- ZDS, ZE, ZY, ZS, ZT short forms --------------------------------
PAT_Z_FAMILY_SHORT = re.compile(
    r'^(?P<family>ZDS|ZE|ZY|ZS|ZT)(?P<seq>\d{3,5})(?:-(?P<sub>\d{1,2}))?$'
)

def _decode_z_family_short(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    family_meanings = {
        'ZDS': 'Z dump stack', 'ZE': 'Z elbow', 'ZY': 'Z Y-pipe',
        'ZS': 'Z stack', 'ZT': 'Z turbo pipe',
    }
    return {
        'pattern': 'z_series',
        'family': family,
        'family_meaning': family_meanings.get(family),
        'z_seq': m.group('seq'),
        'component_idx': m.group('sub'),
    }

# --- Numeric segment-style legacy SKUs (08-20124, 09-0826052) ---------
# Distinct from pure_numeric: these have explicit segment dashes
PAT_NUMERIC_SEGMENT = re.compile(
    r'^\d{2,3}-\d{4,7}(?:-[A-Z0-9]+)?$'
)

def _decode_numeric_segment(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'legacy_segmented',
        'family': 'LEGACY',
        'family_meaning': 'Legacy segmented numeric SKU',
        'requires_human_review': True,
    }

# --- 040PS-MB1-XBRL-08 multi-segment hyphenated component SKUs --------
PAT_MULTI_HYPHEN_COMP = re.compile(
    r'^\d{2,3}[A-Z]+(?:-[A-Z0-9]+){2,4}$'
)

def _decode_multi_hyphen_comp(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'multi_hyphen_component',
        'family': 'COMP',
        'family_meaning': 'Multi-segment component SKU',
        'requires_human_review': True,
    }

# --- G15 series flex extended forms (G15-2512.5, G15-3120EXP) ---------
PAT_G15_EXTENDED = re.compile(
    r'^G15-(?P<rest>[\d.]+(?:EX|EXP|BK)?)$'
)

def _decode_g15_extended(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'g_flex',
        'family': 'G',
        'family_meaning': 'Galvanized flex (.015 wall)',
        'g15_rest': m.group('rest'),
    }

# --- S-prefix tube extended (S35-120SBS4, S4-240SBS4 BARE-IN) ---------
# Wider catch than S-tube above; allows 'BARE', extension marker, and -IN
PAT_S_TUBE_EXTENDED = re.compile(
    r'^S(?P<diameter>\d+(?:\.\d+|-\d+)?)\s*-?\s*(?P<length>\d+)'
    r'(?P<rest>[A-Z][A-Z0-9\s\-]*)?$'
)

def _decode_s_tube_extended(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if '-' in d:
        # 3-1/2 = 3.5, etc.
        whole, frac = d.split('-', 1)
        diameter = float(whole) + float(frac) / 10
    else:
        diameter = float(d)
    return {
        'pattern': 's_tube_extended',
        'family': 'S',
        'family_meaning': 'Straight tube (extended form)',
        'diameter': diameter,
        'length': float(m.group('length')),
        's_rest': (m.group('rest') or '').strip(),
    }

# --- SP/SK with ceramic/koolthe modifiers (SP6-36SBC-CL, SK6-36SBC-5KT) -
# These are S-prefix reducers with -CL (ceramic-lined) or -5KT (Kool-Tube 5") modifiers
PAT_S_REDUCER_MODIFIERS = re.compile(
    r'^S(?P<base_family>BR|BH|WCK|SP|SK|SS|SA|SL|SBR|SBH|K|A|D|M|P|ZP|ZM|L|S)'
    r'(?P<inlet>\d+(?:\.\d+)?)'
    r'-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB)'
    r'(?P<finish>BBC|BS|S3|S4|BC|[ACPS])?'
    r'(?P<outlet_in>\d+(?:\.\d+)?)?'  # may have outlet diameter inline
    r'-(?P<modifier>L?\d+|CL|5KT|5KTL\d+|SURPLUS|\dKT|\d+CL|\d+ID|\d+OD|L\d+CL|\d+-CL|\dCL)$'
)

def _decode_s_reducer_modifiers(m: re.Match) -> dict[str, Any]:
    base = m.group('base_family')
    outlet_in = m.group('outlet_in')
    return {
        'pattern': 's_reducer',
        'family': base,
        'family_meaning': FAMILY_MEANINGS.get(base, base),
        'is_reducer': True,
        'inlet_diameter': float(m.group('inlet')),
        'diameter': float(m.group('inlet')),
        'outlet_diameter': float(outlet_in) if outlet_in else 5.0,
        'outlet_implicit': outlet_in is None,
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
        'ceramic_lined': 'CL' in m.group('modifier'),
        'kool_tube': 'KT' in m.group('modifier'),
    }

# --- T 2-digit diameter form (T35-5, T4-15) ---------------------------
PAT_T_DIAM = re.compile(
    r'^T(?P<diameter>\d{1,2})-(?P<sub>\d{1,3})$'
)

def _decode_t_diam(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 2 and d[1] == '5':
        diameter = float(d[0]) + 0.5
    else:
        diameter = float(d)
    return {
        'pattern': 't_sku',
        'family': 'T',
        'family_meaning': 'Turbo flare adapter',
        'diameter': diameter,
        't_sub': m.group('sub'),
    }

# --- T extended legacy (T407-4-S3, T461-4/90 EL, T3775-.110) ---------
PAT_T_LEGACY_LONG = re.compile(
    r'^T(?P<seq>\d{3,5})-?(?P<rest>.+)$'
)

def _decode_t_legacy_long(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 't_sku',
        'family': 'T',
        'family_meaning': 'Turbo / tube adapter (legacy long form)',
        't_seq': m.group('seq'),
        't_rest': m.group('rest'),
    }

# --- PS engine kit with letter prefix (PS-6.0-A4, PS-6.0-MP) ----------
PAT_PS_KIT_BROAD = re.compile(
    r'^PS-(?P<engine>[\d.]+)(?:\s+)?-?(?P<spec>[A-Z][A-Z0-9\s]*)$'
)

def _decode_ps_kit_broad(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ps_kit',
        'family': 'PS',
        'family_meaning': 'Powerstroke (Ford) kit',
        'oem': 'FT',
        'engine_displacement': m.group('engine'),
        'spec': m.group('spec').strip(),
    }

# --- RC Rain cap (RC-150, RC-200, RC-250) -----------------------------
PAT_RC_RAINCAP = re.compile(
    r'^RC-(?P<diameter>\d{2,4})(?P<finish>[A-Z]+)?$'
)

def _decode_rc_raincap(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 3:
        diameter = float(d[0]) + float(d[1:]) / 100
    elif len(d) == 4:
        diameter = float(d[:2]) + float(d[2:]) / 100
    else:
        diameter = float(d)
    return {
        'pattern': 'rc_raincap',
        'family': 'RC',
        'family_meaning': 'Rain cap',
        'diameter': diameter,
        'finish': m.group('finish'),
    }

# --- VB extended (VB-300E, VB-301I, VB-350I) --------------------------
# Already have basic VB; extend to accept embedded letter+number suffix
PAT_VB_EXTENDED = re.compile(
    r'^VB-(?P<diameter>\d+(?:\.\d+)?)(?P<suffix>[A-Z]+)?$'
)

def _decode_vb_extended(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 3 and '.' not in d:
        diameter = float(d[0]) + float(d[1:]) / 100
    else:
        diameter = float(d)
    return {
        'pattern': 'vb_clamp',
        'family': 'VB',
        'family_meaning': 'V-Band clamp',
        'diameter': diameter,
        'suffix': m.group('suffix'),
    }

# --- PF extended (PF-35ABT, PF-35AVP, PF-3ABT, PF-35SSEX) -------------
PAT_PF_EXTENDED = re.compile(
    r'^PF-(?P<diameter>\d{1,3})(?P<finish>SS|S3|S4|ZN|[A])(?P<modifier>BT|VP|EX|BK)?$'
)

def _decode_pf_extended(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 2 and d[1] == '5':
        diameter = float(d[0]) + 0.5
    elif len(d) == 3:
        diameter = float(d[0]) + float(d[1:]) / 100
    else:
        diameter = float(d)
    return {
        'pattern': 'pf_clamp',
        'family': 'PF',
        'family_meaning': 'Pre-Form clamp',
        'diameter': diameter,
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
    }

# --- AC Aerocab bracket (AC-0830-002, AC3-6KWB) ----------------------
PAT_AC_BRACKET = re.compile(
    r'^AC(?P<width>\d)?-?(?P<rest>[\d-]+(?:[A-Z][A-Z0-9]*)?)$'
)

def _decode_ac_bracket(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ac_bracket',
        'family': 'AC',
        'family_meaning': 'Aerocab bracket / emission component',
        'ac_rest': m.group('rest'),
    }

# --- D-prefix bracket (D31, D32, D33, D33-5, D34) --------------------
PAT_D_BRACKET = re.compile(
    r'^D(?P<seq>\d{2,3})(?:-(?P<sub>\d{1,2}))?$'
)

def _decode_d_bracket(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'd_bracket',
        'family': 'D',
        'family_meaning': 'Dodge bracket',
        'oem': 'DODGE',
        'd_seq': m.group('seq'),
        'd_sub': m.group('sub'),
    }

# --- J-prefix supplier SKU (J008671, J014621-185-147) ----------------
PAT_J_SUPPLIER = re.compile(
    r'^J(?P<seq>\d{6})(?:-(?P<sub1>\d{2,3}))?(?:-(?P<sub2>\d{2,3}))?$'
)

def _decode_j_supplier(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'j_supplier',
        'family': 'J',
        'family_meaning': 'J-prefix supplier SKU',
        'j_seq': m.group('seq'),
    }

# --- M long-form heat sleeve / pipe (M0305, M04-6001-0350X0240) -----
PAT_M_LONGFORM = re.compile(
    r'^M(?P<seq>\d{2,4})(?:-(?P<sub>\d{4,6}(?:-\d{4}X\d{4})?))?$'
)

def _decode_m_longform(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'm_muffler',
        'family': 'M',
        'family_meaning': 'Muffler / heat sleeve (long-form)',
        'm_seq': m.group('seq'),
        'm_sub': m.group('sub'),
    }

# --- RE Rolled End / Rubber Elbow (RE-345, RE4-10EXA, RE-445) -------
PAT_RE_ELBOW = re.compile(
    r'^RE-?(?P<diameter>\d{1,2}(?:\.\d+)?)?-?(?P<rest>[A-Z0-9]+)?$'
)

def _decode_re_elbow(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 're_elbow',
        'family': 'RE',
        'family_meaning': 'Rolled End / Rubber Elbow',
        're_rest': m.group('rest'),
    }

# --- RO Round saddle clamp (RO-158P, RO-15P, RO-175SS) -------------
PAT_RO_CLAMP = re.compile(
    r'^RO-(?P<diameter>\d{1,4})(?P<finish>SS|S3|S4|[ACPS])$'
)

def _decode_ro_clamp(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 3:
        diameter = float(d[0]) + float(d[1:]) / 100
    elif len(d) == 4:
        diameter = float(d[0]) + float(d[1:]) / 1000
    else:
        diameter = float(d)
    return {
        'pattern': 'ro_clamp',
        'family': 'RO',
        'family_meaning': 'Round saddle clamp',
        'diameter': diameter,
        'finish': m.group('finish'),
    }

# --- WFF Westfalia Flex hose ---------------------------------------
PAT_WFF = re.compile(
    r'^WFF-(?P<rest>[\d]+(?:[A-Z]+)?)$'
)

def _decode_wff(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'wff_flex',
        'family': 'WFF',
        'family_meaning': 'Westfalia Flex hose',
        'wff_rest': m.group('rest'),
    }

# --- WFC Westfalia Clamp ------------------------------------------
PAT_WFC = re.compile(
    r'^WFC-(?P<rest>[\d.]+(?:[A-Z]+(?:\s*REV[.\s]*\d*)?)?)$'
)

def _decode_wfc(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'wfc_clamp',
        'family': 'WFC',
        'family_meaning': 'Westfalia preformed clamp',
        'wfc_rest': m.group('rest'),
    }

# --- FK Flex Kit (FK-412, FK-412G) --------------------------------
PAT_FK_KIT = re.compile(
    r'^FK-(?P<diameter>\d)(?P<length>\d{2,3})(?P<material>G)?$'
)

def _decode_fk_kit(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'fk_kit',
        'family': 'FK',
        'family_meaning': 'Flex pipe kit',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'is_galvanized': m.group('material') == 'G',
    }

# --- HCAC / CCAC charge air cooler hose ---------------------------
PAT_CAC_HOSE = re.compile(
    r'^(?P<temp>HCAC|CCAC)-(?P<seq>\d{4,6})$'
)

def _decode_cac_hose(m: re.Match) -> dict[str, Any]:
    temp = m.group('temp')
    return {
        'pattern': 'cac_hose',
        'family': temp,
        'family_meaning': 'Hot CAC hose' if temp == 'HCAC' else 'Cold CAC hose',
        'cac_seq': m.group('seq'),
    }

# --- UCS / USS proprietary (single-customer) parts ----------------
# UCS = Universal Curved Stack (NORCO proprietary)
# USS = Universal Straight Stack (NORCO proprietary)
PAT_NORCO_STACK = re.compile(
    r'^(?P<family>UCS|USS)(?P<diameter>\d)(?P<length>\d{2,3})(?P<spec>[A-Z]+)$'
)

def _decode_norco_stack(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'norco_proprietary',
        'family': family,
        'family_meaning': 'NORCO proprietary stack' if family == 'UCS' else 'Universal straight stack',
        'is_proprietary': True,
        'proprietary_customer': 'NORCO',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'spec': m.group('spec'),
    }

# --- ZE / ZY / ZS / ZT / ZDS short form (already covered above) ---

# --- DPFVB DPF V-Band kit ---------------------------------------
PAT_DPFVB = re.compile(
    r'^DPFVB-(?P<seq>\d{3,4})$'
)

def _decode_dpfvb(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dpfvb_kit',
        'family': 'DPFVB',
        'family_meaning': 'DPF V-Band kit',
        'dpfvb_seq': m.group('seq'),
    }

# --- HH Hump hose (HH-3, HH-4, HH-5) -----------------------------
PAT_HH_HUMP = re.compile(
    r'^HH-(?P<diameter>\d{1,3})$'
)

def _decode_hh_hump(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 3:
        diameter = float(d[0]) + float(d[1:]) / 100
    else:
        diameter = float(d)
    return {
        'pattern': 'hh_hump',
        'family': 'HH',
        'family_meaning': 'Hump hose (rubber)',
        'diameter': diameter,
    }

# --- PT Protube (PT000158, PT001075) -----------------------------
PAT_PT_PROTUBE = re.compile(
    r'^PT(?P<seq>\d{6})$'
)

def _decode_pt_protube(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pt_protube',
        'family': 'PT',
        'family_meaning': 'Protube part',
        'pt_seq': m.group('seq'),
    }

# --- RF Relaxed Flex hose (RF-2512.6, RF-309) -------------------
PAT_RF_FLEX = re.compile(
    r'^RF-(?P<rest>[\d.]+)$'
)

def _decode_rf_flex(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'rf_flex',
        'family': 'RF',
        'family_meaning': 'Relaxed-length flex hose',
        'rf_rest': m.group('rest'),
    }

# --- RMH Round Muffler Hanger ----------------------------------
PAT_RMH = re.compile(
    r'^RMH-(?P<diameter>\d{1,2})$'
)

def _decode_rmh(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'rmh_hanger',
        'family': 'RMH',
        'family_meaning': 'Round muffler hanger',
        'diameter': float(m.group('diameter')),
    }

# --- ARG / ESM Aerocab Replacement / Equivalent Stack Muffler --
PAT_ARG_ESM = re.compile(
    r'^(?P<family>ARG|ESM)-(?P<rest>[\dA-Z-]+)$'
)

def _decode_arg_esm(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'arg_esm_muffler',
        'family': family,
        'family_meaning': 'Aerocab replacement muffler' if family == 'ARG' else 'Equivalent stack muffler',
        'rest': m.group('rest'),
    }

# --- TR Tube Round (TR5120CR, TR6120CR-14GA) -----------------
PAT_TR_TUBE = re.compile(
    r'^TR(?P<diameter>\d)(?P<length>\d{3})(?P<spec>[A-Z]+)?(?:-(?P<gauge>\d+GA))?$'
)

def _decode_tr_tube(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'tr_tube',
        'family': 'TR',
        'family_meaning': 'Tube Round (cold-rolled)',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'spec': m.group('spec'),
        'gauge': m.group('gauge'),
    }

# --- TRF Turbo Repair Flange (TRF-425, TRF-450) ---------------
PAT_TRF = re.compile(
    r'^TRF-(?P<rest>[\d-]+(?:[A-Z]+)?)$'
)

def _decode_trf(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'trf_flange',
        'family': 'TRF',
        'family_meaning': 'Turbo repair flange',
        'trf_rest': m.group('rest'),
    }

# --- TSL Tapered SL elbow ----------------------------------------
PAT_TSL = re.compile(
    r'^TSL(?P<diameter>\d)(?P<angle>\d{2,3})-'
    r'(?P<legs>\d{4})(?P<od>S)?(?P<finish>[ACPS])?'
    r'(?:-(?P<modifier>\dID|\dOD|\d|CL))?$'
)

def _decode_tsl(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'tsl_tapered_elbow',
        'family': 'TSL',
        'family_meaning': 'Tapered SL elbow',
        'diameter': float(m.group('diameter')),
        'angle': int(m.group('angle')),
        'legs': m.group('legs'),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
    }

# --- ZE / ZY / ZS / ZT / ZDS already handled in z_dash_finish ---

# --- US41 stack (West Coast Cut chrome stack) -------------------
PAT_US_STACK = re.compile(
    r'^US(?P<diameter>\d{2})-(?P<rest>\d+)$'
)

def _decode_us_stack(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    diameter = float(d[0]) + float(d[1:]) / 10
    return {
        'pattern': 'us_stack',
        'family': 'US',
        'family_meaning': 'West Coast Cut stack',
        'diameter': diameter,
        'rest': m.group('rest'),
    }

# --- AC standalone bracket (AC3-6KWB style with width-prefix) ---
# Already partly covered in PAT_AC_BRACKET above

# --- HD heatshield / heavy-duty (HD-25ZN, HD17545) -------------
PAT_HD_EXTENDED = re.compile(
    r'^HD-?(?P<rest>[\d]+(?:[A-Z]+)?)$'
)

def _decode_hd_extended(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hd_sku',
        'family': 'HD',
        'family_meaning': 'Heavy-duty hardware/hanger',
        'hd_rest': m.group('rest'),
    }

# --- CSP Cab Side Pipe (CSP-41218SA, CSP-460EXC) ---------------
PAT_CSP_PIPE = re.compile(
    r'^CSP-(?P<rest>\d+(?:[A-Z]+)?)$'
)

def _decode_csp_pipe(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'csp_pipe',
        'family': 'CSP',
        'family_meaning': 'Cab side pipe',
        'csp_rest': m.group('rest'),
    }

# --- F-prefix Ford bracket (F41, F42, F4A, F4MP) ----------------
PAT_F_FORD_BRACKET = re.compile(
    r'^F(?P<rest>\d+[A-Z]*)$'
)

def _decode_f_ford_bracket(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'f_ford_bracket',
        'family': 'F',
        'family_meaning': 'Ford bracket / Ford-specific part',
        'oem': 'FT',
        'f_rest': m.group('rest'),
    }

# --- BT Box T-divert (BT-4A, BT-4EXA, BT-54A) -------------------
PAT_BT_BOX = re.compile(
    r'^BT-(?P<rest>\d+[A-Z]+)$'
)

def _decode_bt_box(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'bt_box',
        'family': 'BT',
        'family_meaning': 'Box T-divert',
        'bt_rest': m.group('rest'),
    }

# --- ED dump elbow (ED-4LA, ED-4LC) ----------------------------
PAT_ED_DUMP = re.compile(
    r'^ED-(?P<rest>\d+[A-Z]+)$'
)

def _decode_ed_dump(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ed_dump',
        'family': 'ED',
        'family_meaning': 'Dump-stack elbow',
        'ed_rest': m.group('rest'),
    }

# --- DTS dump stack (DTS-4A, DTS-5A) ---------------------------
PAT_DTS_DUMP = re.compile(
    r'^DTS-(?P<rest>\d+[A-Z]+)$'
)

def _decode_dts_dump(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dts_dump',
        'family': 'DTS',
        'family_meaning': 'Dump truck stack',
        'dts_rest': m.group('rest'),
    }

# --- JDS dump (JDS-4A) ----------------------------------------
PAT_JDS_DUMP = re.compile(
    r'^JDS-(?P<rest>\d+[A-Z]+)$'
)

def _decode_jds_dump(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'jds_dump',
        'family': 'JDS',
        'family_meaning': 'JDS dump stack',
        'jds_rest': m.group('rest'),
    }

# --- FEC dump elbow (FEC-4SC, FEC-5KA) ------------------------
PAT_FEC_DUMP = re.compile(
    r'^FEC-(?P<rest>\d+[A-Z]+)$'
)

def _decode_fec_dump(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'fec_dump',
        'family': 'FEC',
        'family_meaning': 'FEC dump elbow',
        'fec_rest': m.group('rest'),
    }

# --- OB OD Bottom dump (OB-460A, OB-560A, OB-5601218EXC) ------
PAT_OB_DUMP = re.compile(
    r'^OB-(?P<rest>\d+[A-Z]+)$'
)

def _decode_ob_dump(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ob_dump',
        'family': 'OB',
        'family_meaning': 'OD-bottom dump stack',
        'ob_rest': m.group('rest'),
    }

# --- YB / YC Y-pipe variants ----------------------------------
PAT_Y_VARIANT = re.compile(
    r'^(?P<family>YB|YC)-(?P<rest>\d+[A-Z]*(?:-S\d)?)$'
)

def _decode_y_variant(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'y_pipe',
        'family': family,
        'family_meaning': 'B-style Y-pipe' if family == 'YB' else 'C-style Y-pipe',
        'y_rest': m.group('rest'),
    }

# --- AT Air T-bolt clamp (AT-3SS, AT-35SS) -------------------
PAT_AT_CLAMP = re.compile(
    r'^AT-(?P<diameter>\d{1,3})(?P<finish>SS|S3|S4|[ACPS])$'
)

def _decode_at_clamp(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 2 and d[1] == '5':
        diameter = float(d[0]) + 0.5
    elif len(d) == 3:
        diameter = float(d[0]) + float(d[1:]) / 10
    else:
        diameter = float(d)
    return {
        'pattern': 'at_clamp',
        'family': 'AT',
        'family_meaning': 'Air T-bolt clamp',
        'diameter': diameter,
        'finish': m.group('finish'),
    }

# --- EB Bellows (EB-310SS, EB-3510SS) ------------------------
PAT_EB_BELLOWS = re.compile(
    r'^EB-(?P<rest>\d+[A-Z]+)$'
)

def _decode_eb_bellows(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'eb_bellows',
        'family': 'EB',
        'family_meaning': 'Bellows',
        'eb_rest': m.group('rest'),
    }

# --- AD Adapter / Flange (AD-311, AD-411MF, AD-460EX) -------
PAT_AD_FLANGE = re.compile(
    r'^AD-(?P<rest>\d+[A-Z]*)$'
)

def _decode_ad_flange(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ad_flange',
        'family': 'AD',
        'family_meaning': 'Adapter/Flange',
        'ad_rest': m.group('rest'),
    }

# --- ARP / ESP patches ---------------------------------------
PAT_PATCH = re.compile(
    r'^(?P<family>ARP|ESP)-(?P<rest>[A-Z0-9]+)$'
)

def _decode_patch(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'patch',
        'family': family,
        'family_meaning': 'Pipe patch',
        'rest': m.group('rest'),
    }

# --- ES Sam Reed proprietary (ES-430PC) -----------------
PAT_ES_SHUSTER = re.compile(
    r'^ES-(?P<rest>\d+[A-Z]+)$'
)

def _decode_es_shuster(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'es_proprietary',
        'family': 'ES',
        'family_meaning': 'Sam Reed proprietary',
        'is_proprietary': True,
        'proprietary_customer': 'Sam Reed',
        'es_rest': m.group('rest'),
    }

# --- QP Quiet Performance (QP-10LC, QP BOLTS, QP DISPLAY) ----
PAT_QP_PRODUCT = re.compile(
    r'^QP[\s-](?P<rest>[A-Z0-9]+)$'
)

def _decode_qp_product(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'qp_product',
        'family': 'QP',
        'family_meaning': 'Quiet Performance product',
        'qp_rest': m.group('rest'),
    }

# --- VK Insert / dampener (VK-5, VK-6, VK-6 BRKT) ------------
PAT_VK_INSERT = re.compile(
    r'^VK-(?P<rest>\d+(?:-\w+)?(?:\s+\w+)?)$'
)

def _decode_vk_insert(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'vk_insert',
        'family': 'VK',
        'family_meaning': 'Internal dampener insert',
        'vk_rest': m.group('rest'),
    }

# --- HDT heavy-duty truck (HDT-49090A, HDT-4A) ---------------
PAT_HDT = re.compile(
    r'^HDT(?P<diameter>\d)?-?(?P<rest>\d*[A-Z]+\s*[A-Z]*)$'
)

def _decode_hdt(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hdt_y_pipe',
        'family': 'HDT',
        'family_meaning': 'Heavy-duty truck Y-pipe',
        'rest': m.group('rest'),
    }

# --- PB-prefix Pete OEM-mirror (PB601726WORC, PB613056WORC) -
PAT_PB_LONGFORM = re.compile(
    r'^PB(?P<diameter>\d)(?P<seq>\d{4,5})(?P<suffix>[A-Z]+)?$'
)

def _decode_pb_longform(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pb_longform',
        'family': 'PB',
        'family_meaning': 'Peterbilt OEM-mirror (longform)',
        'oem': 'PB',
        'oem_meaning': 'Peterbilt',
        'diameter': float(m.group('diameter')),
        'pb_seq': m.group('seq'),
        'suffix': m.group('suffix'),
    }

# --- FNT FreightlinerNorthernTransit (FNT-152414014) --------
PAT_FNT = re.compile(
    r'^FNT-?(?P<seq>\d+(?:-\d+)?)$'
)

def _decode_fnt(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'fnt_pipe',
        'family': 'FNT',
        'family_meaning': 'FNT pipe (Freightliner OEM-mirror style)',
        'fnt_seq': m.group('seq'),
    }

# --- E-prefix Active Exhaust component (E1-052-003, E213-F4TP) -
PAT_E_COMPONENT = re.compile(
    r'^E(?P<seq>\d+)-?(?P<sub>\d+)?-?(?P<rest>[A-Z0-9-]*)$'
)

def _decode_e_component(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'e_component',
        'family': 'E',
        'family_meaning': 'E-prefix component',
        'e_seq': m.group('seq'),
    }

# --- DPU 5-inlet variants (DPU-5SK-5I, DPU-6SK-5I) -----------
PAT_DPU_INLET = re.compile(
    r'^DPU-(?P<rest>[\dA-Z-]+)$'
)

def _decode_dpu_inlet(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dpu_kit',
        'family': 'DPU',
        'family_meaning': 'Diesel pickup kit',
        'dpu_rest': m.group('rest'),
    }

# --- DIM Direct-fit Inverted Muffler (DIM-121L-5A, DIM-5M) ----
PAT_DIM = re.compile(
    r'^DIM-(?P<rest>[\dA-Z-]+)$'
)

def _decode_dim(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dim_muffler',
        'family': 'DIM',
        'family_meaning': 'Direct-fit muffler kit',
        'dim_rest': m.group('rest'),
    }

# --- SPU Single Pickup (SPU-4A, SPU-4SK) ---------------------
PAT_SPU_KIT = re.compile(
    r'^SPU-(?P<rest>\d+[A-Z]+(?:-\d+[A-Z]*)?)$'
)

def _decode_spu_kit(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'spu_kit',
        'family': 'SPU',
        'family_meaning': 'Single pickup kit',
        'spu_rest': m.group('rest'),
    }

# --- GH Grab Handle (GH-18C, GH-24S) -----------------------
PAT_GH_HANDLE = re.compile(
    r'^GH-(?P<diameter>\d{1,3})(?P<finish>[ACPS])$'
)

def _decode_gh_handle(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'gh_handle',
        'family': 'GH',
        'family_meaning': 'Grab handle',
        'length': float(m.group('diameter')),
        'finish': m.group('finish'),
    }

# --- H- Hanger (H-10, H-12, H-450F) ---------------------------
PAT_H_HANGER = re.compile(
    r'^H-(?P<rest>\d+[A-Z]*)$'
)

def _decode_h_hanger(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'h_hanger',
        'family': 'H',
        'family_meaning': 'Hanger',
        'h_rest': m.group('rest'),
    }

# --- PH Pipe Hanger (PH-3, PH-4A) ----------------------------
PAT_PH_HANGER = re.compile(
    r'^PH-(?P<diameter>\d{1,2})(?P<suffix>[A-Z]?)$'
)

def _decode_ph_hanger(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 2 and d[1] == '5':
        diameter = float(d[0]) + 0.5
    else:
        diameter = float(d)
    return {
        'pattern': 'ph_hanger',
        'family': 'PH',
        'family_meaning': 'Pipe hanger',
        'diameter': diameter,
    }

# --- GF Galvanized Flex (now-deprecated, points to G15) -------
PAT_GF_DEPRECATED = re.compile(
    r'^GF-(?P<rest>[\d.]+)$'
)

def _decode_gf_deprecated(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'gf_deprecated',
        'family': 'GF',
        'family_meaning': 'Galvanized flex (deprecated; use G15)',
        'gf_rest': m.group('rest'),
        'is_deprecated': True,
    }

# --- GRIPF GR pre-form clamp (GRIPF-35A) ----------------------
PAT_GRIPF = re.compile(
    r'^GRIPF-(?P<rest>\d+[A-Z]+)$'
)

def _decode_gripf(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'gripf_clamp',
        'family': 'GRIPF',
        'family_meaning': 'GR pre-form clamp',
        'gripf_rest': m.group('rest'),
    }

# --- FTE / FTE2 Ford-truck-exhaust kit (FTE-5SK, FTE2-5A-212C) -
PAT_FTE = re.compile(
    r'^FTE\d?-?(?P<rest>[\dA-Z-]+)$'
)

def _decode_fte(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'fte_kit',
        'family': 'FTE',
        'family_meaning': 'Ford truck exhaust kit',
        'oem': 'FT',
        'fte_rest': m.group('rest'),
    }

# --- HB Heat divert Box (HB-4, HB-4SS, HB-SPRING) -------------
PAT_HB = re.compile(
    r'^HB-?(?P<rest>[A-Z0-9]+)$'
)

def _decode_hb(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hb_diverter',
        'family': 'HB',
        'family_meaning': 'Heat diverter box',
        'hb_rest': m.group('rest'),
    }

# --- MI Marson (MI-1227, MI2155) -------------------------------
PAT_MI_MARSON = re.compile(
    r'^MI-?(?P<seq>\d{4})$'
)

def _decode_mi_marson(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'mi_marson',
        'family': 'MI',
        'family_meaning': 'Marson part',
        'mi_seq': m.group('seq'),
    }

# --- MY Muffler Y (MY-055, MY-944) -----------------------------
PAT_MY_MUFFLER = re.compile(
    r'^MY-(?P<rest>[\dA-Z\s]+)$'
)

def _decode_my_muffler(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'my_muffler',
        'family': 'MY',
        'family_meaning': 'Muffler Y-pipe',
        'my_rest': m.group('rest'),
    }

# --- PAC Pivot bushing (PAC-1009, PAC-6004CPK) -----------------
PAT_PAC_PIVOT = re.compile(
    r'^PAC-(?P<rest>\d+[A-Z]*)$'
)

def _decode_pac_pivot(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pac_bushing',
        'family': 'PAC',
        'family_meaning': 'Pivot bushing kit',
        'pac_rest': m.group('rest'),
    }

# --- PDI Apex Diesel Inc proprietary (PDI548CPL-MF-L) --
PAT_PDI_PROPRIETARY = re.compile(
    r'^PDI(?P<rest>\d+[A-Z]+(?:-[A-Z\d]+)*)$'
)

def _decode_pdi_proprietary(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pdi_proprietary',
        'family': 'PDI',
        'family_meaning': 'Apex Diesel Inc proprietary',
        'is_proprietary': True,
        'proprietary_customer': 'Apex Diesel Inc (ADI)',
        'pdi_rest': m.group('rest'),
    }

# --- ZE / ZY / ZS / ZT family already covered by z_dash_finish/short ---

# --- C-prefix supplier mirror (C22587A, C22588C) -------------
PAT_C_SUPPLIER = re.compile(
    r'^C(?P<seq>\d{4,6})(?P<suffix>[A-Z0-9-]*)$'
)

def _decode_c_supplier(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'c_supplier',
        'family': 'C',
        'family_meaning': 'C-prefix supplier-mirror SKU',
        'c_seq': m.group('seq'),
    }

# --- B-prefix bellow (B56SS, B65-0531) ----------------------
PAT_B_BELLOW_LEGACY = re.compile(
    r'^B(?P<rest>\d{2,3}[A-Z0-9-]*)$'
)

def _decode_b_bellow_legacy(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'b_bellow_legacy',
        'family': 'B',
        'family_meaning': 'B-prefix bellow / legacy',
        'b_rest': m.group('rest'),
    }

# --- RD Forge Design proprietary (RD-3512EX, RD688S) ----------
PAT_RD_RAW = re.compile(
    r'^RD-?(?P<rest>[\dA-Z-]+(?:\s+REV\s+\d+)?)$'
)

def _decode_rd_raw(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'rd_forge_design',
        'family': 'RD',
        'family_meaning': 'Forge Design proprietary',
        'is_proprietary': True,
        'proprietary_customer': 'Forge Design',
        'rd_rest': m.group('rest'),
    }

# --- RRE Rubber Reducing Elbow (RRE-5590-5) -----------------
PAT_RRE = re.compile(
    r'^RRE-(?P<diameter>\d+(?:\.\d+)?)(?P<angle>\d{2,3})?-?(?P<reduce>\d+(?:\.\d+)?)?$'
)

def _decode_rre(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'rre_rubber_elbow',
        'family': 'RRE',
        'family_meaning': 'Rubber reducing elbow',
        'diameter': float(m.group('diameter')),
        'angle': int(m.group('angle')) if m.group('angle') else None,
        'reduce_to': float(m.group('reduce')) if m.group('reduce') else None,
    }

# --- RAW raw bender stock (catch-all) ----------------------
PAT_RAW_BEND = re.compile(
    r'^RAW\s+\d+(?:\.\d+)?["\s]*MISC.*BEND$',
    re.IGNORECASE
)

def _decode_raw_bend(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'raw_bend',
        'family': 'RAW',
        'family_meaning': 'Raw bend stock',
        'is_raw_material': True,
    }

# --- SD Scheid Diesel kit (SD-4180ABE, SD-F4) --------------
PAT_SD_KIT = re.compile(
    r'^SD-(?P<rest>[\dA-Z-]+)$'
)

def _decode_sd_kit(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sd_scheid',
        'family': 'SD',
        'family_meaning': 'Scheid Diesel kit',
        'sd_rest': m.group('rest'),
    }

# --- ST extended (ST-22295-000SR, ST-22323-000-S3) ----------
PAT_ST_EXTENDED = re.compile(
    r'^ST-(?P<seq>\d{4,6})(?:-(?P<sub>\d{3}))?(?P<finish>SR|-S3|-S4|-HD|[A-Z]+)?$'
)

def _decode_st_extended(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'st_sku',
        'family': 'ST',
        'family_meaning': 'ST series (stack/turbo extended)',
        'st_seq': m.group('seq'),
    }

# --- ZE / ZY / ZS / ZT short forms covered above ------------

# --- SBH ceramic-lined explicit (SBH7-36SBC5-CL with embedded outlet) ---
# Already covered by S-reducer modifiers; this catches SBH7-36SBC5-CL form
PAT_S_REDUCER_EMBEDDED_OUTLET = re.compile(
    r'^S(?P<base_family>BR|BH|WCK|K|A|D|M|P|L|S)'
    r'(?P<inlet>\d+(?:\.\d+)?)'
    r'-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB)'
    r'(?P<finish>BBC|BS|S3|S4|[ACPS])'
    r'(?P<outlet>\d+(?:\.\d+)?)?'
    r'-(?P<modifier>CL|5KT|\d+CL|\dID|\dOD|\dKT)$'
)

def _decode_s_reducer_embedded_outlet(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 's_reducer',
        'family': m.group('base_family'),
        'family_meaning': FAMILY_MEANINGS.get(m.group('base_family'), m.group('base_family')),
        'is_reducer': True,
        'inlet_diameter': float(m.group('inlet')),
        'diameter': float(m.group('inlet')),
        'outlet_diameter': float(m.group('outlet')) if m.group('outlet') else 5.0,
        'outlet_implicit': m.group('outlet') is None,
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
        'ceramic_lined': 'CL' in m.group('modifier'),
        'kool_tube': 'KT' in m.group('modifier'),
    }

# --- SS variant with -NCL embedded outlet (SS6-40SBC-4CL) -----
# S + S (Straight) + 6" inlet, length 40, OD chrome, reduced to 4 ceramic-lined
# Pattern: SS{D}-{L}{body}{finish}-{outlet_with_CL}
PAT_SS_REDUCER_NCL = re.compile(
    r'^SS(?P<inlet>\d+)-(?P<length>\d+)'
    r'(?P<body>SB|EX|XB)(?P<finish>BBC|BS|S3|S4|[ACPS])'
    r'-(?P<outlet>\d+)?(?P<modifier>CL)$'
)

def _decode_ss_reducer_ncl(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 's_reducer',
        'family': 'S',
        'family_meaning': 'Straight (reducing, ceramic-lined)',
        'is_reducer': True,
        'inlet_diameter': float(m.group('inlet')),
        'diameter': float(m.group('inlet')),
        'outlet_diameter': float(m.group('outlet')) if m.group('outlet') else 5.0,
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'ceramic_lined': True,
    }

# --- SWCK with -L5 embedded outlet (SWCK7-30SBC-L5CL) --------
PAT_SWCK_L_OUTLET = re.compile(
    r'^SWCK(?P<inlet>\d+)-(?P<length>\d+)'
    r'(?P<body>SB|EX|XB)(?P<finish>BBC|BS|S3|S4|[ACPS])'
    r'-L(?P<outlet>\d+)(?P<modifier>CL|X\d+L)?$'
)

def _decode_swck_l_outlet(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 's_reducer',
        'family': 'WCK',
        'family_meaning': 'West Coast Curve (reducing)',
        'is_reducer': True,
        'inlet_diameter': float(m.group('inlet')),
        'diameter': float(m.group('inlet')),
        'outlet_diameter': float(m.group('outlet')),
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'ceramic_lined': m.group('modifier') and 'CL' in m.group('modifier'),
    }

# --- Highly permissive elbow catch-all ---
# Catches any L{numeric}{angle/sub}{rest with letters and digits}
# This is intentionally permissive so the long tail of L-family SKUs
# all classify as elbow even if the encoding details aren't fully decoded.
PAT_ELBOW_PERMISSIVE = re.compile(
    r'^L(?P<rest>\d+(?:\.\d+)?[\d\-A-Z.]*)$'
)

def _decode_elbow_permissive(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'elbow_permissive',
        'family': 'L',
        'family_meaning': 'Elbow (permissive form)',
        'l_rest': m.group('rest'),
        'requires_human_review': False,
    }

# --- 2L prefix elbow (cosmetic-second elbow, but doesn't go through 2ND prefix) ---
# 2L590-1212EXEXC
PAT_2L_ELBOW = re.compile(
    r'^2L(?P<rest>\d+[\d\-A-Z.]*)$'
)

def _decode_2l_elbow(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': '2nd_elbow',
        'family': 'L',
        'family_meaning': 'Elbow (cosmetic-second)',
        'cosmetic_second': True,
        'l_rest': m.group('rest'),
    }

# --- 2NDCSP / 2NDDPFY etc. (cosmetic-seconds with non-standard family code) -
# Already covered by 2ND prefix recursion; expand if not currently caught
PAT_2ND_BROAD = re.compile(
    r'^2ND(?P<rest>[A-Z][A-Z0-9-]*)$'
)

def _decode_2nd_broad(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': '2nd_unknown',
        'family': 'UNKNOWN',
        'family_meaning': '2ND-prefix cosmetic second (family unmatched)',
        'cosmetic_second': True,
        'rest': m.group('rest'),
    }

# --- Numeric prefix free-text (7" BOX CAP, 5" SCRATCH & DENT) ---
PAT_NUMERIC_PRODUCT_DESC = re.compile(
    r'^\d+(?:\.\d+)?["\s].+',
    re.IGNORECASE
)

def _decode_numeric_product_desc(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'descriptive_legacy',
        'family': 'DESC',
        'family_meaning': 'Descriptive product name (legacy)',
        'requires_human_review': True,
    }

# --- M long-form muffler (M090074, M100465) -----------------------
# Matches M{6 digit} with no dash; broader than M_LONGFORM
PAT_M_FULL_LONG = re.compile(
    r'^M(?P<seq>\d{6})$'
)

def _decode_m_full_long(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'm_muffler',
        'family': 'M',
        'family_meaning': 'Muffler (full-length numeric)',
        'm_seq': m.group('seq'),
    }

# --- DC-600 series (DC-600-4SK) -----------------------------------
PAT_DC_600 = re.compile(
    r'^DC-(?P<seq>\d{2,4})-(?P<rest>[A-Z0-9]+)$'
)

def _decode_dc_600(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dc_kit',
        'family': 'DC',
        'family_meaning': 'Dodge Cummins kit (series)',
        'dc_series': m.group('seq'),
        'dc_rest': m.group('rest'),
    }

# --- DC simple (DC-47TP) -----------------------------------------
PAT_DC_SIMPLE = re.compile(
    r'^DC-(?P<rest>[\dA-Z]+)$'
)

def _decode_dc_simple(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dc_kit',
        'family': 'DC',
        'family_meaning': 'Dodge Cummins kit',
        'dc_rest': m.group('rest'),
    }

# --- FB extended (FB-35ZNEXP, FB-3ZNEXP, FB-4PEXP, FB-4ZN SADDLE) -
# Already FB-{D}{finish} but need to allow EXP / SADDLE / multi-letter
PAT_FB_EXTENDED = re.compile(
    r'^FB-(?P<diameter>\d{1,3})(?P<finish>SS|S3|S4|ZN|[ACPS])(?P<modifier>EXP|BK|[\sA-Z]+)?$'
)

def _decode_fb_extended(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 2 and d[1] == '5':
        diameter = float(d[0]) + 0.5
    elif len(d) == 3:
        diameter = float(d[0]) + float(d[1:]) / 100
    else:
        diameter = float(d)
    return {
        'pattern': 'fb_clamp',
        'family': 'FB',
        'family_meaning': 'Flat Bolt clamp',
        'diameter': diameter,
        'finish': m.group('finish'),
        'modifier': (m.group('modifier') or '').strip(),
    }

# --- Y- pipe with plates / S4 (Y-400 PLATES, Y-400S4, Y-350NPA-S4) -
PAT_Y_EXTENDED = re.compile(
    r'^Y-(?P<rest>[\dA-Z\s.]+(?:S4)?)$'
)

def _decode_y_extended(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'y_pipe',
        'family': 'Y',
        'family_meaning': 'Y-pipe',
        'y_rest': m.group('rest').strip(),
    }

# --- 152xx legacy SKU (15238171A, 15241-2001) ---------------------
PAT_15_LEGACY = re.compile(
    r'^15(?P<seq>\d{3,7})(?:-(?P<sub>[\dA-Z-]+))?(?P<suffix>[A-Z]+)?$'
)

def _decode_15_legacy(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': '15_legacy',
        'family': 'LEGACY',
        'family_meaning': 'Legacy 15-prefix SKU',
        'seq': m.group('seq'),
        'sub': m.group('sub'),
    }

# --- 09-08604xxx legacy West-Coast variant ------------------------
PAT_09_LEGACY = re.compile(
    r'^09-(?P<seq>\d{6,9})(?P<suffix>[A-Z]?)$'
)

def _decode_09_legacy(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': '09_legacy',
        'family': 'LEGACY',
        'family_meaning': 'Legacy 09-prefix SKU',
        'seq': m.group('seq'),
    }

# --- D-prefix Dodge bracket extended (D4A, D4T, D4T-2BF, D-44) ----
PAT_D_DODGE = re.compile(
    r'^D-?(?P<rest>\d+[A-Z]*(?:-[\dA-Z]+)?)$'
)

def _decode_d_dodge(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'd_dodge',
        'family': 'D',
        'family_meaning': 'Dodge part / bracket',
        'oem': 'DODGE',
        'd_rest': m.group('rest'),
    }

# --- OS Offset Stack mount bracket --------------------------------
PAT_OS_BRACKET = re.compile(
    r'^OS(?P<width>\d)?-(?P<rest>[\dA-Z]+)$'
)

def _decode_os_bracket(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'os_bracket',
        'family': 'OS',
        'family_meaning': 'Offset stack mount bracket',
        'os_rest': m.group('rest'),
    }

# --- SF flex with WC (welded with clamps) suffix ------------------
PAT_SF_WC = re.compile(
    r'^SF-(?P<diameter>\d)(?P<length>\d{2,3})(?P<suffix>WC|BK)$'
)

def _decode_sf_wc(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sf_flex',
        'family': 'SF',
        'family_meaning': 'Stainless Flex hose',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'suffix': m.group('suffix'),
    }

# --- AS extended with bulk/polish/economy modifiers ---------------
PAT_AS_EXTENDED = re.compile(
    r'^AS-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>SS|SS[MB]|S3|S4|[ACPS])(?P<modifier>BK|P|ECO|M)?$'
)

def _decode_as_extended(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'as_clamp',
        'family': 'AS',
        'family_meaning': 'Accuseal clamp',
        'diameter': float(m.group('diameter')),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
    }

# --- K with BK bulk-pack modifier or non-standard suffix -----------
PAT_K_BULK = re.compile(
    r'^K(?P<diameter>\d+(?:\.\d+)?)-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB)(?P<finish>BBC|BS|S3|S4|BC|SP|[ACPS])'
    r'(?P<modifier>BK|VP|EX)$'
)

def _decode_k_bulk(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'parametric',
        'family': 'K',
        'family_meaning': 'Curved (bulk-pack)',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
        'is_bulk': m.group('modifier') == 'BK',
    }

# --- 8NG / 7-EPK / xPK kit suffix forms ---------------------------
PAT_DIAMETER_KIT = re.compile(
    r'^(?P<diameter>\d)(?P<family>NG|EPK|PK|KWK|CK|SP|MJ)-?(?P<rest>[\dA-Z-]+)$'
)

def _decode_diameter_kit(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'diameter_prefix_kit',
        'family': family,
        'family_meaning': f'{family} kit (diameter-prefix)',
        'diameter': float(m.group('diameter')),
        'rest': m.group('rest'),
    }

# --- T6SP-52EXC-5 tapered spool pipe ------------------------------
PAT_T_SPOOL = re.compile(
    r'^T(?P<diameter>\d+)SP-(?P<length>\d+)(?P<body>SB|EX)(?P<finish>[ACPS])-(?P<outlet>\d+)$'
)

def _decode_t_spool(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 't_spool_pipe',
        'family': 'TSP',
        'family_meaning': 'Tapered spool pipe',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'outlet': float(m.group('outlet')),
    }

# --- 50DD machine drawing (50DD3146-BRKT REV. A) -------------------
PAT_DD_MACHINE = re.compile(
    r'^(?P<prefix>\d{2})DD\s*(?P<seq>\d{3,5})(?:-(?P<sub>[A-Z]+))?(?:\s+REV\.?\s*[A-Z])?$'
)

def _decode_dd_machine(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dd_machine',
        'family': 'DD',
        'family_meaning': 'Detroit Diesel / DD-prefix legacy SKU',
        'oem': 'DD',
        'oem_meaning': 'Detroit Diesel',
        'dd_prefix': m.group('prefix'),
        'dd_seq': m.group('seq'),
        'dd_sub': m.group('sub'),
    }

# --- Pure 4-digit numeric (9390, 9391, 9392) ----------------------
PAT_4DIGIT_NUMERIC = re.compile(r'^\d{4}$')

def _decode_4digit_numeric(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'numeric_4digit',
        'family': 'NUMERIC',
        'family_meaning': '4-digit legacy SKU',
        'requires_human_review': True,
    }

# --- US Unistrap clamp (US-4SS, US-5SS) --------------------------
PAT_US_CLAMP = re.compile(
    r'^US-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>SS|S3|S4|[ACPS])$'
)

def _decode_us_clamp(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'us_clamp',
        'family': 'US',
        'family_meaning': 'Unistrap clamp',
        'diameter': float(m.group('diameter')),
        'finish': m.group('finish'),
    }

# --- F-prefix Ford 5-digit (F5HS5246HA, F6HT-6K770-BD, F6HZ5246AA) -
PAT_F_FORD_LONG = re.compile(
    r'^F(?P<seq>\d[A-Z]{2,3}-?\d+(?:[A-Z]+)?)(?:-(?P<sub>[A-Z\d]+))?$'
)

def _decode_f_ford_long(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'f_ford',
        'family': 'F',
        'family_meaning': 'Ford OEM-mirror (long form)',
        'oem': 'FT',
        'f_seq': m.group('seq'),
    }

# --- RS connector with -S4 finish (RS-418-S4) --------------------
PAT_RS_S4 = re.compile(
    r'^RS-(?P<rest>\d+(?:-S\d)?(?:[A-Z]+)?)$'
)

def _decode_rs_s4(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'rs_connector',
        'family': 'RS',
        'family_meaning': 'Connector / Repair Section',
        'rs_rest': m.group('rest'),
    }

# --- SB longform (SB2-A212FT-S4, SB6-48158IH-A) ------------------
PAT_SB_LONGFORM = re.compile(
    r'^SB(?P<seq>\d)-(?P<rest>[A-Z\d-]+)$'
)

def _decode_sb_longform(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sb_school_bus',
        'family': 'SB',
        'family_meaning': 'School Bus part (long-form)',
        'sb_seq': m.group('seq'),
        'sb_rest': m.group('rest'),
    }

# --- 4FUZ (4" flat U-bolt clamp ZN) -------------------------------
PAT_FUZ = re.compile(
    r'^(?P<diameter>\d)FUZ$'
)

def _decode_fuz(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'fuz_clamp',
        'family': 'FUZ',
        'family_meaning': 'Flat U-bolt zinc clamp',
        'diameter': float(m.group('diameter')),
    }

# --- 2NDCSP / 2NDDPFY (2ND prefix variants) -----------------------
# Already handled, but final catch-all
# --- ZP with letter suffix (ZP2233-PLT) --------------------------
PAT_ZP_LETTER_SUFFIX = re.compile(
    r'^(?P<family>ZP|ZM)(?P<seq>\d{4,5})-(?P<sub>[A-Z]+)$'
)

def _decode_zp_letter_suffix(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'z_series',
        'family': m.group('family'),
        'family_meaning': FAMILY_MEANINGS.get(m.group('family')),
        'z_seq': m.group('seq'),
        'letter_suffix': m.group('sub'),
    }

# --- ZP with -N-CL/-NC ceramic suffix (ZP2174-2C-CL) -------------
PAT_ZP_CL = re.compile(
    r'^(?P<family>ZP|ZM)(?P<seq>\d{3,5})-(?P<sub>\d{1,2})(?P<finish>[ACPS])?-(?P<modifier>CL|\dCL)$'
)

def _decode_zp_cl(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'z_series',
        'family': m.group('family'),
        'family_meaning': FAMILY_MEANINGS.get(m.group('family')),
        'z_seq': m.group('seq'),
        'component_idx': m.group('sub'),
        'ceramic_lined': True,
    }

# --- 8 single-digit kit (8NG, 8PK) --------------------------------
# Already covered by PAT_DIAMETER_KIT

# --- SP6-60EX-S3 (S3 finish via -S3 suffix) ----------------------
PAT_PARAMETRIC_DASH_FINISH = re.compile(
    rf'^(?P<family>{_PARAMETRIC_FAMILY_GROUP}|SP|SK|SS|SA|SBR|SBH|SWCK)'
    r'(?P<diameter>\d+(?:\.\d+)?)'
    r'-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB)'
    r'-(?P<finish>S3|S4|SS|BBC|BS|BC|[ACPS])'
    r'(?:-(?P<modifier>[A-Z0-9]+))?$'
)

def _decode_parametric_dash_finish(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    is_s_reducer = family.startswith('S') and family != 'S' and family[1] in 'BPSWA'
    if is_s_reducer:
        base = family[1:]
        return {
            'pattern': 's_reducer',
            'family': base,
            'family_meaning': FAMILY_MEANINGS.get(base, base),
            'is_reducer': True,
            'inlet_diameter': float(m.group('diameter')),
            'diameter': float(m.group('diameter')),
            'outlet_diameter': 5.0,
            'outlet_implicit': True,
            'length': float(m.group('length')),
            'body': m.group('body'),
            'finish': m.group('finish'),
            'modifier': m.group('modifier'),
        }
    return {
        'pattern': 'parametric',
        'family': family,
        'family_meaning': FAMILY_MEANINGS.get(family, family),
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
    }

# ============================================================================
# Final coverage batch — long-tail clusters
# ============================================================================

# --- R-series flares and reducers (R4.125-RF, R5I-4I-S4, R5I-4OS4) ----
PAT_R_FLARE = re.compile(
    r'^R(?P<diameter>\d+(?:\.\d+)?)-(?P<rest>RF(?:-\d+(?:\.\d+)?)?(?:[A-Z]+)?)$'
)

def _decode_r_flare(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'r_flare',
        'family': 'R',
        'family_meaning': 'Radius-style flare',
        'diameter': float(m.group('diameter')),
        'rest': m.group('rest'),
    }

PAT_R_REDUCER_LETTER = re.compile(
    r'^R(?P<inlet>\d+(?:\.\d+)?)(?P<inlet_unit>I|O)?-?(?P<outlet>\d+(?:\.\d+)?)(?P<outlet_unit>I|O)?(?P<finish>S3|S4|SS|[ACPS])?$'
)

def _decode_r_reducer_letter(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'reducer_alt',
        'family': 'R',
        'family_meaning': 'Reducer (letter-unit form)',
        'is_reducer': True,
        'inlet_diameter': float(m.group('inlet')),
        'diameter': float(m.group('inlet')),
        'outlet_diameter': float(m.group('outlet')),
        'finish': m.group('finish'),
    }

# --- SL with embedded outlet (SL490-1820EXC3.5, SL690-1313SAID6) -----
PAT_SL_EMBEDDED_OUTLET = re.compile(
    r'^SL(?P<diameter>\d)(?P<angle>\d{2,3})-(?P<legs>\d{4})'
    r'(?P<body>SB|EX)?(?P<finish>[ACPS])(?P<outlet_unit>ID|OD)?'
    r'(?P<outlet>\d+(?:\.\d+)?)?(?:-(?P<modifier>CL))?$'
)

def _decode_sl_embedded_outlet(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sl_elbow_reducer',
        'family': 'L',
        'family_meaning': 'Elbow (SL reducer with embedded outlet)',
        'is_reducer': True,
        'diameter': float(m.group('diameter')),
        'inlet_diameter': float(m.group('diameter')),
        'outlet_diameter': float(m.group('outlet')) if m.group('outlet') else 5.0,
        'angle': int(m.group('angle')),
        'legs': m.group('legs'),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'ceramic_lined': m.group('modifier') == 'CL',
    }

# --- ZP with -NPN sub-component suffix (ZP6050-7P1) -------------------
PAT_ZP_PN_SUFFIX = re.compile(
    r'^(?P<family>ZP|ZM)(?P<seq>\d{4,5})-(?P<sub>\d{1,2}P\d)$'
)

def _decode_zp_pn_suffix(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'z_series',
        'family': m.group('family'),
        'family_meaning': FAMILY_MEANINGS.get(m.group('family')),
        'z_seq': m.group('seq'),
        'sub_component': m.group('sub'),
    }

# --- ZP with -NN-gauge (ZP6779-1-14GA) --------------------------------
PAT_ZP_GAUGE = re.compile(
    r'^(?P<family>ZP|ZM)(?P<seq>\d{4,5})-(?P<sub>\d{1,2})-(?P<gauge>\d+GA)$'
)

def _decode_zp_gauge(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'z_series',
        'family': m.group('family'),
        'family_meaning': FAMILY_MEANINGS.get(m.group('family')),
        'z_seq': m.group('seq'),
        'component_idx': m.group('sub'),
        'gauge': m.group('gauge'),
    }

# --- "AT PLATER" -P/-NP suffix work-in-progress SKUs ------------------
# SP7-108EX-5P, SK7-108EX-5P, SS7-108EX-5P, SBH7-108EX5P
PAT_AT_PLATER = re.compile(
    r'^(?P<family>SP|SK|SS|SA|SBR|SBH|SWCK|K|A|D|S|M|P|L|T|Y|BR|BH)'
    r'(?P<diameter>\d+(?:\.\d+)?)'
    r'-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB)'
    r'-?(?P<outlet>\d+)?P$'
)

def _decode_at_plater(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    is_s_reducer = family.startswith('S') and family != 'S' and len(family) > 1 and family[1] in 'BPSWAK'
    base = family[1:] if is_s_reducer else family
    return {
        'pattern': 's_reducer' if is_s_reducer else 'parametric',
        'family': base,
        'family_meaning': FAMILY_MEANINGS.get(base, base),
        'is_reducer': is_s_reducer,
        'diameter': float(m.group('diameter')),
        'inlet_diameter': float(m.group('diameter')),
        'outlet_diameter': float(m.group('outlet')) if m.group('outlet') else 5.0,
        'length': float(m.group('length')),
        'body': m.group('body'),
        'at_plater': True,
        'finish': 'P (at plater)',
    }

# --- MISC catalog items / PLATING service --------------------------
PAT_MISC_ITEM = re.compile(
    r'^MISC(?:\s|\.|$).*',
    re.IGNORECASE
)

def _decode_misc_item(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'freetext_or_admin',
        'family': 'MISC',
        'family_meaning': 'Miscellaneous item',
        'disregard': True,
    }

PAT_PLATING_SVC = re.compile(
    r'^PLATING\s.*',
    re.IGNORECASE
)

def _decode_plating_svc(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'service_line',
        'family': 'PLATING',
        'family_meaning': 'Plating service',
        'is_service': True,
        'disregard': True,
    }

# --- 5/110 STL-L tube with slash and direction (left/right) ---------
PAT_SLASH_TUBE = re.compile(
    r'^(?P<diameter>\d+(?:\.\d+)?)/(?P<length>\d+(?:\.\d+)?)\s+STL-(?P<side>L|R)$'
)

def _decode_slash_tube(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'slash_tube',
        'family': 'STL',
        'family_meaning': 'Slash-form tube (left/right)',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'side': m.group('side'),
    }

# --- M with body/component suffix (M-1136 BODY, M-580CT HANGER) -----
PAT_M_BODY = re.compile(
    r'^M-?(?P<seq>\d+(?:CT|TS)?)\s+(?P<sub>BODY|HANGER|KIT|BRKT)$'
)

def _decode_m_body(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'm_muffler',
        'family': 'M',
        'family_meaning': 'Muffler component',
        'm_seq': m.group('seq'),
        'm_sub': m.group('sub'),
    }

# --- M14-PORT, M2418G (catch various M variants) -------------------
PAT_M_GENERIC = re.compile(
    r'^M-?(?P<seq>\d+[A-Z\-]*(?:\s+[A-Z]+)*)$'
)

def _decode_m_generic(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'm_muffler',
        'family': 'M',
        'family_meaning': 'M-prefix muffler/component',
        'm_seq': m.group('seq'),
    }

# --- SP modifiers (SP3-9863BB, SP7-32SB-5RAW, SP7-36SB5CC) --------
PAT_SP_MODIFIERS = re.compile(
    r'^SP(?P<diameter>\d+(?:\.\d+)?)'
    r'-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB)?'
    r'(?P<finish>BBC|BS|S3|S4|BC|[ACPS])?'
    r'(?P<outlet>\d+)?'
    r'(?:-?(?P<modifier>CC|RAW|BB|CL|\dCC|\dCL|[A-Z]+))?$'
)

def _decode_sp_modifiers(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 's_reducer',
        'family': 'P',
        'family_meaning': 'Pipe (mitre stack, reducing)',
        'is_reducer': True,
        'diameter': float(m.group('diameter')),
        'inlet_diameter': float(m.group('diameter')),
        'outlet_diameter': float(m.group('outlet')) if m.group('outlet') else 5.0,
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
        'ceramic_coated': m.group('modifier') and 'CC' in m.group('modifier'),
        'ceramic_lined': m.group('modifier') and 'CL' in m.group('modifier'),
    }

# --- PB longform with REV / 2ND suffix (PB-13056 2ND, PB-15560 REV B) -
PAT_PB_REV = re.compile(
    r'^PB-?(?P<seq>\d{4,6})(?:\s+(?P<rev>2ND|REV[\s\.A-Z]*))?(?P<rest>[A-Z\-/]*)?$'
)

def _decode_pb_rev(m: re.Match) -> dict[str, Any]:
    is_2nd = m.group('rev') and '2ND' in m.group('rev').upper()
    return {
        'pattern': 'pb_longform',
        'family': 'PB',
        'family_meaning': 'Peterbilt OEM-mirror',
        'oem': 'PB',
        'oem_meaning': 'Peterbilt',
        'pb_seq': m.group('seq'),
        'cosmetic_second': bool(is_2nd),
    }

# --- Y-pipe with -OD or -CL or -PLT suffix (Y-500A-OD, Y-500C-CL) ---
PAT_Y_FULL = re.compile(
    r'^Y-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>[ACPS])-(?P<modifier>OD|ID|CL|PLT|S4|NPA[\-A-Z]*)$'
)

def _decode_y_full(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'y_pipe',
        'family': 'Y',
        'family_meaning': 'Y-pipe',
        'diameter': float(m.group('diameter')),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
        'ceramic_lined': m.group('modifier') == 'CL',
    }

# --- ACFM Aerocab Frame Mount bracket (ACFM-5, ACFM-6) -------------
PAT_ACFM = re.compile(
    r'^ACFM-(?P<diameter>\d+(?:\.\d+)?)$'
)

def _decode_acfm(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'acfm_bracket',
        'family': 'ACFM',
        'family_meaning': 'Aerocab Frame Mount bracket',
        'diameter': float(m.group('diameter')),
    }

# --- AC with KW/RW/LW suffix (AC-6KWLP-5.5) ------------------------
PAT_AC_KW = re.compile(
    r'^AC-?(?P<rest>[\dA-Z\-\.]+)$'
)

def _decode_ac_kw(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ac_bracket',
        'family': 'AC',
        'family_meaning': 'Aerocab bracket',
        'ac_rest': m.group('rest'),
    }

# --- AF Aluminized Flex (AF-35120, AF-472) ------------------------
PAT_AF_FLEX = re.compile(
    r'^AF-(?P<rest>\d+[A-Z]*)$'
)

def _decode_af_flex(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'af_flex',
        'family': 'AF',
        'family_meaning': 'Aluminized flex hose',
        'af_rest': m.group('rest'),
    }

# --- CBS multi-component kit (CBS-FL-ODS-S4, CBS-FL-ODS-S41) -------
PAT_CBS_KIT = re.compile(
    r'^CBS-(?P<rest>[A-Z\-\d]+)$'
)

def _decode_cbs_kit(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'cbs_kit',
        'family': 'CBS',
        'family_meaning': 'Multi-component CBS kit',
        'cbs_rest': m.group('rest'),
    }

# --- CG CO2 / Cold-rolled bend (CG-22-45-35) ----------------------
PAT_CG_BEND = re.compile(
    r'^CG-(?P<rest>[\d\-]+)$'
)

def _decode_cg_bend(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'cg_bend',
        'family': 'CG',
        'family_meaning': 'Cold-rolled compound bend',
        'cg_rest': m.group('rest'),
    }

# --- ES extended (ES-436PLC-45) -----------------------------------
PAT_ES_EXTENDED = re.compile(
    r'^ES-(?P<rest>\d+[A-Z]+(?:-\d+)?)$'
)

def _decode_es_extended(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'es_proprietary',
        'family': 'ES',
        'family_meaning': 'Sam Reed proprietary',
        'is_proprietary': True,
        'proprietary_customer': 'Sam Reed',
        'es_rest': m.group('rest'),
    }

# --- FL longform (FL-17476 2ND, FL6-09657-013) -------------------
PAT_FL_LONGFORM = re.compile(
    r'^FL-?(?P<seq>\d+)?-?(?P<sub>\d{3,5})?(?:-(?P<sub2>\d+))?(?P<finish>[ACPS])?(?:\s+(?P<rev>2ND|REV[\s\.A-Z]+))?$'
)

def _decode_fl_longform(m: re.Match) -> dict[str, Any]:
    is_2nd = m.group('rev') and '2ND' in m.group('rev').upper()
    return {
        'pattern': 'fl_oem_mirror',
        'family': 'FL',
        'family_meaning': 'Freightliner OEM-mirror',
        'oem': 'FL',
        'oem_meaning': 'Freightliner',
        'cosmetic_second': bool(is_2nd),
    }

# --- FS Flat Seal clamp (FS-3SS, FS-35SS) -------------------------
PAT_FS_CLAMP = re.compile(
    r'^FS-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>SS|S3|S4|[ACPS])$'
)

def _decode_fs_clamp(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 2 and d[1] == '5' and '.' not in d:
        diameter = float(d[0]) + 0.5
    else:
        diameter = float(d)
    return {
        'pattern': 'fs_clamp',
        'family': 'FS',
        'family_meaning': 'Flat Seal clamp',
        'diameter': diameter,
        'finish': m.group('finish'),
    }

# --- GBS Bellows flex (GBS-312, GBS-3512) ------------------------
PAT_GBS_FLEX = re.compile(
    r'^GBS-(?P<rest>\d+)$'
)

def _decode_gbs_flex(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'gbs_bellows',
        'family': 'GBS',
        'family_meaning': 'GBS bellows flex',
        'gbs_rest': m.group('rest'),
    }

# --- HD with letter+seq (HD-8769B-6, HD-8787B-S4, HD-4ZN-TICO) ----
PAT_HD_LONGFORM = re.compile(
    r'^HD-(?P<rest>[\dA-Z\-]+)$'
)

def _decode_hd_longform(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hd_sku',
        'family': 'HD',
        'family_meaning': 'Heavy-duty hardware (longform)',
        'hd_rest': m.group('rest'),
    }

# --- HF High Flow muffler (HF-1030, HF-1051) ----------------------
PAT_HF_MUFFLER = re.compile(
    r'^HF-(?P<rest>[\dA-Z\-]+)$'
)

def _decode_hf_muffler(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hf_muffler',
        'family': 'HF',
        'family_meaning': 'High-Flow muffler',
        'hf_rest': m.group('rest'),
    }

# --- IH OEM-mirror with space (IH-8521C5 2B) ----------------------
PAT_IH_LONGFORM = re.compile(
    r'^IH-?(?P<seq>\d+[A-Z]\d?)(?:\s+(?P<sub>[A-Z\-\d]+))?$'
)

def _decode_ih_longform(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ih_oem_mirror',
        'family': 'IH',
        'family_meaning': 'International Harvester OEM-mirror',
        'oem': 'IH',
        'oem_meaning': 'International Harvester',
        'ih_seq': m.group('seq'),
        'ih_sub': m.group('sub'),
    }

# --- HDT5A LINK X (component link variants) -----------------------
PAT_HDT_LINK = re.compile(
    r'^HDT(?P<rest>\d+[A-Z]?\s+(?:LINK\s+\d+|FLAP))$'
)

def _decode_hdt_link(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hdt_y_pipe',
        'family': 'HDT',
        'family_meaning': 'Heavy-duty truck Y-pipe component',
        'hdt_rest': m.group('rest'),
    }

# --- OK OEM kit (OK-1977, OK-1977-1) -----------------------------
PAT_OK_KIT = re.compile(
    r'^OK-(?P<rest>\d+(?:-\d+)?)$'
)

def _decode_ok_kit(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ok_kit',
        'family': 'OK',
        'family_meaning': 'OK-prefix kit',
        'ok_rest': m.group('rest'),
    }

# --- P parametric variants (P4-12ESWC, P4-4ABT, P5-60SBCBP) -------
PAT_P_VARIANTS = re.compile(
    r'^P(?P<diameter>\d+(?:\.\d+)?)'
    r'-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX|XB|ES)'
    r'(?P<finish>BBC|BS|S3|S4|BC|[ACPS])?'
    r'(?P<modifier>WC|BT|BP|BK|VP|EX)?$'
)

def _decode_p_variants(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'parametric',
        'family': 'P',
        'family_meaning': 'Pipe (mitre stack)',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
    }

# --- PRK BRKT/BOLT/NUT/WASHER components --------------------------
PAT_PRK_COMP = re.compile(
    r'^PRK\s+(?P<rest>[A-Z]+(?:\s+[A-Z]+)?)$'
)

def _decode_prk_comp(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'prk_kit',
        'family': 'PRK',
        'family_meaning': 'Peterbilt Retro Kit hardware',
        'prk_component': m.group('rest'),
    }

# --- RHH Rubber Hump Hose Reducer (RHH-5-4, RHH-6-5) ---------------
PAT_RHH = re.compile(
    r'^RHH-(?P<inlet>\d+(?:\.\d+)?)-(?P<outlet>\d+(?:\.\d+)?)$'
)

def _decode_rhh(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'rhh_rubber',
        'family': 'RHH',
        'family_meaning': 'Rubber hump hose reducer',
        'inlet_diameter': float(m.group('inlet')),
        'outlet_diameter': float(m.group('outlet')),
        'is_reducer': True,
    }

# --- SC Seal Clamp / Silent rain Cap (SC3-6SC, SC-450) -------------
PAT_SC_CLAMP = re.compile(
    r'^SC(?P<width>\d)?-(?P<rest>[\dA-Z]+)$'
)

def _decode_sc_clamp(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sc_clamp',
        'family': 'SC',
        'family_meaning': 'Seal clamp / Silent rain cap',
        'sc_rest': m.group('rest'),
    }

# --- STC Standard T-bolt Clamp (STC-3SS) ---------------------------
PAT_STC_CLAMP = re.compile(
    r'^STC-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>SS|S3|S4|[ACPS])$'
)

def _decode_stc_clamp(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 2 and d[1] == '5' and '.' not in d:
        diameter = float(d[0]) + 0.5
    else:
        diameter = float(d)
    return {
        'pattern': 'stc_clamp',
        'family': 'STC',
        'family_meaning': 'Standard T-bolt clamp',
        'diameter': diameter,
        'finish': m.group('finish'),
    }

# --- UB U-Bolt mount bracket (UB-2SS, UB-5PBS) ---------------------
PAT_UB_BRACKET = re.compile(
    r'^UB-(?P<rest>[\dA-Z]+)$'
)

def _decode_ub_bracket(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ub_bracket',
        'family': 'UB',
        'family_meaning': 'U-Bolt mount bracket',
        'ub_rest': m.group('rest'),
    }

# --- WB Wittke (WB-520C, WB-590A) ---------------------------------
PAT_WB_WITTKE = re.compile(
    r'^WB-(?P<rest>\d+[A-Z]+)$'
)

def _decode_wb_wittke(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'wb_wittke',
        'family': 'WB',
        'family_meaning': 'Wittke OEM-mirror',
        'wb_rest': m.group('rest'),
    }

# --- YC Y-pipe Type C with Z prefix (YC3Z-5246DA) ----------------
PAT_YC_LONGFORM = re.compile(
    r'^YC(?P<seq>\d?[A-Z]?)-(?P<rest>[\dA-Z\-]+)$'
)

def _decode_yc_longform(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'y_pipe',
        'family': 'YC',
        'family_meaning': 'C-style Y-pipe',
        'yc_rest': m.group('rest'),
    }

# --- Z Power Flex (Z02.500SSPFHW10.1125, Z02.50SSPFHW-5.25"NL) ---
PAT_Z_POWERFLEX = re.compile(
    r'^Z(?P<rest>\d+\.\d+SSPFHW.+)$'
)

def _decode_z_powerflex(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'z_powerflex',
        'family': 'Z',
        'family_meaning': 'Z Power Flex hose',
        'z_rest': m.group('rest'),
    }

# --- ZL Z-elbow longform (ZL101-1, ZL2500-1) ---------------------
PAT_ZL_ELBOW = re.compile(
    r'^ZL(?P<seq>\d{3,5})-(?P<sub>\d+)$'
)

def _decode_zl_elbow(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'z_series',
        'family': 'ZL',
        'family_meaning': 'Z-elbow',
        'z_seq': m.group('seq'),
        'sub': m.group('sub'),
    }

# --- BK/BP/MR/HSK hood stack kit components (BK5-HSK, BP5-HSK, MR5-HSK, HSK-5) -
PAT_HSK_COMP = re.compile(
    r'^(?P<family>BK|BP|MR|HSK|PGH)(?P<diameter>\d)(?:-(?P<sub>HSK|S))?$'
)

def _decode_hsk_comp(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    family_meanings = {
        'BK': 'Hood stack basket', 'BP': 'Hood stack bolt plate',
        'MR': 'Hood stack mount ring', 'HSK': 'Hood stack kit',
        'PGH': 'Polished pipe grab handle',
    }
    return {
        'pattern': 'hsk_component',
        'family': family,
        'family_meaning': family_meanings.get(family, family),
        'diameter': float(m.group('diameter')),
    }

# --- HSK basic (HSK-5, HSK-6) -------------------------------------
PAT_HSK_KIT = re.compile(
    r'^HSK-(?P<diameter>\d)$'
)

def _decode_hsk_kit(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hsk_kit',
        'family': 'HSK',
        'family_meaning': 'Hood stack kit',
        'diameter': float(m.group('diameter')),
    }

# --- BRT Brute miter tip (BRT5, BRT6) ----------------------------
PAT_BRT_TIP = re.compile(
    r'^BRT(?P<diameter>\d)$'
)

def _decode_brt_tip(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'brt_tip',
        'family': 'BRT',
        'family_meaning': 'Brute miter tip',
        'diameter': float(m.group('diameter')),
    }

# --- CN connector with -mm suffix (CN-312A-80MM, CN-510S4) -------
PAT_CN_LONGFORM = re.compile(
    r'^CN-(?P<rest>\d+(?:\.\d+)?[A-Z](?:-\d+MM)?(?P<finish>S\d|SS|[ACPS])?)$'
)

def _decode_cn_longform(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'cn_connector',
        'family': 'CN',
        'family_meaning': 'Connector',
        'cn_rest': m.group('rest'),
    }

# --- CS Cat Stack (CS-642, CS-742) -------------------------------
PAT_CS_STACK = re.compile(
    r'^CS-(?P<diameter>\d)(?P<length>\d{2,3})$'
)

def _decode_cs_stack(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'cs_stack',
        'family': 'CS',
        'family_meaning': 'Cat stack',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
    }

# --- CSP w/ explicit outlet diameter ------------------------------
PAT_CSP_EXTENDED = re.compile(
    r'^CSP(?P<diameter>\d)?-?(?P<rest>[\dA-Z\-]+)$'
)

def _decode_csp_extended(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'csp_pipe',
        'family': 'CSP',
        'family_meaning': 'Cab side pipe (extended)',
        'csp_rest': m.group('rest'),
    }

# --- DPFY DPF Y-pipe (DPFY-0622C, DPFY-0822C) --------------------
PAT_DPFY = re.compile(
    r'^DPFY-(?P<rest>\d+[A-Z]?)$'
)

def _decode_dpfy(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'dpfy_y_pipe',
        'family': 'DPFY',
        'family_meaning': 'DPF tapered Y-pipe',
        'dpfy_rest': m.group('rest'),
    }

# --- EEM Emergency Equipment Muffler (EEM-1876) ------------------
PAT_EEM = re.compile(
    r'^EEM-(?P<rest>[\dA-Z\s]+)$'
)

def _decode_eem(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'eem_muffler',
        'family': 'EEM',
        'family_meaning': 'Emergency Equipment Muffler kit',
        'eem_rest': m.group('rest'),
    }

# --- EKM Emergency Kit Muffler (EKM-1036) -------------------------
PAT_EKM = re.compile(
    r'^EKM-(?P<rest>\d+)$'
)

def _decode_ekm(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ekm_muffler',
        'family': 'EKM',
        'family_meaning': 'EKM universal muffler',
        'ekm_rest': m.group('rest'),
    }

# --- HS Heat Sleeve kit (HS-12, HS-18) ---------------------------
PAT_HS_KIT = re.compile(
    r'^HS-(?P<length>\d+)$'
)

def _decode_hs_kit(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hs_kit',
        'family': 'HS',
        'family_meaning': 'Heat sleeve kit',
        'length': float(m.group('length')),
    }

# --- IC Internal Coupler (IC-68CR) -------------------------------
PAT_IC_COUPLER = re.compile(
    r'^IC-(?P<rest>\d+[A-Z]+)$'
)

def _decode_ic_coupler(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ic_coupler',
        'family': 'IC',
        'family_meaning': 'Internal coupler',
        'ic_rest': m.group('rest'),
    }

# --- MMB Mass Mount Bracket clamp (MMB-10) ------------------------
PAT_MMB = re.compile(
    r'^MMB-(?P<diameter>\d+)$'
)

def _decode_mmb(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'mmb_clamp',
        'family': 'MMB',
        'family_meaning': 'MMB powder-coat clamp',
        'diameter': float(m.group('diameter')),
    }

# --- MPB Multi-Ply Bellow (MPB410SS) ------------------------------
PAT_MPB = re.compile(
    r'^MPB(?P<inlet>\d)(?P<length>\d{2})(?P<finish>SS|S3|S4|[ACPS])$'
)

def _decode_mpb(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'mpb_bellow',
        'family': 'MPB',
        'family_meaning': 'Multi-ply bellow',
        'diameter': float(m.group('inlet')),
        'length': float(m.group('length')),
        'finish': m.group('finish'),
    }

# --- MS deprecated (MS5-36SBC -> use A5-36SBC) -------------------
PAT_MS_DEPRECATED = re.compile(
    r'^MS(?P<rest>\d+(?:\.\d+)?-\d+(?:\.\d+)?(?:[A-Z]+)?)$'
)

def _decode_ms_deprecated(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ms_deprecated',
        'family': 'MS',
        'family_meaning': 'MS (deprecated; use A-equivalent)',
        'is_deprecated': True,
        'ms_rest': m.group('rest'),
    }

# --- PACSWR PACCAR weld-on reducer (PACSWR6-5, PACSWR7-5-6) ------
PAT_PACSWR = re.compile(
    r'^PACSWR(?P<inlet>\d)-(?P<outlet>\d)(?:-(?P<sub>\d))?$'
)

def _decode_pacswr(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pacswr_reducer',
        'family': 'PACSWR',
        'family_meaning': 'PACCAR weld-on reducer',
        'is_reducer': True,
        'inlet_diameter': float(m.group('inlet')),
        'outlet_diameter': float(m.group('outlet')),
    }

# --- PRKY Peterbilt Retro Kit Y-pipe (PRKY-13944) ----------------
PAT_PRKY = re.compile(
    r'^PRKY-(?P<rest>\d+(?:-[A-Z\d]+)*)$'
)

def _decode_prky(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'prky_y_pipe',
        'family': 'PRKY',
        'family_meaning': 'Peterbilt Retro Kit Y-pipe',
        'prky_rest': m.group('rest'),
    }

# --- PSC Pipe Stack/Slide-Carrier mount (PSC3-6PBS) ---------------
PAT_PSC = re.compile(
    r'^PSC(?P<width>\d)-(?P<rest>[\dA-Z]+)$'
)

def _decode_psc(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'psc_bracket',
        'family': 'PSC',
        'family_meaning': 'PSC stack slide bracket',
        'psc_rest': m.group('rest'),
    }

# --- PUB Power Up Bracket (PUB-L, PUB-R) -------------------------
PAT_PUB = re.compile(
    r'^PUB-(?P<rest>[A-Z](?:\s+[A-Z\s]+)?)$'
)

def _decode_pub(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pub_bracket',
        'family': 'PUB',
        'family_meaning': 'PUB DPU bracket',
        'pub_rest': m.group('rest'),
    }

# --- RTP Replaces / Roberts (RTP-477A, RTP-L10 MP) ---------------
PAT_RTP = re.compile(
    r'^RTP-(?P<rest>[A-Z\d\s]+)$'
)

def _decode_rtp(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'rtp_oem',
        'family': 'RTP',
        'family_meaning': 'RTP OEM replacement',
        'rtp_rest': m.group('rest'),
    }

# --- RU Ulrich (RU-590C, RU-5110C-2) ------------------------------
PAT_RU = re.compile(
    r'^RU-(?P<rest>[\dA-Z\-]+)$'
)

def _decode_ru(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ru_ulrich',
        'family': 'RU',
        'family_meaning': 'Ulrich-style elbow/stack',
        'ru_rest': m.group('rest'),
    }

# --- SV Service Vehicle (SV-3-3BE, SV-4-2BA) ---------------------
PAT_SV = re.compile(
    r'^SV-(?P<rest>[\dA-Z\-]+)$'
)

def _decode_sv(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sv_service',
        'family': 'SV',
        'family_meaning': 'Service vehicle pipe',
        'sv_rest': m.group('rest'),
    }

# --- TBE Tilt Bell (TBE-4) ----------------------------------------
PAT_TBE = re.compile(
    r'^TBE-(?P<diameter>\d+)$'
)

def _decode_tbe(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'tbe_bell',
        'family': 'TBE',
        'family_meaning': 'Tilt bell',
        'diameter': float(m.group('diameter')),
    }

# --- TL / TPB / TR variants (TL690-3127SC, TPB6-13056C-5) --------
PAT_TL_TAPERED = re.compile(
    r'^TL(?P<diameter>\d)(?P<angle>\d{2,3})-(?P<legs>\d{4})(?P<body>SB|EX)?(?P<finish>[ACPS])(?:-(?P<outlet>\d))?$'
)

def _decode_tl_tapered(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'tl_tapered',
        'family': 'TL',
        'family_meaning': 'Tapered L-elbow',
        'diameter': float(m.group('diameter')),
        'angle': int(m.group('angle')),
        'legs': m.group('legs'),
        'finish': m.group('finish'),
    }

PAT_TPB_TAPERED = re.compile(
    r'^TPB(?P<diameter>\d)-(?P<rest>[\dA-Z\-]+)$'
)

def _decode_tpb_tapered(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'tpb_tapered',
        'family': 'TPB',
        'family_meaning': 'Tapered Peterbilt elbow',
        'oem': 'PB',
        'diameter': float(m.group('diameter')),
        'tpb_rest': m.group('rest'),
    }

PAT_TR_LONGFORM = re.compile(
    r'^TR(?P<diameter>\d)(?P<length>\d{2,3})(?P<spec>[A-Z]+)$'
)

def _decode_tr_longform(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'tr_tube',
        'family': 'TR',
        'family_meaning': 'Tube round',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'spec': m.group('spec'),
    }

# --- UM Universal Muffler (UM-5LA, UM-5LEXC) ----------------------
PAT_UM_DUMP = re.compile(
    r'^UM-(?P<rest>\d+[A-Z]+)$'
)

def _decode_um_dump(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'um_dump',
        'family': 'UM',
        'family_meaning': 'Universal Muffler dump stack',
        'um_rest': m.group('rest'),
    }

# --- YDB Y-pipe Drop Bracket (YDB-21) -----------------------------
PAT_YDB = re.compile(
    r'^YDB-(?P<rest>\d+[A-Z]?(?:\s+MANUAL)?)$'
)

def _decode_ydb(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ydb_bracket',
        'family': 'YDB',
        'family_meaning': 'Y-pipe drop bracket',
        'ydb_rest': m.group('rest'),
    }

# --- APU Aspirator Unit (APU-1.5, APU-1.75) ----------------------
PAT_APU = re.compile(
    r'^APU-(?P<diameter>\d+(?:\.\d+)?)$'
)

def _decode_apu(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'apu_connector',
        'family': 'APU',
        'family_meaning': 'Aspirator unit connector',
        'diameter': float(m.group('diameter')),
    }

# --- B-prefix component (B9-1155TH, B9-3029IH) -------------------
PAT_B_COMPONENT = re.compile(
    r'^B(?P<seq>\d+)-(?P<sub>\d+[A-Z]+)$'
)

def _decode_b_component(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'b_component',
        'family': 'B',
        'family_meaning': 'B-prefix component',
        'b_seq': m.group('seq'),
        'b_sub': m.group('sub'),
    }

# --- 4U-B-S, 4WSBP (numeric-prefix saddle/U-bolt) -----------------
PAT_NUM_USADDLE = re.compile(
    r'^(?P<diameter>\d+(?:\.\d+)?)(?P<rest>U-B-[A-Z]+|WSBP)$'
)

def _decode_num_usaddle(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'num_saddle',
        'family': 'SADDLE',
        'family_meaning': 'U-bolt saddle / numeric-prefix saddle',
        'diameter': float(m.group('diameter')),
        'rest': m.group('rest'),
    }

# --- F94-41, F94-44 Ford OEM-mirror short ------------------------
PAT_F94 = re.compile(
    r'^F(?P<seq>\d+)-(?P<sub>\d+)$'
)

def _decode_f94(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'f_ford',
        'family': 'F',
        'family_meaning': 'Ford OEM-mirror short',
        'oem': 'FT',
        'f_seq': m.group('seq'),
        'f_sub': m.group('sub'),
    }

# --- "GRAB" merch components --------------------------------------
PAT_GRAB_MERCH = re.compile(
    r'^GRAB\s.*',
    re.IGNORECASE
)

def _decode_grab_merch(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'merch',
        'family': 'GRAB',
        'family_meaning': 'Grab handle merch / hardware',
        'is_merch': True,
    }

# ============================================================================
# Long-tail final patterns
# ============================================================================

# --- SL with full ALZ/CHR/ID outlet (SL690-1313SAID6, SL690-1313SCID6) -
PAT_SL_FULL_OUTLET = re.compile(
    r'^SL(?P<diameter>\d)(?P<angle>\d{2,3})-(?P<legs>\d{4})'
    r'(?P<body>SB|EX|S)?(?P<finish>[ACPS])(?P<unit>ID|OD)(?P<outlet>\d+(?:\.\d+)?)$'
)

def _decode_sl_full_outlet(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'sl_elbow_reducer',
        'family': 'L',
        'family_meaning': 'Elbow (SL with explicit outlet+unit)',
        'is_reducer': True,
        'diameter': float(m.group('diameter')),
        'inlet_diameter': float(m.group('diameter')),
        'outlet_diameter': float(m.group('outlet')),
        'angle': int(m.group('angle')),
        'legs': m.group('legs'),
        'finish': m.group('finish'),
        'outlet_unit': m.group('unit'),
    }

# --- G18 / G24 flex (G18-2512.5, G24-4120EXP) ----------------------
PAT_G18_FLEX = re.compile(
    r'^G(?P<thickness>\d{2})-(?P<rest>[\d.]+(?:[A-Z]+)?)$'
)

def _decode_g18_flex(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'g_flex',
        'family': 'G',
        'family_meaning': 'Galvanized flex',
        'thickness': m.group('thickness'),
        'rest': m.group('rest'),
    }

# --- G18-16 120 (with space) --------------------------------------
PAT_G_FLEX_SPACE = re.compile(
    r'^G(?P<thickness>\d{2})-(?P<diam>\d+)\s+(?P<length>\d+)$'
)

def _decode_g_flex_space(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'g_flex',
        'family': 'G',
        'family_meaning': 'Galvanized flex (space-separated)',
        'thickness': m.group('thickness'),
        'diameter': float(m.group('diam')),
        'length': float(m.group('length')),
    }

# --- EBK with C-suffix (EBK-3846462C5, EBK-4061C1) ----------------
PAT_EBK_C = re.compile(
    r'^EBK-(?P<seq>\d+C\d+)$'
)

def _decode_ebk_c(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ebk_bellows',
        'family': 'EBK',
        'family_meaning': 'EBK bellows kit (C-suffix)',
        'ebk_seq': m.group('seq'),
    }

# --- M-prefix bolt/screw (M82580025A2000, M8C110HCSSS) -----------
PAT_M_BOLT = re.compile(
    r'^M(?P<seq>\d+[A-Z\d]+)$'
)

def _decode_m_bolt(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'm_bolt',
        'family': 'M',
        'family_meaning': 'Metric bolt / screw (hardware)',
        'is_hardware': True,
        'm_seq': m.group('seq'),
    }

# --- ZP with descriptive component suffix (ZP8176-1 CN-PYRO, ZP8176-1 UNION, ZP8176-1 Y-PIPE) -
PAT_ZP_DESC = re.compile(
    r'^(?P<family>ZP|ZM)(?P<seq>\d{4,5})-(?P<sub>\d+)\s+(?P<descr>[A-Z\-]+)$'
)

def _decode_zp_desc(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'z_series',
        'family': m.group('family'),
        'family_meaning': FAMILY_MEANINGS.get(m.group('family')),
        'z_seq': m.group('seq'),
        'component_idx': m.group('sub'),
        'description_suffix': m.group('descr'),
    }

# --- K with SP-finish (polished) (K5-36SPS3) ----------------------
# This is K5-36 SP S3 = K(curved) 5" 36" body=SP finish=S3
PAT_K_SP_S = re.compile(
    r'^(?P<family>K|A|D|M|P|S)(?P<diameter>\d+(?:\.\d+)?)'
    r'-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SP|XB|SB|EX)'
    r'(?P<finish>S3|S4|SS|[ACPS])$'
)

def _decode_k_sp_s(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'parametric',
        'family': m.group('family'),
        'family_meaning': FAMILY_MEANINGS.get(m.group('family'), m.group('family')),
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'polished': m.group('body') == 'SP',
    }

# --- R8-5HW reducer with HW (heavy-wall) suffix -------------------
PAT_R_HW = re.compile(
    r'^R(?P<inlet>\d+)-(?P<outlet>\d+)(?P<modifier>HW[L]?)?$'
)

def _decode_r_hw(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'reducer_alt',
        'family': 'R',
        'family_meaning': 'Reducer (heavy-wall)',
        'is_reducer': True,
        'inlet_diameter': float(m.group('inlet')),
        'outlet_diameter': float(m.group('outlet')),
        'modifier': m.group('modifier'),
    }

# --- PF with SSP modifier (PF-5SSP) -------------------------------
PAT_PF_SSP = re.compile(
    r'^PF-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>SS|S3|S4|[ACPS])(?P<modifier>P)?$'
)

def _decode_pf_ssp(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pf_clamp',
        'family': 'PF',
        'family_meaning': 'Pre-Form clamp',
        'diameter': float(m.group('diameter')),
        'finish': m.group('finish'),
        'polished': m.group('modifier') == 'P',
    }

# --- PF-5 (no finish suffix; deprecated -> PF-5SS) ---------------
PAT_PF_BARE = re.compile(
    r'^PF-(?P<diameter>\d+(?:\.\d+)?)$'
)

def _decode_pf_bare(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pf_clamp',
        'family': 'PF',
        'family_meaning': 'Pre-Form clamp (bare diameter)',
        'diameter': float(m.group('diameter')),
        'is_deprecated': True,
    }

# --- PG-prefix hardware (PG-6ZBRKT BOLT, PG-BRKT-SS BOLT) --------
PAT_PG_HARDWARE = re.compile(
    r'^PG-(?P<rest>[\dA-Z\s\-]+)$'
)

def _decode_pg_hardware(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pg_hardware',
        'family': 'PG',
        'family_meaning': 'Pipe Guard hardware',
        'pg_rest': m.group('rest'),
    }

# --- PGH polished pipe grab handle (PGH5-24S) ----------------------
PAT_PGH = re.compile(
    r'^PGH(?P<diameter>\d)-(?P<length>\d{2})(?P<finish>[ACPS])$'
)

def _decode_pgh(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'pgh_handle',
        'family': 'PGH',
        'family_meaning': 'Polished pipe grab handle',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'finish': m.group('finish'),
    }

# --- RO with PBK/SBK suffix (RO-4PBK, RO-5P-10EX) -----------------
PAT_RO_BULK = re.compile(
    r'^RO-(?P<diameter>\d+(?:\.\d+)?)(?P<finish>P|SS)(?P<modifier>BK|-\d+EX)?$'
)

def _decode_ro_bulk(m: re.Match) -> dict[str, Any]:
    d = m.group('diameter')
    if len(d) == 3 and '.' not in d:
        diameter = float(d[0]) + float(d[1:]) / 100
    elif len(d) == 4 and '.' not in d:
        diameter = float(d[0]) + float(d[1:]) / 1000
    else:
        diameter = float(d)
    return {
        'pattern': 'ro_clamp',
        'family': 'RO',
        'family_meaning': 'Round saddle clamp (bulk)',
        'diameter': diameter,
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
    }

# --- SP-WFFn surplus westfalia flex (SP-WFF4) ---------------------
PAT_SP_WFF = re.compile(
    r'^SP-WFF(?P<diameter>\d+)$'
)

def _decode_sp_wff(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'wff_flex',
        'family': 'WFF',
        'family_meaning': 'Surplus Westfalia flex',
        'diameter': float(m.group('diameter')),
        'is_surplus': True,
    }

# --- SP7-32SB-5RAW raw work-in-progress ---------------------------
PAT_SP_RAW = re.compile(
    r'^SP(?P<diameter>\d+(?:\.\d+)?)-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX)(?P<finish>[ACPS])?-(?P<outlet>\d+)?(?P<modifier>RAW|CC|CL)$'
)

def _decode_sp_raw(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 's_reducer',
        'family': 'P',
        'family_meaning': 'Pipe (mitre stack, raw/work-in-progress)',
        'is_reducer': True,
        'diameter': float(m.group('diameter')),
        'inlet_diameter': float(m.group('diameter')),
        'outlet_diameter': float(m.group('outlet')) if m.group('outlet') else 5.0,
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
        'is_raw': m.group('modifier') == 'RAW',
    }

# --- TL tapered with double-CHR (TL690-3127SC) -------------------
PAT_TL_TAPERED_BROAD = re.compile(
    r'^TL(?P<diameter>\d)(?P<angle>\d{2,3})-(?P<legs>\d{4})'
    r'(?P<body>SB|EX|S)?(?P<finish>[ACPS])(?:-(?P<modifier>[A-Z\d]+))?$'
)

def _decode_tl_tapered_broad(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'tl_tapered',
        'family': 'TL',
        'family_meaning': 'Tapered L-elbow',
        'diameter': float(m.group('diameter')),
        'angle': int(m.group('angle')),
        'legs': m.group('legs'),
        'finish': m.group('finish'),
    }

# --- Y-prefix Y-pipe with NPA suffix (Y-350NPA-S4, Y-500NPA-OD) ---
PAT_Y_NPA = re.compile(
    r'^Y-(?P<diameter>\d+)(?P<finish>NPA|NP)(?:-(?P<modifier>[A-Z\d]+))?$'
)

def _decode_y_npa(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'y_pipe',
        'family': 'Y',
        'family_meaning': 'Y-pipe (NPA variant)',
        'diameter': float(m.group('diameter')),
        'finish': m.group('finish'),
        'modifier': m.group('modifier'),
    }

# --- Slash + suffix (10115491/CHF6150, 10132341/CHF-6697) ---------
PAT_SLASH_LEGACY = re.compile(
    r'^(?P<seq1>\d{6,8})/(?P<seq2>[A-Z]+\-?\d+)$'
)

def _decode_slash_legacy(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'slash_legacy',
        'family': 'LEGACY',
        'family_meaning': 'Legacy SKU with slash-pair',
        'seq1': m.group('seq1'),
        'seq2': m.group('seq2'),
    }

# --- 109-prefix Forge Design tip (109-TIP1M/L, 109-TIP1M/L-CP) -----
PAT_FORGE_DESIGN_TIP = re.compile(
    r'^109-(?P<rest>TIP[\dA-Z/\-]+)$'
)

def _decode_forge_design_tip(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'forge_design_tip',
        'family': '109',
        'family_meaning': 'Forge Design tip',
        'rest': m.group('rest'),
    }

# --- 13160 heat wrap clamp / tool ---------------------------------
PAT_HW_CLAMP = re.compile(
    r'^13160[A-Z]?(?:-[A-Z]+)?$'
)

def _decode_hw_clamp(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hw_clamp',
        'family': 'HW',
        'family_meaning': 'Heat wrap clamp / tool',
    }

# --- 134228-ASSY assembly numbers ---------------------------------
PAT_ASSY = re.compile(
    r'^(?P<seq>\d{4,7})-ASSY$'
)

def _decode_assy(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'assembly',
        'family': 'ASSY',
        'family_meaning': 'Assembly numeric SKU',
        'assy_seq': m.group('seq'),
    }

# --- 14WT600S, 14GA. STEEL PLATE (raw material 14gauge) ----------
PAT_14GA_MATERIAL = re.compile(
    r'^14[A-Z]+\.?\s.*',
    re.IGNORECASE
)

def _decode_14ga_material(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'material',
        'family': '14GA',
        'family_meaning': 'Raw 14-gauge material',
        'is_raw_material': True,
    }

# --- 427964-1B numeric+letter+sub (legacy) -----------------------
PAT_LEGACY_DASHED = re.compile(
    r'^\d{6}-\d+[A-Z]+$'
)

def _decode_legacy_dashed(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'legacy_dashed',
        'family': 'LEGACY',
        'family_meaning': 'Legacy dashed numeric SKU',
        'requires_human_review': True,
    }

# --- 5U-B S, 5WSB numeric-prefix saddle ---------------------------
PAT_NUM_SADDLE_SHORT = re.compile(
    r'^(?P<diameter>\d+(?:\.\d+)?)(?P<rest>U-B[\s\-A-Z]+|WSB[A-Z]?|FUZ)$'
)

def _decode_num_saddle_short(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'num_saddle',
        'family': 'SADDLE',
        'family_meaning': 'U-bolt saddle (numeric-prefix)',
        'diameter': float(m.group('diameter')),
        'rest': m.group('rest'),
    }

# --- 6170 / 8037 / 8077 4-digit + sub legacy SKUs ----------------
PAT_4DIGIT_LEGACY = re.compile(
    r'^\d{4}-?\d*[A-Z]?(?:\s+REV[\s\.A-Z]*)?$'
)

def _decode_4digit_legacy(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': '4digit_legacy',
        'family': 'LEGACY',
        'family_meaning': 'Legacy 4-digit + sub SKU',
        'requires_human_review': True,
    }

# --- AIRFRSHNR merch ----------------------------------------------
PAT_AIRFRSHNR = re.compile(
    r'^AIRFRSHNR-.*'
)

def _decode_airfrshnr(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'merch',
        'family': 'AIRFRSHNR',
        'family_meaning': 'Air freshener merch',
        'is_merch': True,
    }

# --- BD/BM/CDR/CK/DD/DET/DIL/DPF/DPLX/DS/ED/EEM/FPH/GIK/GRC/GRCHF/GRF/HC/HW/IHCC/IHL/JG/KW/LDF/LH/MA/OMH/PB/PBF/POWERFLOW/PRKF/RB/RI/SAS/SPRING/SPT/SSF/SWR/TBA/TPF/TT/VL/VS/WED/WFC/WSA/WSP/WSWCK/ZA/ZR ---
# Catch-all: 2-3-letter family + dash + content with various forms
PAT_GENERIC_FAMILY = re.compile(
    r'^(?P<family>[A-Z]{2,8})-(?P<rest>[\dA-Z\.][\dA-Z\.\s\-/]*)$'
)

def _decode_generic_family(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': f'{family.lower()}_generic',
        'family': family,
        'family_meaning': f'{family} family (generic-form match)',
        'rest': m.group('rest'),
    }

# --- IHCC378, IHCC378-36 IHC chrome stack ------------------------
PAT_IHCC = re.compile(
    r'^IHCC(?P<seq>\d+)(?:-(?P<sub>\d+))?$'
)

def _decode_ihcc(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'ihcc_stack',
        'family': 'IHCC',
        'family_meaning': 'IHC custom stack chrome',
        'oem': 'IH',
        'ihcc_seq': m.group('seq'),
        'ihcc_sub': m.group('sub'),
    }

# --- DET stack with custom dimensions (DET4-11.328SBS3) ----------
PAT_DET_STACK = re.compile(
    r'^DET(?P<diameter>\d+(?:\.\d+)?)-(?P<length>\d+(?:\.\d+)?)(?P<body>SB|EX)(?P<finish>S3|S4|SS|[ACPS])$'
)

def _decode_det_stack(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'det_stack',
        'family': 'DET',
        'family_meaning': 'Custom DET stack',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
    }

# --- LH long-horn stack (LH5-1218LC) -----------------------------
PAT_LH_STACK = re.compile(
    r'^LH(?P<diameter>\d)-(?P<rest>\d+[A-Z]+)$'
)

def _decode_lh_stack(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'lh_longhorn',
        'family': 'LH',
        'family_meaning': 'Longhorn stack',
        'diameter': float(m.group('diameter')),
        'lh_rest': m.group('rest'),
    }

# --- WSA/WSP/WSWCK Western Star variants -------------------------
PAT_WS_VARIANT = re.compile(
    r'^(?P<family>WSA|WSP|WSWCK)(?P<diameter>\d+(?:\.\d+)?)-(?P<length>\d+(?:\.\d+)?)'
    r'(?P<body>SB|EX)(?P<finish>[ACPS])-(?P<outlet>\d+)$'
)

def _decode_ws_variant(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'western_star_variant',
        'family': family,
        'family_meaning': f'Western Star {family[2:]} variant',
        'oem': 'WS',
        'oem_meaning': 'Western Star',
        'diameter': float(m.group('diameter')),
        'length': float(m.group('length')),
        'body': m.group('body'),
        'finish': m.group('finish'),
        'outlet_diameter': float(m.group('outlet')),
        'is_reducer': True,
    }

# --- ZA/ZR Z-assembly / Z-reducer ---------------------------------
PAT_ZA_ZR = re.compile(
    r'^(?P<family>ZA|ZR)(?P<seq>\d{4,5})-(?P<sub>\d+)$'
)

def _decode_za_zr(m: re.Match) -> dict[str, Any]:
    family = m.group('family')
    return {
        'pattern': 'z_series',
        'family': family,
        'family_meaning': 'Z-assembly' if family == 'ZA' else 'Z-reducer',
        'z_seq': m.group('seq'),
        'sub': m.group('sub'),
    }

# --- KW spacer (KW-A1BMH-SS SPACER) -------------------------------
PAT_KW_SPACER = re.compile(
    r'^KW-(?P<rest>[A-Z\d]+(?:-SS)?(?:\s+SPACER)?)$'
)

def _decode_kw_spacer(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'kw_spacer',
        'family': 'KW',
        'family_meaning': 'Kenworth spacer / hardware',
        'oem': 'KW',
        'kw_rest': m.group('rest'),
    }

# --- POWERFLOW catalog freetext ----------------------------------
PAT_POWERFLOW_CAT = re.compile(
    r'^POWERFLOW\s.*',
    re.IGNORECASE
)

def _decode_powerflow_cat(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'freetext_or_admin',
        'family': 'POWERFLOW',
        'family_meaning': 'Powerflow catalog freetext',
        'disregard': True,
    }

# --- "MUFFLER TIP", "NUT FOR ..." descriptive parts --------------
PAT_DESCRIPTIVE_PART = re.compile(
    r'^(MUFFLER|NUT|SPRING|GRAB)\s.*',
    re.IGNORECASE
)

def _decode_descriptive_part(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'descriptive_legacy',
        'family': 'DESC',
        'family_meaning': 'Descriptive legacy part name',
        'requires_human_review': True,
    }

# --- McMaster-Carr style hardware (4936K175, 8491A654, 91201A011) ---
# These are pass-through hardware items: digit+letter+digit pattern
PAT_MCMASTER_HARDWARE = re.compile(
    r'^\d{4,5}[A-Z]\d{2,4}$'
)

def _decode_mcmaster_hardware(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'hardware_passthrough',
        'family': 'HARDWARE',
        'family_meaning': 'Hardware passthrough (McMaster-Carr style)',
        'is_hardware': True,
        'disregard': True,
    }

# --- Numeric-prefix component (50000+ range, 5-7 digit + sub) -----
PAT_NUMERIC_LONG_LEGACY = re.compile(
    r'^\d{4,7}(?:[A-Z]?-?\w*)*$'
)

def _decode_numeric_long_legacy(m: re.Match) -> dict[str, Any]:
    return {
        'pattern': 'numeric_long_legacy',
        'family': 'LEGACY',
        'family_meaning': 'Long numeric legacy SKU',
        'requires_human_review': True,
    }

# --- BD/BM/PB/PBF/RI/PRKF/SAS/SSF/TBA/TT/VL/VS/WED 2-3 char + content ---
# These all match PAT_GENERIC_FAMILY which is registered last as catch-all


# ============================================================================
# Pattern dispatch (order matters)
# ============================================================================

PATTERNS = [
    # Recursive / prefix-stripping (must run first)
    ('2nd_prefix', PAT_2ND, _decode_2nd),
    ('2nd_elbow', PAT_2L_ELBOW, _decode_2l_elbow),
    ('sw_riverton', PAT_SW_RIVERTON, _decode_sw_riverton),

    # Highly specific customer/program codes (before generic patterns)
    ('explicit_disregard', None, None),  # placeholder; checked separately
    ('hardware_passthrough', PAT_HARDWARE_PASSTHROUGH, _decode_hardware_passthrough),
    ('customer_mirror_304', PAT_CUSTOMER_MIRROR_304, _decode_customer_mirror_304),
    ('customer_mirror_e', PAT_CUSTOMER_MIRROR_E, _decode_customer_mirror_e),
    ('brightwater_component', PAT_BRIGHTWATER_COMPONENT, _decode_brightwater_component),
    ('ch_customer', PAT_CH_CUSTOMER, _decode_ch_customer),
    ('surplus_reducer', PAT_SURPLUS_REDUCER, _decode_surplus_reducer),
    ('powerflow_flex', PAT_POWERFLOW_FLEX, _decode_powerflow_flex),
    ('year_range', PAT_YEAR_RANGE, _decode_year_range),
    ('expander_tool', PAT_EXPANDER, _decode_expander),

    # Branded merch / disregard families (before generic decoding)
    ('asd_display', PAT_ASD, _decode_asd),
    ('pwfl_merch', PAT_PWFL, _decode_pwfl),
    ('gr_merch', PAT_GR_MERCH, _decode_gr_merch),

    # Customer codes (50 before custom_review, both specific)
    ('custom_50', PAT_CUSTOM_50, _decode_custom_50),
    ('custom_review', PAT_CUSTOM_REVIEW, _decode_custom_review),

    # Marmon (length-suffix variant before bare suffix)
    ('marmon_l_length', PAT_MARMON_L_LENGTH, _decode_marmon_l_length),
    ('marmon', PAT_MARMON, _decode_marmon),

    # Hangers, kits, brackets
    ('hanger', PAT_HANGER, _decode_hanger),
    ('prk_kit', PAT_PRK, _decode_prk),
    ('complete_kit', PAT_COMPLETE_KIT, _decode_complete_kit),
    ('mb_bracket', PAT_MB, _decode_mb),
    ('smb_bracket', PAT_SMB, _decode_smb),

    # Engine programs
    ('ford_engine', PAT_FORD_ENGINE, _decode_ford_engine),

    # Gasket (G-{OEM}{seq}) — must come before any plain G-prefix pattern
    ('gasket', PAT_GASKET, _decode_gasket),

    # Bulk pack and clamps
    ('bulk_2k', PAT_2K, _decode_2k),
    ('ez_clamp', PAT_EZ, _decode_ez),
    ('griez_clamp', PAT_GRIEZ, _decode_griez),

    # Specialized product families
    ('cm_muffler', PAT_CM, _decode_cm),
    ('dss_flex', PAT_DSS, _decode_dss),

    # HP customer drawings (0199-LL2-XXX) — must be before drawing_number
    ('hp_drawing', PAT_HP_DRAWING, _decode_hp_drawing),

    # Parametric forms (numeric-first variant before standard)
    ('parametric_nf', PAT_PARAMETRIC_NF, _decode_parametric_nf),
    ('reducer', PAT_REDUCER, _decode_reducer),
    # SL elbow reducer must come BEFORE plain elbow (S-prefix)
    ('sl_elbow_reducer', PAT_SL_ELBOW_REDUCER, _decode_sl_elbow_reducer),
    # Decimal-diameter elbow (L1.5..., L1.75...) before standard elbow
    ('elbow', PAT_ELBOW_DECIMAL_D, _decode_elbow_decimal_d),
    ('elbow', PAT_ELBOW, _decode_elbow),
    ('perf_diesel', PAT_PERF_DIESEL, _decode_perf_diesel),
    # S-prefix stack reducer must come BEFORE plain parametric
    # (else SS5-36SBC would parse as family=S diameter=5)
    ('s_reducer', PAT_S_REDUCER, _decode_s_reducer),
    ('parametric', PAT_PARAMETRIC, _decode_parametric),

    # SB school bus parts (839 SKUs — major family)
    ('sb_school_bus', PAT_SB_SCHOOL_BUS, _decode_sb_school_bus),

    # B-prefix stack blanks (B-0954TH style)
    ('b_blank', PAT_B_BLANK, _decode_b_blank),

    # Z-series modern pipes/mufflers (broad; 1100+ SKUs)
    ('z_series', PAT_Z_SERIES, _decode_z_series),

    # OEM-mirror SKUs (KW-, FL-, PB-, etc.) — many hundreds
    ('oem_mirror', PAT_OEM_MIRROR, _decode_oem_mirror),

    # Specialty clamp/accessory families
    ('s_reducer', PAT_SK_CERAMIC, _decode_sk_ceramic),  # SK ceramic-lined; before s_reducer
    ('s_reducer', PAT_S_REDUCER_MODIFIERS, _decode_s_reducer_modifiers),
    ('s_reducer', PAT_S_REDUCER_EMBEDDED_OUTLET, _decode_s_reducer_embedded_outlet),
    ('s_reducer', PAT_SS_REDUCER_NCL, _decode_ss_reducer_ncl),
    ('s_reducer', PAT_SWCK_L_OUTLET, _decode_swck_l_outlet),
    ('pf_clamp', PAT_PF_EXTENDED, _decode_pf_extended),
    ('pf_clamp', PAT_PF_CLAMP, _decode_pf_clamp),
    ('vb_clamp', PAT_VB_EXTENDED, _decode_vb_extended),
    ('vb_clamp', PAT_VB_CLAMP, _decode_vb_clamp),
    ('fb_clamp', PAT_FB_CLAMP, _decode_fb_clamp),
    ('rb_clamp', PAT_RB_CLAMP, _decode_rb_clamp),
    ('as_clamp', PAT_AS_CLAMP, _decode_as_clamp),
    ('ro_clamp', PAT_RO_CLAMP, _decode_ro_clamp),
    ('at_clamp', PAT_AT_CLAMP, _decode_at_clamp),
    ('hd_sku', PAT_HD_EXTENDED, _decode_hd_extended),
    ('hd_sku', PAT_HD_SKU, _decode_hd_sku),
    ('st_sku', PAT_ST_EXTENDED, _decode_st_extended),
    ('st_sku', PAT_ST_SKU, _decode_st_sku),

    # Customer-engine kits
    ('ps_kit', PAT_PS_KIT, _decode_ps_kit),
    ('ps_kit', PAT_PS_KIT_BROAD, _decode_ps_kit_broad),
    ('dc_kit', PAT_DC_KIT, _decode_dc_kit),
    ('dc_kit', PAT_DC_KIT_BROAD, _decode_dc_kit_broad),
    ('dc_kit', PAT_DC_600, _decode_dc_600),
    ('dc_kit', PAT_DC_SIMPLE, _decode_dc_simple),
    ('dpu_kit', PAT_DPU_INLET, _decode_dpu_inlet),
    ('dpu_kit', PAT_DPU_KIT, _decode_dpu_kit),
    ('spu_kit', PAT_SPU_KIT, _decode_spu_kit),
    ('dim_muffler', PAT_DIM, _decode_dim),

    # Tapered Kenworth + tapered SL
    ('tkw_tapered', PAT_TKW, _decode_tkw),
    ('tsl_tapered_elbow', PAT_TSL, _decode_tsl),

    # M Muffler family
    ('m_muffler', PAT_M_FULL_LONG, _decode_m_full_long),
    ('m_muffler', PAT_M_LONGFORM, _decode_m_longform),
    ('m_muffler', PAT_M_MUFFLER, _decode_m_muffler),

    # IM Intermediate Muffler
    ('im_muffler', PAT_IM_MUFFLER, _decode_im_muffler),

    # EBK Bellows kit
    ('ebk_bellows', PAT_EBK_BELLOWS, _decode_ebk_bellows),

    # Z-series variants (most specific first)
    ('z_dash_finish', PAT_Z_DASH_FINISH, _decode_z_dash_finish),
    ('z_with_finish', PAT_Z_WITH_FINISH, _decode_z_with_finish),
    ('z_series', PAT_ZP_CL, _decode_zp_cl),
    ('z_series', PAT_ZP_LETTER_SUFFIX, _decode_zp_letter_suffix),
    ('z_series', PAT_Z_FAMILY_SHORT, _decode_z_family_short),
    ('z_short', PAT_Z_SHORT, _decode_z_short),

    # T turbo / U-bolt
    ('t_sku', PAT_T_DIAM, _decode_t_diam),
    ('t_sku', PAT_T_EXTENDED, _decode_t_extended),
    ('t_sku', PAT_T_SKU, _decode_t_sku),
    ('t_sku', PAT_T_LEGACY_LONG, _decode_t_legacy_long),
    ('u_bolt', PAT_U_BOLT, _decode_u_bolt),

    # K-elbow special (legacy form)
    ('k_elbow_special', PAT_K_ELBOW_SPECIAL, _decode_k_elbow_special),

    # Connector / Coupler / Y-pipe / Muffler families
    ('cp_coupler', PAT_CP_COUPLER, _decode_cp_coupler),
    ('cn_connector', PAT_CN_CONNECTOR, _decode_cn_connector),
    ('rs_connector', PAT_RS_CONNECTOR, _decode_rs_connector),
    ('y_pipe', PAT_Y_VARIANT, _decode_y_variant),
    ('y_pipe', PAT_Y_PIPE, _decode_y_pipe),
    ('qpm_muffler', PAT_QPM, _decode_qpm),

    # Dump-stack family-specific decoders
    ('ed_dump', PAT_ED_DUMP, _decode_ed_dump),
    ('dts_dump', PAT_DTS_DUMP, _decode_dts_dump),
    ('jds_dump', PAT_JDS_DUMP, _decode_jds_dump),
    ('fec_dump', PAT_FEC_DUMP, _decode_fec_dump),
    ('ob_dump', PAT_OB_DUMP, _decode_ob_dump),

    # Box-T / Heat divert
    ('bt_box', PAT_BT_BOX, _decode_bt_box),
    ('hb_diverter', PAT_HB, _decode_hb),

    # Bellows
    ('eb_bellows', PAT_EB_BELLOWS, _decode_eb_bellows),

    # Adapter / patch / flange
    ('ad_flange', PAT_AD_FLANGE, _decode_ad_flange),
    ('patch', PAT_PATCH, _decode_patch),

    # CSP cab-side pipe
    ('csp_pipe', PAT_CSP_PIPE, _decode_csp_pipe),

    # OEM-mirror with diameter prefix (KW6-10742LA)
    ('oem_mirror_with_diam', PAT_KW_DIAM, _decode_kw_diam),

    # PB longform Pete OEM
    ('pb_longform', PAT_PB_LONGFORM, _decode_pb_longform),

    # F-Ford OEM-mirror (F1HZ-...) and bracket (F41, F4MP)
    ('f_ford', PAT_F_FORD, _decode_f_ford),
    ('f_ford_bracket', PAT_F_FORD_BRACKET, _decode_f_ford_bracket),

    # FNT pipe
    ('fnt_pipe', PAT_FNT, _decode_fnt),

    # FTE Ford truck exhaust
    ('fte_kit', PAT_FTE, _decode_fte),

    # SD Scheid
    ('sd_scheid', PAT_SD_KIT, _decode_sd_kit),

    # MI Marson, MY Muffler-Y, PAC pivot
    ('mi_marson', PAT_MI_MARSON, _decode_mi_marson),
    ('my_muffler', PAT_MY_MUFFLER, _decode_my_muffler),
    ('pac_bushing', PAT_PAC_PIVOT, _decode_pac_pivot),

    # Proprietary customers
    ('pdi_proprietary', PAT_PDI_PROPRIETARY, _decode_pdi_proprietary),
    ('norco_proprietary', PAT_NORCO_STACK, _decode_norco_stack),
    ('es_proprietary', PAT_ES_SHUSTER, _decode_es_shuster),
    ('rd_forge_design', PAT_RD_RAW, _decode_rd_raw),

    # D-prefix bracket (Dodge) and legacy
    ('d_bracket', PAT_D_BRACKET, _decode_d_bracket),
    ('d_legacy', PAT_D_LEGACY, _decode_d_legacy),

    # J-prefix supplier
    ('j_supplier', PAT_J_SUPPLIER, _decode_j_supplier),

    # P-prefix Pipe family (long-form legacy SKU)
    ('p_pipe_legacy', PAT_P_SUPPLIER, _decode_p_supplier),

    # SP standalone spring plate
    ('sp_plate', PAT_SP_PLATE, _decode_sp_plate),

    # AC Aerocab bracket
    ('ac_bracket', PAT_AC_BRACKET, _decode_ac_bracket),

    # Westfalia
    ('wff_flex', PAT_WFF, _decode_wff),
    ('wfc_clamp', PAT_WFC, _decode_wfc),

    # Hose families
    ('cac_hose', PAT_CAC_HOSE, _decode_cac_hose),
    ('hh_hump', PAT_HH_HUMP, _decode_hh_hump),
    ('rre_rubber_elbow', PAT_RRE, _decode_rre),
    ('re_elbow', PAT_RE_ELBOW, _decode_re_elbow),

    # Hangers
    ('rmh_hanger', PAT_RMH, _decode_rmh),
    ('h_hanger', PAT_H_HANGER, _decode_h_hanger),
    ('ph_hanger', PAT_PH_HANGER, _decode_ph_hanger),

    # ARG/ESM aerocab muffler
    ('arg_esm_muffler', PAT_ARG_ESM, _decode_arg_esm),

    # GH grab handle
    ('gh_handle', PAT_GH_HANDLE, _decode_gh_handle),

    # Tube/flange families
    ('tr_tube', PAT_TR_TUBE, _decode_tr_tube),
    ('trf_flange', PAT_TRF, _decode_trf),

    # FK flex kit
    ('fk_kit', PAT_FK_KIT, _decode_fk_kit),

    # GF deprecated (points to G15)
    ('gf_deprecated', PAT_GF_DEPRECATED, _decode_gf_deprecated),

    # GRIPF GR pre-form clamp
    ('gripf_clamp', PAT_GRIPF, _decode_gripf),

    # PT Protube
    ('pt_protube', PAT_PT_PROTUBE, _decode_pt_protube),

    # RC Rain cap
    ('rc_raincap', PAT_RC_RAINCAP, _decode_rc_raincap),

    # RF Relaxed flex
    ('rf_flex', PAT_RF_FLEX, _decode_rf_flex),

    # VK insert
    ('vk_insert', PAT_VK_INSERT, _decode_vk_insert),

    # HDT heavy-duty truck
    ('hdt_y_pipe', PAT_HDT, _decode_hdt),

    # DPFVB DPF V-Band kit
    ('dpfvb_kit', PAT_DPFVB, _decode_dpfvb),

    # E-component
    ('e_component', PAT_E_COMPONENT, _decode_e_component),

    # QP Quiet Performance
    ('qp_product', PAT_QP_PRODUCT, _decode_qp_product),

    # B legacy bellow
    ('b_bellow_legacy', PAT_B_BELLOW_LEGACY, _decode_b_bellow_legacy),

    # C-prefix supplier
    ('c_supplier', PAT_C_SUPPLIER, _decode_c_supplier),

    # Legacy short-form elbow (L3-10SA)
    ('elbow_old', PAT_L_ELBOW_OLD, _decode_l_elbow_old),

    # Compressed elbow with 3-digit diameter (L190-, L315-, L345-)
    ('elbow', PAT_ELBOW_3DIGIT, _decode_elbow_3digit),

    # Permissive elbow catch-all (final L-prefix fallback)
    ('elbow', PAT_ELBOW_PERMISSIVE, _decode_elbow_permissive),

    # 2NDxxx broad (cosmetic-second with unrecognized family)
    ('2nd_unknown', PAT_2ND_BROAD, _decode_2nd_broad),

    # FB clamp extended
    ('fb_clamp', PAT_FB_EXTENDED, _decode_fb_extended),

    # AS clamp extended
    ('as_clamp', PAT_AS_EXTENDED, _decode_as_extended),

    # K bulk-pack
    ('parametric', PAT_K_BULK, _decode_k_bulk),

    # Parametric with -S{N} dash-finish (K5-36SP-S3, SP6-60EX-S3)
    ('parametric', PAT_PARAMETRIC_DASH_FINISH, _decode_parametric_dash_finish),

    # Y-pipe extended (Y-400 PLATES, Y-400S4)
    ('y_pipe', PAT_Y_EXTENDED, _decode_y_extended),

    # OS Offset Stack mount bracket
    ('os_bracket', PAT_OS_BRACKET, _decode_os_bracket),

    # SF flex with WC suffix
    ('sf_flex', PAT_SF_WC, _decode_sf_wc),

    # T spool pipe (T6SP-...)
    ('t_spool_pipe', PAT_T_SPOOL, _decode_t_spool),

    # 50DD machine drawing
    ('dd_machine', PAT_DD_MACHINE, _decode_dd_machine),

    # 152xx and 09-xxxxxx legacy
    ('15_legacy', PAT_15_LEGACY, _decode_15_legacy),
    ('09_legacy', PAT_09_LEGACY, _decode_09_legacy),

    # D-Dodge bracket extended
    ('d_dodge', PAT_D_DODGE, _decode_d_dodge),

    # F-Ford long form
    ('f_ford', PAT_F_FORD_LONG, _decode_f_ford_long),

    # RS extended
    ('rs_connector', PAT_RS_S4, _decode_rs_s4),

    # SB longform
    ('sb_school_bus', PAT_SB_LONGFORM, _decode_sb_longform),

    # FUZ flat U-bolt zinc
    ('fuz_clamp', PAT_FUZ, _decode_fuz),

    # US Unistrap clamp
    ('us_clamp', PAT_US_CLAMP, _decode_us_clamp),

    # Diameter-prefix kits (8NG, 8PK, 7CK, 6KWK, 6SP)
    ('diameter_prefix_kit', PAT_DIAMETER_KIT, _decode_diameter_kit),

    # 4-digit numeric (9390, 9391)
    ('numeric_4digit', PAT_4DIGIT_NUMERIC, _decode_4digit_numeric),

    # Numeric-prefix descriptive product (e.g., 5" SCRATCH & DENT)
    ('descriptive_legacy', PAT_NUMERIC_PRODUCT_DESC, _decode_numeric_product_desc),

    # Last resort: legacy / unknown structural patterns flagged for review
    ('legacy_with_space', PAT_LEGACY_WITH_SPACE, _decode_legacy_with_space),

    # Reducer alternate forms
    ('reducer_alt', PAT_R_REDUCER_PERMISSIVE, _decode_r_reducer_permissive),
    ('reducer_alt', PAT_R_REDUCER_ALT, _decode_r_reducer_alt),

    # S-prefix tube (extended catch + basic)
    ('s_tube_extended', PAT_S_TUBE_EXTENDED, _decode_s_tube_extended),
    ('s_tube', PAT_S_TUBE, _decode_s_tube),

    # Compressed-form elbow (more permissive than half-diam)
    ('elbow', PAT_ELBOW_HALF_DIAM, _decode_elbow_half_diam),
    ('elbow', PAT_ELBOW_COMPRESSED, _decode_elbow_compressed),

    # Flex hose families
    ('sf_flex', PAT_SF_FLEX_EXT, _decode_sf_flex_ext),
    ('sf_flex', PAT_SF_FLEX, _decode_sf_flex),
    ('g_flex', PAT_G15_EXTENDED, _decode_g15_extended),
    ('g_flex', PAT_G_FLEX, _decode_g_flex),
    ('g_extended', PAT_G_EXTENDED, _decode_g_extended),

    # US41 stack
    ('us_stack', PAT_US_STACK, _decode_us_stack),

    # === Final coverage batch ===

    # Admin / service / merch (highest priority freetext)
    ('freetext_or_admin', PAT_MISC_ITEM, _decode_misc_item),
    ('service_line', PAT_PLATING_SVC, _decode_plating_svc),
    ('merch', PAT_GRAB_MERCH, _decode_grab_merch),

    # M body / generic
    ('m_muffler', PAT_M_BODY, _decode_m_body),

    # SP modifiers (CC/RAW/BB/CL)
    ('s_reducer', PAT_SP_MODIFIERS, _decode_sp_modifiers),

    # AT PLATER work-in-progress (-P/-NP suffix)
    ('at_plater', PAT_AT_PLATER, _decode_at_plater),

    # ZP/ZM with -PN sub-component
    ('z_series', PAT_ZP_PN_SUFFIX, _decode_zp_pn_suffix),
    ('z_series', PAT_ZP_GAUGE, _decode_zp_gauge),

    # SL with embedded outlet
    ('sl_elbow_reducer', PAT_SL_EMBEDDED_OUTLET, _decode_sl_embedded_outlet),

    # R-series flares and reducers
    ('r_flare', PAT_R_FLARE, _decode_r_flare),
    ('reducer_alt', PAT_R_REDUCER_LETTER, _decode_r_reducer_letter),

    # PB/FL OEM-mirror longform
    ('pb_longform', PAT_PB_REV, _decode_pb_rev),
    ('fl_oem_mirror', PAT_FL_LONGFORM, _decode_fl_longform),

    # Y-pipe full
    ('y_pipe', PAT_Y_FULL, _decode_y_full),

    # Slash tube
    ('slash_tube', PAT_SLASH_TUBE, _decode_slash_tube),

    # ACFM, AC, AF flex
    ('acfm_bracket', PAT_ACFM, _decode_acfm),
    ('ac_bracket', PAT_AC_KW, _decode_ac_kw),
    ('af_flex', PAT_AF_FLEX, _decode_af_flex),

    # CBS, CG, ES, FS, GBS multi-component
    ('cbs_kit', PAT_CBS_KIT, _decode_cbs_kit),
    ('cg_bend', PAT_CG_BEND, _decode_cg_bend),
    ('es_proprietary', PAT_ES_EXTENDED, _decode_es_extended),
    ('fs_clamp', PAT_FS_CLAMP, _decode_fs_clamp),
    ('gbs_bellows', PAT_GBS_FLEX, _decode_gbs_flex),

    # HD/HF longform
    ('hd_sku', PAT_HD_LONGFORM, _decode_hd_longform),
    ('hf_muffler', PAT_HF_MUFFLER, _decode_hf_muffler),

    # IH OEM
    ('ih_oem_mirror', PAT_IH_LONGFORM, _decode_ih_longform),

    # HDT link components
    ('hdt_y_pipe', PAT_HDT_LINK, _decode_hdt_link),

    # OK kit
    ('ok_kit', PAT_OK_KIT, _decode_ok_kit),

    # P-prefix variants
    ('parametric', PAT_P_VARIANTS, _decode_p_variants),

    # PRK components
    ('prk_kit', PAT_PRK_COMP, _decode_prk_comp),

    # RHH rubber hump hose
    ('rhh_rubber', PAT_RHH, _decode_rhh),

    # SC, STC clamps
    ('sc_clamp', PAT_SC_CLAMP, _decode_sc_clamp),
    ('stc_clamp', PAT_STC_CLAMP, _decode_stc_clamp),

    # UB, WB
    ('ub_bracket', PAT_UB_BRACKET, _decode_ub_bracket),
    ('wb_wittke', PAT_WB_WITTKE, _decode_wb_wittke),

    # YC longform
    ('y_pipe', PAT_YC_LONGFORM, _decode_yc_longform),

    # Z power flex / ZL
    ('z_powerflex', PAT_Z_POWERFLEX, _decode_z_powerflex),
    ('z_series', PAT_ZL_ELBOW, _decode_zl_elbow),

    # Hood stack components (BK/BP/MR/HSK/PGH)
    ('hsk_component', PAT_HSK_COMP, _decode_hsk_comp),
    ('hsk_kit', PAT_HSK_KIT, _decode_hsk_kit),

    # BRT, CN/CS/CSP/DPFY/EEM/EKM/HS/IC/MMB/MPB
    ('brt_tip', PAT_BRT_TIP, _decode_brt_tip),
    ('cn_connector', PAT_CN_LONGFORM, _decode_cn_longform),
    ('cs_stack', PAT_CS_STACK, _decode_cs_stack),
    ('csp_pipe', PAT_CSP_EXTENDED, _decode_csp_extended),
    ('dpfy_y_pipe', PAT_DPFY, _decode_dpfy),
    ('eem_muffler', PAT_EEM, _decode_eem),
    ('ekm_muffler', PAT_EKM, _decode_ekm),
    ('hs_kit', PAT_HS_KIT, _decode_hs_kit),
    ('ic_coupler', PAT_IC_COUPLER, _decode_ic_coupler),
    ('mmb_clamp', PAT_MMB, _decode_mmb),
    ('mpb_bellow', PAT_MPB, _decode_mpb),

    # MS deprecated
    ('ms_deprecated', PAT_MS_DEPRECATED, _decode_ms_deprecated),

    # PACSWR, PRKY, PSC, PUB, RTP, RU, SV, TBE
    ('pacswr_reducer', PAT_PACSWR, _decode_pacswr),
    ('prky_y_pipe', PAT_PRKY, _decode_prky),
    ('psc_bracket', PAT_PSC, _decode_psc),
    ('pub_bracket', PAT_PUB, _decode_pub),
    ('rtp_oem', PAT_RTP, _decode_rtp),
    ('ru_ulrich', PAT_RU, _decode_ru),
    ('sv_service', PAT_SV, _decode_sv),
    ('tbe_bell', PAT_TBE, _decode_tbe),

    # TL/TPB/TR longform
    ('tl_tapered', PAT_TL_TAPERED, _decode_tl_tapered),
    ('tpb_tapered', PAT_TPB_TAPERED, _decode_tpb_tapered),
    ('tr_tube', PAT_TR_LONGFORM, _decode_tr_longform),

    # UM, YDB
    ('um_dump', PAT_UM_DUMP, _decode_um_dump),
    ('ydb_bracket', PAT_YDB, _decode_ydb),

    # APU, B-component
    ('apu_connector', PAT_APU, _decode_apu),
    ('b_component', PAT_B_COMPONENT, _decode_b_component),

    # Numeric saddle/U-bolt and F94
    ('num_saddle', PAT_NUM_USADDLE, _decode_num_usaddle),
    ('f_ford', PAT_F94, _decode_f94),

    # M generic (catch-all M-prefix; place near end of M chain)
    ('m_muffler', PAT_M_GENERIC, _decode_m_generic),

    # Generic guards (PG/MG/AHS/UHS) — last because of broad regex
    ('guard_variant', PAT_GUARD, _decode_guard),

    # === Long-tail final batch ===

    # Admin / hardware / merch (high-priority freetext)
    ('freetext_or_admin', PAT_POWERFLOW_CAT, _decode_powerflow_cat),
    ('descriptive_legacy', PAT_DESCRIPTIVE_PART, _decode_descriptive_part),
    ('material', PAT_14GA_MATERIAL, _decode_14ga_material),
    ('merch', PAT_AIRFRSHNR, _decode_airfrshnr),

    # SL with full outlet spec
    ('sl_elbow_reducer', PAT_SL_FULL_OUTLET, _decode_sl_full_outlet),

    # G18 / G24 flex
    ('g_flex', PAT_G18_FLEX, _decode_g18_flex),
    ('g_flex', PAT_G_FLEX_SPACE, _decode_g_flex_space),

    # EBK longform
    ('ebk_bellows', PAT_EBK_C, _decode_ebk_c),

    # M-prefix bolts (hardware)
    ('m_bolt', PAT_M_BOLT, _decode_m_bolt),

    # ZP descriptive
    ('z_series', PAT_ZP_DESC, _decode_zp_desc),

    # K SP-finish polished
    ('parametric', PAT_K_SP_S, _decode_k_sp_s),

    # R heavy-wall
    ('reducer_alt', PAT_R_HW, _decode_r_hw),

    # PF variants
    ('pf_clamp', PAT_PF_SSP, _decode_pf_ssp),
    ('pf_clamp', PAT_PF_BARE, _decode_pf_bare),

    # PG hardware
    ('pg_hardware', PAT_PG_HARDWARE, _decode_pg_hardware),

    # PGH polished
    ('pgh_handle', PAT_PGH, _decode_pgh),

    # RO bulk
    ('ro_clamp', PAT_RO_BULK, _decode_ro_bulk),

    # SP-WFF surplus / SP RAW
    ('wff_flex', PAT_SP_WFF, _decode_sp_wff),
    ('s_reducer', PAT_SP_RAW, _decode_sp_raw),

    # TL tapered broad
    ('tl_tapered', PAT_TL_TAPERED_BROAD, _decode_tl_tapered_broad),

    # Y NPA
    ('y_pipe', PAT_Y_NPA, _decode_y_npa),

    # Slash legacy
    ('slash_legacy', PAT_SLASH_LEGACY, _decode_slash_legacy),

    # 109 forge design tip
    ('forge_design_tip', PAT_FORGE_DESIGN_TIP, _decode_forge_design_tip),

    # Heat wrap clamp / tools / assembly
    ('hw_clamp', PAT_HW_CLAMP, _decode_hw_clamp),
    ('assembly', PAT_ASSY, _decode_assy),

    # 4-digit and dashed legacy
    ('legacy_dashed', PAT_LEGACY_DASHED, _decode_legacy_dashed),

    # Numeric saddle short
    ('num_saddle', PAT_NUM_SADDLE_SHORT, _decode_num_saddle_short),

    # IHCC
    ('ihcc_stack', PAT_IHCC, _decode_ihcc),

    # DET stack
    ('det_stack', PAT_DET_STACK, _decode_det_stack),

    # LH long-horn
    ('lh_longhorn', PAT_LH_STACK, _decode_lh_stack),

    # Western Star variants
    ('western_star_variant', PAT_WS_VARIANT, _decode_ws_variant),

    # Z-assembly / Z-reducer
    ('z_series', PAT_ZA_ZR, _decode_za_zr),

    # KW spacer
    ('kw_spacer', PAT_KW_SPACER, _decode_kw_spacer),

    # Generic family catch-all (must come after all specific 2-3 letter family decoders)
    ('generic_family', PAT_GENERIC_FAMILY, _decode_generic_family),

    # 4-digit numeric legacy (catches 8037-3161, 8077-3008, 6170-6C, etc.)
    ('4digit_legacy', PAT_4DIGIT_LEGACY, _decode_4digit_legacy),

    # McMaster-Carr style hardware (passthrough)
    ('hardware_passthrough', PAT_MCMASTER_HARDWARE, _decode_mcmaster_hardware),

    # Material specs and placeholders
    ('material', PAT_MATERIAL, _decode_material),
    ('drop_bin', PAT_DROP_BIN, _decode_drop_bin),
    ('raw_bend', PAT_RAW_BEND, _decode_raw_bend),
    ('misc_bend', PAT_MISC_BEND, _decode_misc_bend),
    ('box', PAT_BOX, _decode_box),

    # Last resort: legacy / unknown structural patterns flagged for review
    ('legacy_with_space', PAT_LEGACY_WITH_SPACE, _decode_legacy_with_space),
    ('legacy_segmented', PAT_NUMERIC_SEGMENT, _decode_numeric_segment),
    ('multi_hyphen_component', PAT_MULTI_HYPHEN_COMP, _decode_multi_hyphen_comp),
    ('legacy_7digit', PAT_KIMBLE_7, _decode_kimble_7),
    ('drawing_number', PAT_DRAWING_NUMBER, _decode_drawing_number),
    ('multi_segment_legacy', PAT_MULTI_SEGMENT_DASH, _decode_multi_segment_dash),
    ('numeric_long_legacy', PAT_NUMERIC_LONG_LEGACY, _decode_numeric_long_legacy),
    ('pure_numeric', PAT_PURE_NUMERIC, _decode_pure_numeric),
]


def _try_patterns(sku: str) -> dict[str, Any] | None:
    """Try each pattern in order. Return the first match's decoded dict.

    Three special non-regex checks run first:
      1. EXPLICIT_DISREGARD_REVIEW — hardcoded one-off SKUs
      2. FREETEXT_PREFIXES — non-product line items (PA-, RESTOCK-, etc.)
      3. NPI/test SKUs
    """
    # 1. Explicit disregard list
    if sku in EXPLICIT_DISREGARD_REVIEW:
        return {
            'pattern': 'explicit_disregard',
            'family': 'DISREGARD',
            'family_meaning': 'Explicit disregard (per SME)',
            'disregard': True,
            'requires_human_review': True,
            'disregard_reason': EXPLICIT_DISREGARD_REVIEW[sku],
        }

    # 2. Freetext / admin prefixes (non-product line items)
    sku_upper = sku.upper()
    for prefix in FREETEXT_PREFIXES:
        if sku_upper.startswith(prefix):
            return {
                'pattern': 'freetext_or_admin',
                'family': 'ADMIN',
                'family_meaning': 'Non-product line item',
                'disregard': True,
                'admin_prefix': prefix.strip(),
            }

    # 3. Regex pattern dispatch
    for name, regex, decoder in PATTERNS:
        if regex is None or decoder is None:
            continue  # placeholder entries
        m = regex.match(sku)
        if m:
            result = decoder(m)
            if result is not None:
                return result
    return None


# ============================================================================
# Public API
# ============================================================================

def parse(sku: str) -> dict[str, Any]:
    """Decode a a catalog SKU string into a structured dict.

    Always returns a dict. Unrecognized inputs get pattern='unstructured'.
    """
    if not sku or not isinstance(sku, str):
        return {'part_number': sku, 'pattern': 'empty'}

    sku = sku.strip().upper()
    if not sku:
        return {'part_number': sku, 'pattern': 'empty'}

    result = _try_patterns(sku)
    if result is None:
        return {'part_number': sku, 'pattern': 'unstructured'}

    result['part_number'] = sku
    return result
