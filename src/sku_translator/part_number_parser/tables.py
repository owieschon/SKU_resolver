"""Static lookup tables for the part-number grammar (family / finish / body /
OEM meanings and family sets). Pure data, no logic."""
from __future__ import annotations

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
