# Vocabulary Specification for SKU Translator

This document specifies every family-word mapping the agent should add to `FAMILY_WORD_ALIASES` in `normalizer.py`. Mappings are derived from a catalog audit of the families that lacked rep-phrase vocabulary.

Format: each entry is the dict key (the rep's phrase) followed by the value dict (with code, category, name).

## HIGH PRIORITY (17 highest-volume families)

### CP - Coupler

```python
'coupler':              {'code': 'CP',  'category': 'pipe',     'name': 'Coupler'},
'couplers':             {'code': 'CP',  'category': 'pipe',     'name': 'Coupler'},
```

### DPU - Dual Pipe Universal

```python
'dual pipe universal':  {'code': 'DPU', 'category': 'kit',      'name': 'Dual Pipe Universal kit'},
'universal dual pipe':  {'code': 'DPU', 'category': 'kit',      'name': 'Dual Pipe Universal kit'},
'dpu':                  {'code': 'DPU', 'category': 'kit',      'name': 'Dual Pipe Universal kit'},
'dpu kit':              {'code': 'DPU', 'category': 'kit',      'name': 'Dual Pipe Universal kit'},
```

### ED - End Dump

```python
'end dump':             {'code': 'ED',  'category': 'stack',    'name': 'End Dump stack'},
'end-dump':             {'code': 'ED',  'category': 'stack',    'name': 'End Dump stack'},
'end dump stack':       {'code': 'ED',  'category': 'stack',    'name': 'End Dump stack'},
```

### FB - Flat Bolt clamp

```python
'flat bolt':            {'code': 'FB',  'category': 'clamp',    'name': 'Flat Bolt clamp'},
'flat-bolt':            {'code': 'FB',  'category': 'clamp',    'name': 'Flat Bolt clamp'},
'flatbolt':             {'code': 'FB',  'category': 'clamp',    'name': 'Flat Bolt clamp'},
'flat bolt clamp':      {'code': 'FB',  'category': 'clamp',    'name': 'Flat Bolt clamp'},
```

### P - Donaldson cross-reference parts

```python
'donaldson':            {'code': 'P',   'category': 'oem_cross', 'name': 'Donaldson cross-reference part'},
'donaldson part':       {'code': 'P',   'category': 'oem_cross', 'name': 'Donaldson cross-reference part'},
'don part':             {'code': 'P',   'category': 'oem_cross', 'name': 'Donaldson cross-reference part'},
```

Note for agent: Do NOT add bare `'don'` because it collides too often with normal speech. Require either "donaldson" or "don part" with explicit qualifier.

Note for agent: Donaldson parts have opaque internal numbering (vendor-controlled). Resolution typically returns PENDING_DISAMBIGUATION because the constructor can't build new P-family SKUs from spec; it must search existing P-family catalog entries that match description keywords.

### PF - Preformed clamp

```python
'preformed':            {'code': 'PF',  'category': 'clamp',    'name': 'Preformed clamp'},
'pre-formed':           {'code': 'PF',  'category': 'clamp',    'name': 'Preformed clamp'},
'pre formed':           {'code': 'PF',  'category': 'clamp',    'name': 'Preformed clamp'},
'preform':              {'code': 'PF',  'category': 'clamp',    'name': 'Preformed clamp'},
'preformed clamp':      {'code': 'PF',  'category': 'clamp',    'name': 'Preformed clamp'},
```

### RB - Round Bolt clamp / Saddle clamp

```python
'round bolt':           {'code': 'RB',  'category': 'clamp',    'name': 'Round Bolt clamp'},
'round-bolt':           {'code': 'RB',  'category': 'clamp',    'name': 'Round Bolt clamp'},
'roundbolt':            {'code': 'RB',  'category': 'clamp',    'name': 'Round Bolt clamp'},
'saddle clamp':         {'code': 'RB',  'category': 'clamp',    'name': 'Saddle clamp (Round Bolt)'},
'single saddle':        {'code': 'RB',  'category': 'clamp',    'name': 'Single Saddle clamp'},
```

Note for agent: Sample SKUs show RB descriptions include both "ROUND BOLT" and "SADDLE CLAMP" / "SINGLE SADDLE." Reps may use either. Both phrasings map to RB.

### SB - School Bus parts

```python
'school bus':           {'code': 'SB',  'category': 'oem_truck', 'name': 'School Bus part'},
'schoolbus':            {'code': 'SB',  'category': 'oem_truck', 'name': 'School Bus part'},
'school bus pipe':      {'code': 'SB',  'category': 'oem_truck', 'name': 'School Bus pipe'},
'school bus stack':     {'code': 'SB',  'category': 'oem_truck', 'name': 'School Bus stack'},
```

**Critical disambiguation:** SB at the start of an Item Number means School Bus family. SB between digits (e.g., K5-24SBC) means Straight Bottom body code. The parser already handles position-based disambiguation correctly (the School Bus regex and SB body-code aliases in part_number_parser/_patterns.py). Do not modify the existing body-code handling.

### VB - V-Band clamp

```python
'v-band':               {'code': 'VB',  'category': 'clamp',    'name': 'V-Band clamp'},
'v band':               {'code': 'VB',  'category': 'clamp',    'name': 'V-Band clamp'},
'vband':                {'code': 'VB',  'category': 'clamp',    'name': 'V-Band clamp'},
'v-band clamp':         {'code': 'VB',  'category': 'clamp',    'name': 'V-Band clamp'},
```

### WFC - World's Finest Clamp

```python
"world's finest":       {'code': 'WFC', 'category': 'clamp',    'name': "World's Finest Clamp"},
'worlds finest':        {'code': 'WFC', 'category': 'clamp',    'name': "World's Finest Clamp"},
'wfc':                  {'code': 'WFC', 'category': 'clamp',    'name': "World's Finest Clamp"},
"world's finest clamp": {'code': 'WFC', 'category': 'clamp',    'name': "World's Finest Clamp"},
```

### Y - Y-pipe

```python
'y pipe':               {'code': 'Y',   'category': 'pipe',     'name': 'Y-pipe'},
'y-pipe':               {'code': 'Y',   'category': 'pipe',     'name': 'Y-pipe'},
'ypipe':                {'code': 'Y',   'category': 'pipe',     'name': 'Y-pipe'},
'wye':                  {'code': 'Y',   'category': 'pipe',     'name': 'Y-pipe'},
'wye pipe':             {'code': 'Y',   'category': 'pipe',     'name': 'Y-pipe'},
```

### OEM-prefix families (PB, FL, KW, IH) - no vocabulary needed

the SME confirmed reps reference these by explicit SKU. Existing OEM_ALIASES handles the OEM word for context (e.g., "peterbilt" for filtering), and the fuzzy matcher handles literal SKU lookup. No FAMILY_WORD_ALIASES entries needed for PB, FL, KW, or IH.

### SBR (Brute reducer), BH (Bullhorn), T (Turbo pipe) - already mapped

These are in the existing FAMILY_WORD_ALIASES. No changes.

## MEDIUM PRIORITY (high-impact subset)

### TR - Tube (cold rolled tubing)

```python
'tube':                 {'code': 'TR',  'category': 'pipe',     'name': 'Tube (cold rolled)'},
'cold rolled tube':     {'code': 'TR',  'category': 'pipe',     'name': 'Tube (cold rolled)'},
'cr tube':              {'code': 'TR',  'category': 'pipe',     'name': 'Tube (cold rolled)'},
```

Note for agent: TR descriptions explicitly say "COLD ROLLED TUBE" with gauge specs. The word "tube" alone may collide with general use, so also map the qualified forms.

### HB - Diverter box

```python
'diverter':             {'code': 'HB',  'category': 'kit',      'name': 'Diverter box'},
'diverter box':         {'code': 'HB',  'category': 'kit',      'name': 'Diverter box'},
'two position diverter':{'code': 'HB',  'category': 'kit',      'name': '2-Position Diverter box'},
```

### AS - Accuseal clamp

```python
'accuseal':             {'code': 'AS',  'category': 'clamp',    'name': 'Accuseal clamp'},
'accu-seal':            {'code': 'AS',  'category': 'clamp',    'name': 'Accuseal clamp'},
'accu seal':            {'code': 'AS',  'category': 'clamp',    'name': 'Accuseal clamp'},
```

Note: Sample SKUs (AS-225A "2.25 ACCUSEAL CLAMP") confirm this. The SAS code (Sleeved Accu-Seal) is distinct; reps must qualify with "sleeved" to get SAS instead of AS.

### QP - Quiet Performance insert

```python
'quiet performance':    {'code': 'QP',  'category': 'kit',      'name': 'Quiet Performance insert'},
'qp insert':            {'code': 'QP',  'category': 'kit',      'name': 'Quiet Performance insert'},
'quiet performance insert': {'code': 'QP', 'category': 'kit', 'name': 'Quiet Performance insert'},
```

### HD - Universal hanger (tail pipe)

```python
'hd hanger':            {'code': 'HD',  'category': 'accessory', 'name': 'Heavy Duty Universal Hanger'},
'tail pipe hanger':     {'code': 'HD',  'category': 'accessory', 'name': 'Tail Pipe Hanger (Universal)'},
'universal hanger':     {'code': 'HD',  'category': 'accessory', 'name': 'Universal Hanger'},
```

Note for agent: HD descriptions say "HANGER ... TAIL PIPE UNIVERSAL." HD as a clamp-grade attribute (Heavy Duty) is described elsewhere; here HD is the family code for these specific hangers. Reps probably say "tail pipe hanger" or "universal hanger" rather than literally "HD."

### WFF - Westfalia flex

```python
'westfalia':            {'code': 'WFF', 'category': 'flex',     'name': 'Westfalia flex'},
'westfalia flex':       {'code': 'WFF', 'category': 'flex',     'name': 'Westfalia flex'},
'wff':                  {'code': 'WFF', 'category': 'flex',     'name': 'Westfalia flex'},
```

### TRF - Turbo Repair Flare

```python
'turbo repair':         {'code': 'TRF', 'category': 'pipe',     'name': 'Turbo Repair Flare'},
'turbo repair flare':   {'code': 'TRF', 'category': 'pipe',     'name': 'Turbo Repair Flare'},
'trf':                  {'code': 'TRF', 'category': 'pipe',     'name': 'Turbo Repair Flare'},
```

### RS - Resonator / Connector (SS)

Sample SKUs show "CONNECTOR" descriptions. Possibly a sub-variant of CN family. Map cautiously.

```python
# Skip vocabulary for RS unless the SME confirms a specific rep phrase.
# Sample descriptions just say "CONNECTOR" which already maps to CN.
```

**Flag for the SME:** RS sample descriptions all say "CONNECTOR." Is RS a different family from CN, or are they functionally equivalent and just different SKU namespaces?

### PRKY - Peterbilt Retrofit Kit Y-pipe

```python
'peterbilt retrofit kit y-pipe': {'code': 'PRKY', 'category': 'kit', 'name': 'Peterbilt Retrofit Y-Pipe Kit'},
'prk y-pipe':           {'code': 'PRKY', 'category': 'kit',     'name': 'Peterbilt Retrofit Y-Pipe Kit'},
'pete retro y':         {'code': 'PRKY', 'category': 'kit',     'name': 'Peterbilt Retrofit Y-Pipe Kit'},
```

Note: PRK (Peterbilt Retrofit Kit) is already in the vocabulary. PRKY adds the Y-pipe variant. Reps probably specify "peterbilt retrofit y-pipe" or qualify the kit. If reps just say "PRK y-pipe" the existing PRK mapping plus Y-pipe should compose; verify whether catalog SKUs actually exist as PRK-something-Y or as PRKY-something.

### HDT - Heavy Duty Truck Y-pipe (Y-pipe variant)

```python
# Skip vocabulary unless the SME confirms a specific rep phrase.
# Sample descriptions just say "Y-PIPE" which already maps to Y family.
```

**Flag for the SME:** HDT samples all describe Y-pipes. What makes HDT different from Y or YC? Heavy-duty truck application?

### RMH - Round Muffler Hanger

```python
'round muffler hanger': {'code': 'RMH', 'category': 'accessory', 'name': 'Round Muffler Hanger'},
'rmh':                  {'code': 'RMH', 'category': 'accessory', 'name': 'Round Muffler Hanger'},
```

### AC - Aero Cab brackets / Emission components

```python
'aero cab':             {'code': 'AC',  'category': 'accessory', 'name': 'Aero Cab bracket'},
'aerocab bracket':      {'code': 'AC',  'category': 'accessory', 'name': 'Aero Cab bracket'},
'emission bellows':     {'code': 'AC',  'category': 'flex',     'name': 'Emission Bellows'},
'emission flex':        {'code': 'AC',  'category': 'flex',     'name': 'Emission Flex'},
```

**Flag for the SME:** AC family has two distinct product lines (Aero Cab brackets and emission flex components). Confirm this is intentional, or whether one should be a separate family.

### RO - Round Open clamp (Saddle clamp variant)

```python
'round open':           {'code': 'RO',  'category': 'clamp',    'name': 'Round Open clamp (Saddle)'},
'round-open':           {'code': 'RO',  'category': 'clamp',    'name': 'Round Open clamp (Saddle)'},
'ro clamp':             {'code': 'RO',  'category': 'clamp',    'name': 'Round Open clamp'},
```

Note: Samples say "CLAMP SADDLE." This is closely related to RB (also saddle clamps). Need disambiguation rule: RO is round-open saddle, RB is round-bolt saddle. Distinct.

### DIM - KW Aerocab Dual Muffler Kit

```python
'kw aerocab kit':       {'code': 'DIM', 'category': 'kit',      'name': 'KW Aerocab Dual Muffler Kit'},
'aerocab muffler kit':  {'code': 'DIM', 'category': 'kit',      'name': 'KW Aerocab Dual Muffler Kit'},
'dim kit':              {'code': 'DIM', 'category': 'kit',      'name': 'DIM kit'},
```

### PS - Ford Powerstroke kit

```python
'powerstroke kit':      {'code': 'PS',  'category': 'kit',      'name': 'Ford Powerstroke kit'},
'powerstroke':          {'code': 'PS',  'category': 'kit',      'name': 'Ford Powerstroke kit'},
'ford powerstroke':     {'code': 'PS',  'category': 'kit',      'name': 'Ford Powerstroke kit'},
'ps kit':               {'code': 'PS',  'category': 'kit',      'name': 'Ford Powerstroke kit'},
```

### US - West Coast Cut chrome stacks

```python
'west coast cut':       {'code': 'US',  'category': 'stack',    'name': 'West Coast Cut chrome stack'},
'westcoast cut':        {'code': 'US',  'category': 'stack',    'name': 'West Coast Cut chrome stack'},
'wcc stack':            {'code': 'US',  'category': 'stack',    'name': 'West Coast Cut chrome stack'},
```

Note for agent: WCK (West Coast Stack) is already in vocabulary. US is a different but related family. Distinguish by checking whether "cut" appears in the rep's request.

### UM - Universal dump stack (universal mount variant)

```python
'universal dump':       {'code': 'UM',  'category': 'stack',    'name': 'Universal mount Dump stack'},
'universal dump stack': {'code': 'UM',  'category': 'stack',    'name': 'Universal mount Dump stack'},
```

**Flag for the SME:** UM is one of several dump-stack variants (D, DTS, ED, OB, SW, JDS, UM, SK, OS). What distinguishes UM specifically?

### TSL - Tapered Elbow

```python
'tapered elbow':        {'code': 'TSL', 'category': 'elbow',    'name': 'Tapered Elbow'},
'tapered el':           {'code': 'TSL', 'category': 'elbow',    'name': 'Tapered Elbow'},
'taper elbow':          {'code': 'TSL', 'category': 'elbow',    'name': 'Tapered Elbow'},
```

### IM - Internal Baffle / Baffles

```python
'internal baffle':      {'code': 'IM',  'category': 'muffler',  'name': 'Internal Baffle'},
'baffle':               {'code': 'IM',  'category': 'muffler',  'name': 'Internal Baffle'},
'baffles':              {'code': 'IM',  'category': 'muffler',  'name': 'Internal Baffles'},
```

### DC - Dodge Cummins kit

```python
'dodge cummins':        {'code': 'DC',  'category': 'kit',      'name': 'Dodge/Cummins kit'},
'dodge kit':            {'code': 'DC',  'category': 'kit',      'name': 'Dodge/Cummins kit'},
'cummins kit':          {'code': 'DC',  'category': 'kit',      'name': 'Dodge/Cummins kit'},
```

### RE - Rubber Elbow

```python
'rubber elbow':         {'code': 'RE',  'category': 'elbow',    'name': 'Rubber Elbow'},
```

Note: Some RE samples say "ROLLED END" instead of rubber elbow. Possibly two product lines under one code. The "rubber elbow" mapping is unambiguous and high-volume; skip the rolled end mapping unless the SME confirms it.

### OB - OD Bottom Dump Stack

```python
'od bottom dump':       {'code': 'OB',  'category': 'stack',    'name': 'OD Bottom Dump Stack'},
'od btm dump':          {'code': 'OB',  'category': 'stack',    'name': 'OD Bottom Dump Stack'},
```

### HW - Heat Wrap

```python
'heat wrap':            {'code': 'HW',  'category': 'accessory', 'name': 'Heat Wrap'},
'exhaust wrap':         {'code': 'HW',  'category': 'accessory', 'name': 'Heat Wrap (Exhaust)'},
```

### F - Ford (OEM-cross)

Reference reps' SKU directly. Existing OEM_ALIASES likely covers 'ford'. Verify, but no new family-word entries needed if OEM detection routes correctly.

```python
# Skip vocabulary. the SME confirmed: F = Ford, OEM cross-reference like PB/FL/KW/IH.
# Reps reference these by explicit SKU.
```

### CN - Connector

```python
'connector':            {'code': 'CN',  'category': 'pipe',     'name': 'Connector'},
'connectors':           {'code': 'CN',  'category': 'pipe',     'name': 'Connector'},
```

### DTS - Dump Top Stack

```python
'dump top':             {'code': 'DTS', 'category': 'stack',    'name': 'Dump Top Stack'},
'dump top stack':       {'code': 'DTS', 'category': 'stack',    'name': 'Dump Top Stack'},
'dts':                  {'code': 'DTS', 'category': 'stack',    'name': 'Dump Top Stack'},
```

### HF - High Flow muffler

```python
'high flow':            {'code': 'HF',  'category': 'muffler',  'name': 'High Flow muffler'},
'high flow muffler':    {'code': 'HF',  'category': 'muffler',  'name': 'High Flow muffler'},
'hf muffler':           {'code': 'HF',  'category': 'muffler',  'name': 'High Flow muffler'},
```

### VK - Internal Dampner insert

```python
'internal dampner':     {'code': 'VK',  'category': 'muffler',  'name': 'Internal Dampner insert'},
'dampener insert':      {'code': 'VK',  'category': 'muffler',  'name': 'Internal Dampner insert'},
'dampner insert':       {'code': 'VK',  'category': 'muffler',  'name': 'Internal Dampner insert'},
```

### FK - Flex Pipe Kit

```python
'flex pipe kit':        {'code': 'FK',  'category': 'flex',     'name': 'Flex Pipe Kit'},
'flex kit':             {'code': 'FK',  'category': 'flex',     'name': 'Flex Pipe Kit'},
```

### HS - Heat Sleeve

```python
'heat sleeve':          {'code': 'HS',  'category': 'accessory', 'name': 'Heat Sleeve'},
```

### HSK - Hood Stack Kit

```python
'hood stack':           {'code': 'HSK', 'category': 'stack',    'name': 'Hood Stack Kit'},
'hood stack kit':       {'code': 'HSK', 'category': 'stack',    'name': 'Hood Stack Kit'},
```

### STC - Stainless T-Bolt Clamp

```python
# Samples just say "CLAMP, X SS" - generic. STC is OBSOLETE product group.
# Skip vocabulary unless reps actively request this code by name.
```

**Flag for the SME:** STC samples are all marked OBSOLETE. Worth mapping vocabulary, or skip as legacy-only?

### MMB - Powder coat clamp

```python
'powder coat clamp':    {'code': 'MMB', 'category': 'clamp',    'name': 'Powder Coat clamp'},
```

### GH - Grab Handle

```python
'grab handle':          {'code': 'GH',  'category': 'accessory', 'name': 'Grab Handle'},
```

### SPU - Single Pipe Universal kit

```python
'single pipe universal': {'code': 'SPU', 'category': 'kit',     'name': 'Single Pipe Universal kit'},
'single stack kit':     {'code': 'SPU', 'category': 'kit',      'name': 'Single Stack Kit'},
'spu':                  {'code': 'SPU', 'category': 'kit',      'name': 'Single Pipe Universal kit'},
'spu kit':              {'code': 'SPU', 'category': 'kit',      'name': 'Single Pipe Universal kit'},
```

Note: SPU is the single-stack counterpart to DPU (dual stack).

### EKM - Multi-bend muffler (universal)

```python
'multi muffler':        {'code': 'EKM', 'category': 'muffler',  'name': 'Multi-bend universal muffler'},
'universal muffler':    {'code': 'EKM', 'category': 'muffler',  'name': 'Universal muffler'},
```

### AT - Air T-Bolt clamp

```python
'air t-bolt':           {'code': 'AT',  'category': 'clamp',    'name': 'Air T-Bolt clamp'},
't-bolt clamp':         {'code': 'AT',  'category': 'clamp',    'name': 'Air T-Bolt clamp'},
'air tbolt':            {'code': 'AT',  'category': 'clamp',    'name': 'Air T-Bolt clamp'},
```

### JDS, FEC - Dump stack variants

**Flag for the SME:** Both JDS and FEC are dump-stack variants. What distinguishes them from D, DTS, ED, OB, SW, UM, SK, OS? If they're customer-specific or application-specific, the vocabulary needs to reflect that.

### EB - Emission Bellows

```python
'emission bellow':      {'code': 'EB',  'category': 'flex',     'name': 'Emission Bellows'},
'bellows ss':           {'code': 'EB',  'category': 'flex',     'name': 'Stainless Bellows'},
```

Note: Existing vocabulary has `'bellow'` → EBK (bellows kit). EB is bellows-only without kit. Disambiguation: "bellows kit" → EBK, plain "bellows" → ambiguous between EB and EBK, surface clarification.

### YC - Type C Y-pipe

```python
'type c y':             {'code': 'YC',  'category': 'pipe',     'name': 'Type C Y-pipe'},
'type c y pipe':        {'code': 'YC',  'category': 'pipe',     'name': 'Type C Y-pipe'},
```

### QPM - Quiet Performance Muffler

```python
'quiet performance muffler': {'code': 'QPM', 'category': 'muffler', 'name': 'Quiet Performance Muffler'},
'qp muffler':           {'code': 'QPM', 'category': 'muffler',  'name': 'Quiet Performance Muffler'},
'qpm':                  {'code': 'QPM', 'category': 'muffler',  'name': 'Quiet Performance Muffler'},
```

### ACFM - Aerocab Frame Mount bracket

```python
'aerocab frame':        {'code': 'ACFM', 'category': 'accessory', 'name': 'Aerocab Frame Mount bracket'},
'acfm bracket':         {'code': 'ACFM', 'category': 'accessory', 'name': 'Aerocab Frame Mount bracket'},
```

### OS - Offset Stack Mount Bracket

```python
'offset stack mount':   {'code': 'OS',  'category': 'accessory', 'name': 'Offset Stack Mount Bracket'},
'offset stack bracket': {'code': 'OS',  'category': 'accessory', 'name': 'Offset Stack Mount Bracket'},
```

### SW - Dump Stack (variant)

**Flag for the SME:** SW samples show "DUMP STACK 18/19/27." Another dump-stack variant. What's its distinguishing attribute?

### FT - Ford Truck

the SME confirmed: FT = Ford Truck. OEM cross-reference like F. Reps reference by explicit SKU.

```python
# Skip family vocabulary. Existing OEM_ALIASES likely covers 'ford truck' if needed.
```

## LOW PRIORITY (selective additions)

Most LOW-tier families are low-volume with small SKU counts. Adding vocabulary for all 28 is probably diminishing returns. Add only the ones below where descriptions clearly suggest rep phrases:

### PH - Pipe Hanger

```python
'pipe hanger':          {'code': 'PH',  'category': 'accessory', 'name': 'Pipe Hanger'},
```

### HH - Hump Hose

```python
'hump hose':            {'code': 'HH',  'category': 'flex',     'name': 'Hump Hose (Rubber)'},
```

### TBE - Tilt Bell

```python
'tilt bell':            {'code': 'TBE', 'category': 'pipe',     'name': 'Tilt Bell'},
```

### GBS - Bellows Flex

```python
'bellows flex':         {'code': 'GBS', 'category': 'flex',     'name': 'Bellows Flex'},
```

### RHH - Rubber Reducer Hump Hose

```python
'rubber reducer':       {'code': 'RHH', 'category': 'flex',     'name': 'Rubber Reducer Hose'},
```

### RRE - Rubber Reducer Elbow

```python
'rubber reducer elbow': {'code': 'RRE', 'category': 'elbow',    'name': 'Rubber Reducer Elbow'},
```

### LH - Longhorn Stack

```python
'longhorn':             {'code': 'LH',  'category': 'stack',    'name': 'Longhorn Stack'},
'longhorn stack':       {'code': 'LH',  'category': 'stack',    'name': 'Longhorn Stack'},
```

### PGH - Pipe Grab Handle (Stainless)

```python
'pipe grab handle':     {'code': 'PGH', 'category': 'accessory', 'name': 'Pipe Grab Handle'},
```

### EBK - Bellows Kit

```python
'bellows kit':          {'code': 'EBK', 'category': 'flex',     'name': 'Bellows Kit'},
'bellow kit':           {'code': 'EBK', 'category': 'flex',     'name': 'Bellows Kit'},
```

### RF - Relaxed Length Flex (SS)

```python
'relaxed length':       {'code': 'RF',  'category': 'flex',     'name': 'Relaxed Length Flex'},
'relaxed flex':         {'code': 'RF',  'category': 'flex',     'name': 'Relaxed Length Flex'},
```

### TPB / TKW / FNT / FTE / PRKY / IHCC / DPFVB

These all have specialized SKU patterns and low rep-phrasing applicability. **Skip vocabulary** unless the SME specifies rep phrases.

## TRIVIAL (skip)

All 36 trivial-tier families are very low volume. Skip vocabulary entirely unless one of them is a high-frequency support request despite low revenue.

## Disambiguation rules to add

The audit also surfaced disambiguation rules the agent should implement:

### Position-based code resolution (already implemented)

- `SB` at SKU start = School Bus family (handled in part_number_parser/_patterns.py)
- `SB` between digits = Straight Bottom body code (handled in BODY_ALIASES)
- No agent action needed; verify continues to work after vocabulary additions.

### Ambiguous family words to add to AMBIGUOUS_FAMILY_WORDS

```python
'muffler hanger': {
    'category': 'accessory',
    'options': ['RMH', 'OMH', 'H'],
    'reason': "'muffler hanger' alone is ambiguous; could be RMH (round), OMH (oval), or H (generic hanger). Clarify with the customer.",
},
'bellows': {
    'category': 'flex',
    'options': ['EB', 'EBK'],
    'reason': "'bellows' alone is ambiguous; could be EB (bellows only) or EBK (bellows kit with mounting hardware). Clarify if rep needs the kit.",
},
'dump stack': {
    'category': 'stack',
    'options': ['D', 'DTS', 'ED', 'OB', 'JDS', 'SW', 'UM', 'FEC'],
    'reason': "'dump stack' alone matches many variants. Ask which variant (end dump, OD bottom, dump top, etc).",
},
```

### Order of recognition

Add the new entries to FAMILY_WORD_ALIASES. The existing recognition order (longest match wins) handles multi-word phrases correctly. Verify that "world's finest clamp" matches before "clamp" alone resolves to ambiguous.

## Items still requiring the SME's input

The following flags require the SME to provide guidance before the agent can add vocabulary:

1. **RS family**: Sample descriptions say "CONNECTOR" but the family code is different from CN. Same product or different?
2. **HDT family**: All samples are Y-pipes. What distinguishes HDT from Y, YC, or PRKY?
3. **AC family**: Two distinct product lines (Aero Cab brackets and Emission flex). Intentional, or should one split?
4. **UM, SW, JDS, FEC families**: All dump-stack variants. What attribute distinguishes each?
5. **STC family**: All OBSOLETE. Worth vocabulary, or skip as legacy?

The agent should implement everything above and leave the flagged items for a follow-up pass after the SME answers.

## Execution notes for the agent

1. **Read the existing FAMILY_WORD_ALIASES dict completely before adding entries.** Some words from this spec might already exist with different mappings; do not overwrite without checking.

2. **The dict insertion order matters for documentation but not for matching.** The normalizer uses dict lookup, not iteration order. However, when adding many entries, group them by family code with a comment header for maintainability:

```python
# === Couplers ===
'coupler':              {'code': 'CP', ...},
'couplers':             {'code': 'CP', ...},

# === V-Band clamps ===
'v-band':               {'code': 'VB', ...},
...
```

3. **Test each addition against the existing test suite first.** A breaking change in vocabulary recognition could cascade. Run `test_integration.py` after each family's additions.

4. **Do not modify part_number_parser/_patterns.py family code definitions.** The parser already knows these codes. The vocabulary layer just teaches the system to recognize rep phrasing.

5. **For families flagged as needing the SME's input, add a TODO comment with the family code rather than guessing.**

```python
# TODO: RS family vocabulary pending the SME confirmation (RS vs CN distinction)
# TODO: HDT family vocabulary pending the SME confirmation (HDT vs Y distinction)
```
