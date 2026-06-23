"""
SKU Translator — Lexical Normalization Layer
=============================================

Pure-function layer that normalizes free-form human input into structured
spec attributes that downstream components (entity extractor, fuzzy matcher,
SKU constructor) can consume.

This is the foundation of the SKU translation pipeline. Every other component
calls into the normalizers here. No state, no I/O, no side effects.

Design principles
-----------------
1. **Idempotent.** Running a normalizer twice produces the same result as
   running it once.
2. **Frequency-driven aliases.** Alias dictionaries are sized by how often
   reps actually use each variant in catalog descriptions and rep notes.
   ALZ (2574 descriptions) is canonical; aluminized and ALUMINIZED both
   normalize to A. CHR (967) and CHROME (1198) both normalize to C.
3. **Separation of concerns.** Each normalizer handles one attribute class.
   Composition happens in the entity extractor, not here.
4. **Lossless intermediate forms.** Normalizers return structured data, not
   stringified canonicals. The SKU constructor decides how to render to a
   final SKU string.

Usage
-----
::

    from sku_translator.normalizer import (
        normalize_finish,
        normalize_dimension,
        normalize_fit,
        normalize_family_word,
        normalize_oem,
        normalize_input,
    )

    # Single-attribute normalization
    normalize_finish('chr')       # -> {'code': 'C', 'meaning': 'Chrome'}
    normalize_dimension('5"')     # -> {'value': 5.0, 'unit': 'inch'}
    normalize_fit('id/od')        # -> {'inlet': 'ID', 'outlet': 'OD'}
    normalize_oem('pete')         # -> {'code': 'PB', 'meaning': 'Peterbilt'}

    # Whole-input normalization (for downstream extraction)
    spec = normalize_input(
        '5 inch chrome curved stack 24 inches long, ID/OD'
    )
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# ============================================================================
# Section 1: Surface normalization
# ============================================================================
# Cleans up the raw input before any pattern matching. Strips smart quotes,
# normalizes whitespace, lowercases, etc.


SMART_QUOTE_TO_PLAIN = {
    '\u2018': "'",  # left single quote
    '\u2019': "'",  # right single quote
    '\u201c': '"',  # left double quote
    '\u201d': '"',  # right double quote
    '\u2032': "'",  # prime
    '\u2033': '"',  # double prime
}


def normalize_surface(text: str) -> str:
    """Clean the raw input string. Idempotent.

    - Convert smart quotes to plain ASCII quotes (reps copy-paste from email)
    - Strip leading/trailing whitespace
    - Collapse internal whitespace runs to single spaces
    - Preserve case (case-folding happens at the matching layer, not here,
      because some attributes — like SKUs — are case-sensitive)
    """
    if text is None:
        return ''
    s = str(text)
    # Normalize Unicode (e.g., precomposed accents)
    s = unicodedata.normalize('NFKC', s)
    # Replace smart quotes
    for smart, plain in SMART_QUOTE_TO_PLAIN.items():
        s = s.replace(smart, plain)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


# ============================================================================
# Section 1b: Spoken-word substitution (NATO alphabet, number words)
# ============================================================================
# Voice-dictated and phonetic input arrives with spoken-out letters and numbers
# instead of the compact alphanumeric form that real SKUs use. This pass turns
# tokens like 'Kilo five dash thirty-six Sierra Bravo Charlie' into 'K5-36SBC'
# so the downstream tokenizer can recognize them as SKU fragments.
#
# Per-token substitution runs first (NATO -> letter, number word -> digit,
# 'dash' -> '-', fraction word -> decimal). Then a second pass joins runs of
# spelled tokens together with adjacent short alpha tokens that look like SKU
# family or suffix codes. Unit words like 'inch' / 'foot' / 'gauge' are
# explicitly blocked from being absorbed, so '0.5 inch' stays as two tokens
# rather than collapsing into '0.5INCH'.


NATO_ALPHABET: dict[str, str] = {
    'alpha': 'A', 'alfa': 'A',
    'bravo': 'B',
    'charlie': 'C',
    'delta': 'D',
    'echo': 'E',
    'foxtrot': 'F',
    'golf': 'G',
    'hotel': 'H',
    'india': 'I',
    'juliet': 'J', 'juliett': 'J',
    'kilo': 'K',
    'lima': 'L',
    'mike': 'M',
    'november': 'N',
    'oscar': 'O',
    'papa': 'P',
    'quebec': 'Q',
    'romeo': 'R',
    'sierra': 'S',
    'tango': 'T',
    'uniform': 'U',
    'victor': 'V',
    'whiskey': 'W', 'whisky': 'W',
    'x-ray': 'X', 'xray': 'X',
    'yankee': 'Y',
    'zulu': 'Z',
}


NUMBER_WORDS: dict[str, str] = {
    'zero': '0', 'oh': '0',
    'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7', 'eight': '8',
    'nine': '9', 'niner': '9',
    'ten': '10', 'eleven': '11', 'twelve': '12',
    'thirteen': '13', 'fourteen': '14', 'fifteen': '15',
    'sixteen': '16', 'seventeen': '17', 'eighteen': '18',
    'nineteen': '19', 'twenty': '20',
    'thirty': '30', 'forty': '40', 'fifty': '50',
    'sixty': '60', 'seventy': '70', 'eighty': '80',
    'ninety': '90', 'hundred': '100',
}


def _build_number_compounds() -> dict[str, str]:
    tens = [('twenty', 20), ('thirty', 30), ('forty', 40), ('fifty', 50),
            ('sixty', 60), ('seventy', 70), ('eighty', 80), ('ninety', 90)]
    ones = [('one', 1), ('two', 2), ('three', 3), ('four', 4),
            ('five', 5), ('six', 6), ('seven', 7), ('eight', 8), ('nine', 9)]
    out: dict[str, str] = {}
    for t_word, t_val in tens:
        for o_word, o_val in ones:
            out[f'{t_word}-{o_word}'] = str(t_val + o_val)
    return out


# Hyphenated compound numbers: 'twenty-four' -> '24', 'thirty-six' -> '36', ...
NUMBER_COMPOUNDS: dict[str, str] = _build_number_compounds()


FRACTION_WORDS: dict[str, str] = {
    'quarter': '0.25',
    'half': '0.5',
    'three-quarter': '0.75', 'three-quarters': '0.75',
}


# Multi-word number phrases, matched before single-word substitution so that
# 'four and a half' beats 'four' + 'and' + 'a' + 'half'. Also covers
# space-separated tens compounds ('twenty four' -> '24') for transcripts that
# drop the hyphen.
def _build_number_phrases() -> dict[str, str]:
    phrases: dict[str, str] = {
        'one half': '0.5', 'one quarter': '0.25',
        'three quarter': '0.75', 'three quarters': '0.75',
        'one and a half': '1.5', 'two and a half': '2.5',
        'three and a half': '3.5', 'four and a half': '4.5',
        'five and a half': '5.5', 'six and a half': '6.5',
        'seven and a half': '7.5', 'eight and a half': '8.5',
        'nine and a half': '9.5', 'ten and a half': '10.5',
    }
    # Space-separated tens compounds: 'twenty four' -> '24', etc.
    for compound, value in NUMBER_COMPOUNDS.items():
        phrases[compound.replace('-', ' ')] = value
    return phrases


NUMBER_PHRASES: dict[str, str] = _build_number_phrases()


# Words spoken as the '-' separator in voice-dictated SKUs.
DASH_WORDS: set[str] = {'dash', 'hyphen'}


# Short alpha tokens that should NEVER be absorbed into an adjacent
# spelled-token run, even though they're 1-4 letters long. Unit markers like
# 'inch' must remain separate so dimensional parsing can pair them with a
# value; English filler words must stay out so 'I need a 5 inch' doesn't get
# glued to anything.
_ABSORB_BLOCKLIST: set[str] = {
    'inch', 'inches', 'foot', 'feet', 'gauge', 'gauges', 'ga',
    'long', 'wide', 'tall', 'high', 'deep', 'thick',
    'the', 'and', 'a', 'an', 'or', 'of', 'to', 'by', 'is', 'are',
    'for', 'on', 'in', 'at', 'as', 'be', 'do', 'go', 'so', 'up',
    'i', 'we', 'you', 'he', 'she', 'it',
}


def _is_absorbable_alpha(s: str) -> bool:
    """True if s is a short alphabetic token (1-4 letters) that may be merged
    into an adjacent spelled-token run as part of a SKU fragment."""
    if not s or len(s) > 4 or not s.isalpha():
        return False
    if s.lower() in _ABSORB_BLOCKLIST:
        return False
    return True


def _normalize_spoken_words(text: str) -> str:
    """Replace NATO phonetics, number words, and fraction words with their
    digit/letter equivalents, then merge consecutive spelled tokens (plus
    adjacent short alpha tokens) into compact SKU-fragment-like strings.

    Examples
    --------
    >>> _normalize_spoken_words('Kilo five dash thirty-six Sierra Bravo Charlie')
    'K5-36SBC'
    >>> _normalize_spoken_words('thirty-six inches')
    '36 inches'
    >>> _normalize_spoken_words('half inch')
    '0.5 inch'
    >>> _normalize_spoken_words('5 inch chrome curved stack')
    '5 inch chrome curved stack'
    """
    if not text:
        return text
    raw_tokens = text.split()
    if not raw_tokens:
        return text

    # Phase 1: per-token substitution. Tracks (text, was_spelled).
    out: list[tuple[str, bool]] = []
    i = 0
    while i < len(raw_tokens):
        word = raw_tokens[i]
        word_clean = word.lower().rstrip('.,;:!?')
        suffix = word[len(word_clean):]

        # Multi-word phrases first (4-word, 3-word, 2-word).
        matched = False
        for span in (4, 3, 2):
            if i + span > len(raw_tokens):
                continue
            phrase = ' '.join(
                rt.lower().rstrip('.,;:!?') for rt in raw_tokens[i:i + span]
            )
            if phrase in NUMBER_PHRASES:
                last = raw_tokens[i + span - 1]
                trail_match = re.search(r'[.,;:!?]+$', last)
                trail = trail_match.group() if trail_match else ''
                out.append((NUMBER_PHRASES[phrase] + trail, True))
                i += span
                matched = True
                break
        if matched:
            continue

        if word_clean in NUMBER_COMPOUNDS:
            out.append((NUMBER_COMPOUNDS[word_clean] + suffix, True))
            i += 1
            continue
        if word_clean in NATO_ALPHABET:
            out.append((NATO_ALPHABET[word_clean] + suffix, True))
            i += 1
            continue
        if word_clean in NUMBER_WORDS:
            out.append((NUMBER_WORDS[word_clean] + suffix, True))
            i += 1
            continue
        if word_clean in FRACTION_WORDS:
            out.append((FRACTION_WORDS[word_clean] + suffix, True))
            i += 1
            continue
        if word_clean in DASH_WORDS:
            out.append(('-', True))
            i += 1
            continue

        out.append((word, False))
        i += 1

    # Phase 2: merge runs. A run is a stretch of tokens where at least one was
    # spelled; non-spelled short-alpha tokens at the boundaries are absorbed.
    merged: list[str] = []
    j = 0
    while j < len(out):
        text_tok, is_spelled = out[j]

        if not is_spelled:
            # Lookahead: does a spelled run start within reach (through
            # short-alpha tokens)? If so, this is the leading absorbed prefix.
            if _is_absorbable_alpha(text_tok):
                k = j + 1
                run_has_spelled = False
                while k < len(out):
                    nt, ns = out[k]
                    if ns:
                        run_has_spelled = True
                        k += 1
                    elif _is_absorbable_alpha(nt):
                        k += 1
                    else:
                        break
                if run_has_spelled:
                    pieces = [
                        out[m][0].upper() if _is_absorbable_alpha(out[m][0])
                        else out[m][0]
                        for m in range(j, k)
                    ]
                    merged.append(''.join(pieces))
                    j = k
                    continue
            merged.append(text_tok)
            j += 1
            continue

        # Spelled token: build a run forward, then absorb any short-alpha
        # tokens already pushed onto `merged` (e.g., 'BH' before 'thirty-six').
        run = [text_tok]
        k = j + 1
        while k < len(out):
            nt, ns = out[k]
            if ns:
                run.append(nt)
                k += 1
            elif _is_absorbable_alpha(nt):
                run.append(nt.upper())
                k += 1
            else:
                break
        while merged and _is_absorbable_alpha(merged[-1]):
            run.insert(0, merged.pop().upper())
        merged.append(''.join(run))
        j = k

    return ' '.join(merged)


# ============================================================================
# Section 2: Dimension normalization
# ============================================================================
# Numbers + units. Catalog descriptions show diameters and lengths written
# many ways: `5"`, `5 inch`, `5 in`, `5'`, `5IN`, `five inch`, etc.
# All collapse to a structured value.


# Spelled-out small numbers reps occasionally use
SPELLED_NUMBERS = {
    'one':       1.0,   'two':       2.0,   'three':     3.0,
    'four':      4.0,   'five':      5.0,   'six':       6.0,
    'seven':     7.0,   'eight':     8.0,   'nine':      9.0,
    'ten':      10.0,
    # Common fractional forms
    'one half':  0.5,   'half':      0.5,
    'quarter':   0.25,  'three quarter': 0.75,  'three quarters': 0.75,
    'two and a half': 2.5, 'three and a half': 3.5, 'four and a half': 4.5,
    'five and a half': 5.5,
}

# Inch-unit aliases. Normalize anything that means "inches" to a canonical 'inch'.
INCH_UNIT_ALIASES = {
    'inch', 'inches', 'in', '"', "''", "''",  # double-prime variants
    'i.n.', 'in.',
}

# Foot-unit aliases (rare in this catalog but reps use them)
FOOT_UNIT_ALIASES = {
    'foot', 'feet', 'ft', "'", 'ft.',
}

# Gauge unit (for material thickness)
GAUGE_UNIT_ALIASES = {
    'gauge', 'gauges', 'ga', 'ga.', 'g.a.',
}


def _spelled_number_to_float(text: str) -> float | None:
    """Convert spelled-out numbers like 'five and a half' to 5.5. None if not recognized."""
    cleaned = text.lower().strip()
    if cleaned in SPELLED_NUMBERS:
        return SPELLED_NUMBERS[cleaned]
    return None


def normalize_dimension(text: str) -> dict[str, Any] | None:
    """Parse a single dimension expression into ``{value, unit}``.

    Examples
    --------
    >>> normalize_dimension('5"')
    {'value': 5.0, 'unit': 'inch'}
    >>> normalize_dimension('5 inch')
    {'value': 5.0, 'unit': 'inch'}
    >>> normalize_dimension('3.5"')
    {'value': 3.5, 'unit': 'inch'}
    >>> normalize_dimension('14 ga')
    {'value': 14.0, 'unit': 'gauge'}
    >>> normalize_dimension('two')
    {'value': 2.0, 'unit': None}

    Returns None if the input doesn't look like a dimension at all.
    """
    if not text:
        return None
    cleaned = normalize_surface(text).lower()

    # Try spelled-out form first (most fragile path; quick exit if no match)
    spelled_value = _spelled_number_to_float(cleaned)
    if spelled_value is not None:
        return {'value': spelled_value, 'unit': None}

    # Numeric form: capture the number, then look at trailing unit token
    m = re.match(r'^([\d.]+)\s*(.*)$', cleaned)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit_text = m.group(2).strip().rstrip('.')

    if not unit_text:
        return {'value': value, 'unit': None}
    if unit_text in INCH_UNIT_ALIASES:
        return {'value': value, 'unit': 'inch'}
    if unit_text in FOOT_UNIT_ALIASES:
        return {'value': value * 12, 'unit': 'inch'}  # convert feet to inches
    if unit_text in GAUGE_UNIT_ALIASES:
        return {'value': value, 'unit': 'gauge'}

    # Unknown unit suffix: return value with raw unit text so caller can decide
    return {'value': value, 'unit': unit_text}


# Compound dimension regex: matches `5"x24"`, `5 x 24`, `5 by 24`, `5 X 24"`, etc.
COMPOUND_DIM_RE = re.compile(
    r'(?P<a>[\d.]+)\s*(?P<unit_a>"|in|inch(?:es)?|\')?'
    r'\s*(?:x|X|by|BY|\*)\s*'
    r'(?P<b>[\d.]+)\s*(?P<unit_b>"|in|inch(?:es)?|\')?',
)


def normalize_compound_dimension(text: str) -> dict[str, Any] | None:
    """Parse compound dimensions like ``5"x24"`` or ``5 by 24 inches``.

    Returns ``{first: {value, unit}, second: {value, unit}}`` or None.
    """
    if not text:
        return None
    cleaned = normalize_surface(text)
    m = COMPOUND_DIM_RE.search(cleaned)
    if not m:
        return None
    first = normalize_dimension(f"{m.group('a')}{m.group('unit_a') or ''}")
    second = normalize_dimension(f"{m.group('b')}{m.group('unit_b') or ''}")
    # If only one side had an explicit unit, share it
    if first and second:
        if first['unit'] is None and second['unit'] is not None:
            first['unit'] = second['unit']
        elif second['unit'] is None and first['unit'] is not None:
            second['unit'] = first['unit']
    return {'first': first, 'second': second}


# ============================================================================
# Section 3: Finish normalization
# ============================================================================
# Map every way a rep might write a finish to its canonical code.
# Prevalence numbers from catalog descriptions (10,669 SKUs surveyed):
#   ALZ:        2574  -> A
#   CHROME:     1198  -> C
#   CHR:         967  -> C
#   409:         156  -> S4
#   RAW:         142  -> S or R depending on context
#   304:         119  -> S3
#   BLACK:        92  -> BS
#   ALUMINIZED:   86  -> A
#   STAINLESS:    74  -> S3 or S4 depending on context
#   POLISHED:     36  -> S3 (commonly polished 304)
#   CRM:          31  -> C


FINISH_ALIASES: dict[str, dict[str, Any]] = {
    # Aluminized (ALZ) — canonical code A
    'alz':           {'code': 'A', 'meaning': 'Aluminized (ALZ)'},
    'aluminized':    {'code': 'A', 'meaning': 'Aluminized (ALZ)'},
    'aluminum':      {'code': 'A', 'meaning': 'Aluminized (ALZ)'},
    'alum':          {'code': 'A', 'meaning': 'Aluminized (ALZ)'},
    # Note: 'a' alone is NOT a finish alias — it conflicts with the English
    # indefinite article. The SKU-fragment recognizer handles cases like
    # 'K5A' where A trails a diameter.

    # Chrome — canonical code C
    'chrome':        {'code': 'C', 'meaning': 'Chrome'},
    'chr':           {'code': 'C', 'meaning': 'Chrome'},
    'crm':           {'code': 'C', 'meaning': 'Chrome'},
    'chromed':       {'code': 'C', 'meaning': 'Chrome'},
    # Note: 'c' alone is NOT a finish alias — too generic in English.

    # Plater (work-in-process) — canonical code P
    'at plater':     {'code': 'P', 'meaning': 'At Plater (work-in-process)'},
    'plater':        {'code': 'P', 'meaning': 'At Plater (work-in-process)'},
    'wip':           {'code': 'P', 'meaning': 'At Plater (work-in-process)'},

    # Raw / cold-rolled — canonical code R
    'raw':           {'code': 'R', 'meaning': 'Raw / Cold-Rolled (unfinished)'},
    'cold roll':     {'code': 'R', 'meaning': 'Raw / Cold-Rolled (unfinished)'},
    'cold-roll':     {'code': 'R', 'meaning': 'Raw / Cold-Rolled (unfinished)'},
    'cold rolled':   {'code': 'R', 'meaning': 'Raw / Cold-Rolled (unfinished)'},
    'c/r':           {'code': 'R', 'meaning': 'Raw / Cold-Rolled (unfinished)'},
    'cr':            {'code': 'R', 'meaning': 'Raw / Cold-Rolled (unfinished)'},

    # 304 stainless — canonical code S3
    '304':           {'code': 'S3', 'meaning': '304 stainless steel'},
    '304 ss':        {'code': 'S3', 'meaning': '304 stainless steel'},
    '304ss':         {'code': 'S3', 'meaning': '304 stainless steel'},
    '304 stainless': {'code': 'S3', 'meaning': '304 stainless steel'},
    'polished':      {'code': 'S3', 'meaning': '304 stainless steel (polished)'},
    'polish':        {'code': 'S3', 'meaning': '304 stainless steel (polished)'},
    'p304':          {'code': 'S3', 'meaning': '304 stainless steel'},

    # 409 stainless — canonical code S4
    '409':           {'code': 'S4', 'meaning': '409 stainless steel'},
    '409 ss':        {'code': 'S4', 'meaning': '409 stainless steel'},
    '409ss':         {'code': 'S4', 'meaning': '409 stainless steel'},
    '409 stainless': {'code': 'S4', 'meaning': '409 stainless steel'},

    # Black series — canonical code BS
    'black':         {'code': 'BS', 'meaning': 'Black Series (matte black)'},
    'black series':  {'code': 'BS', 'meaning': 'Black Series (matte black)'},
    'matte black':   {'code': 'BS', 'meaning': 'Black Series (matte black)'},
    'bs':            {'code': 'BS', 'meaning': 'Black Series (matte black)'},

    # Black brushed chrome (deprecated)
    'bbc':                  {'code': 'BBC', 'meaning': 'Black Brushed Chrome (deprecated)'},
    'black brushed chrome': {'code': 'BBC', 'meaning': 'Black Brushed Chrome (deprecated)'},

    # Smooth raw / smooth joint
    'smooth raw':    {'code': 'SR', 'meaning': 'Smooth Raw (non-expanded, before plating)'},
    'sr':            {'code': 'SR', 'meaning': 'Smooth Raw (non-expanded, before plating)'},
    'smooth joint':  {'code': 'SJ', 'meaning': 'Smooth Joint'},
    'sj':            {'code': 'SJ', 'meaning': 'Smooth Joint'},
}

# 'stainless' is genuinely ambiguous — could be S3 (304) or S4 (409). Caller
# resolves with context. We report it but flag the ambiguity.
AMBIGUOUS_FINISHES: dict[str, list[str]] = {
    'stainless':    ['S3', 'S4'],
    'ss':           ['S3', 'S4'],
    'stainless steel': ['S3', 'S4'],
    # Note: bare 's' is NOT included — too easily a false positive on
    # English contractions, possessives, or arbitrary letters. The
    # SKU-fragment recognizer handles cases where 'S' trails a diameter.
}


def normalize_finish(text: str) -> dict[str, Any] | None:
    """Map a finish word/phrase to its canonical code.

    Returns ``{code, meaning}`` for unambiguous finishes, or
    ``{ambiguous: True, candidates: [...], raw: ...}`` for ambiguous ones.
    Returns None if the input doesn't look like a finish reference.
    """
    if not text:
        return None
    cleaned = normalize_surface(text).lower().strip(' .,;:')
    if cleaned in FINISH_ALIASES:
        return dict(FINISH_ALIASES[cleaned])  # copy
    if cleaned in AMBIGUOUS_FINISHES:
        return {
            'ambiguous': True,
            'candidates': list(AMBIGUOUS_FINISHES[cleaned]),
            'raw': cleaned,
            'reason': f"'{cleaned}' could mean either 304 (S3) or 409 (S4) stainless",
        }
    return None


# ============================================================================
# Section 4: Family-word normalization
# ============================================================================
# Map natural-language family names to family codes.
# Catalog prevalence:
#   STACK:    1310    (generic — could be K, BH, BR, A, etc.)
#   BEND:     1706    (means elbow when count >= 1)
#   ELBOW:    1184    -> L family
#   PIPE:     1132    -> S, R, ZP families (depends on context)
#   CURVED:    554    -> K family hint
#   STRAIGHT:  517    -> S family hint
#   MUFFLER:   316    -> M, ZM, CM families
#   TURBO:     283    -> T family hint
#   AUSSIE:    187    -> A family
#   REDUCER:   175    -> R family
#   BULLHORN:  147    -> BH family
#   BRUTE:     145    -> BR family
#   DUMP:      130    -> D family
#   CURVE:     120    -> K family hint
#   WEST COAST: 115   -> WCK family
#   SPOOL:      23    -> SP family


FAMILY_WORD_ALIASES: dict[str, dict[str, Any]] = {
    # Stack families (specific)
    'curved':            {'code': 'K',   'category': 'stack', 'name': 'Curved-Top Stack'},
    'curve':             {'code': 'K',   'category': 'stack', 'name': 'Curved-Top Stack'},
    'curved stack':      {'code': 'K',   'category': 'stack', 'name': 'Curved-Top Stack'},
    'curve stack':       {'code': 'K',   'category': 'stack', 'name': 'Curved-Top Stack'},
    'curved top stack':  {'code': 'K',   'category': 'stack', 'name': 'Curved-Top Stack'},
    'curve top stack':   {'code': 'K',   'category': 'stack', 'name': 'Curved-Top Stack'},
    'k stack':           {'code': 'K',   'category': 'stack', 'name': 'Curved-Top Stack'},
    'k-stack':           {'code': 'K',   'category': 'stack', 'name': 'Curved-Top Stack'},

    'bullhorn':          {'code': 'BH',  'category': 'stack', 'name': 'Bullhorn Stack'},
    'bullhorn stack':    {'code': 'BH',  'category': 'stack', 'name': 'Bullhorn Stack'},
    'bull horn':         {'code': 'BH',  'category': 'stack', 'name': 'Bullhorn Stack'},

    'brute':             {'code': 'BR',  'category': 'stack', 'name': 'Brute Stack'},
    'brute stack':       {'code': 'BR',  'category': 'stack', 'name': 'Brute Stack'},

    'aussie':            {'code': 'A',   'category': 'stack', 'name': 'Aussie Stack'},
    'aussie stack':      {'code': 'A',   'category': 'stack', 'name': 'Aussie Stack'},
    'aus stack':         {'code': 'A',   'category': 'stack', 'name': 'Aussie Stack'},

    'west coast':        {'code': 'WCK', 'category': 'stack', 'name': 'West Coast Curve Stack'},
    'west coast curve':  {'code': 'WCK', 'category': 'stack', 'name': 'West Coast Curve Stack'},
    'west coast stack':  {'code': 'WCK', 'category': 'stack', 'name': 'West Coast Curve Stack'},
    'wc stack':          {'code': 'WCK', 'category': 'stack', 'name': 'West Coast Curve Stack'},

    'straight stack':    {'code': 'SS',  'category': 'stack', 'name': 'Straight Stack'},

    'spool':             {'code': 'SP',  'category': 'stack', 'name': 'Spool Pipe'},
    'spool pipe':        {'code': 'SP',  'category': 'stack', 'name': 'Spool Pipe'},
    'mitre':             {'code': 'SP',  'category': 'stack', 'name': 'Mitre / Spool / Spring Plate'},
    'miter':             {'code': 'SP',  'category': 'stack', 'name': 'Miter cut stack'},
    'mitre cut':         {'code': 'SP',  'category': 'stack', 'name': 'Mitre / Spool / Spring Plate'},
    'miter cut':         {'code': 'SP',  'category': 'stack', 'name': 'Miter cut stack'},

    # 'dump stack' is intentionally NOT here — see AMBIGUOUS_FAMILY_WORDS
    # below. Reps say 'dump stack' for any of D/DTS/ED/OB/JDS/SW/UM/FEC and
    # we need them to narrow before resolving.

    # Pipe families
    'elbow':             {'code': 'L',   'category': 'elbow', 'name': 'Elbow'},
    'bend':              {'code': 'L',   'category': 'elbow', 'name': 'Elbow (bent pipe)'},
    'reducer':           {'code': 'R',   'category': 'reducer', 'name': 'Reducer'},
    'reducers':          {'code': 'R',   'category': 'reducer', 'name': 'Reducer'},
    'straight tube':     {'code': 'S',   'category': 'pipe', 'name': 'Straight tube'},
    'straight pipe':     {'code': 'S',   'category': 'pipe', 'name': 'Straight tube / pipe'},
    'coupler':           {'code': 'CP',  'category': 'pipe', 'name': 'Coupler'},
    'couplers':          {'code': 'CP',  'category': 'pipe', 'name': 'Coupler'},
    'turbo flare':       {'code': 'T',   'category': 'pipe', 'name': 'Turbo flare / tube fitting'},
    'turbo flares':      {'code': 'T',   'category': 'pipe', 'name': 'Turbo flare / tube fitting'},

    # Muffler families
    'muffler':           {'code': 'M',   'category': 'muffler', 'name': 'Muffler'},
    'chrome muffler':    {'code': 'CM',  'category': 'muffler', 'name': 'Chrome Muffler'},
    'type one':          {'code': 'M',   'category': 'muffler', 'name': 'Type 1 Muffler'},
    'type 1':            {'code': 'M',   'category': 'muffler', 'name': 'Type 1 Muffler'},
    'type-1':            {'code': 'M',   'category': 'muffler', 'name': 'Type 1 Muffler'},
    'type1':             {'code': 'M',   'category': 'muffler', 'name': 'Type 1 Muffler'},

    # Generic categories that don't pin a specific family — flagged ambiguous
    # below. We list them here so they're recognized as family-words but the
    # caller knows to ask for more.

    # Application kits
    'turbo pipe':        {'code': 'T',   'category': 'pipe', 'name': 'Turbo / tube fitting'},
    'pete retrofit':     {'code': 'PRK', 'category': 'kit', 'name': 'Peterbilt Retrofit Kit'},
    'pete retro':        {'code': 'PRK', 'category': 'kit', 'name': 'Peterbilt Retrofit Kit'},
    'peterbilt retrofit':{'code': 'PRK', 'category': 'kit', 'name': 'Peterbilt Retrofit Kit'},
    'complete kit':      {'code': 'CK',  'category': 'kit', 'name': 'Complete Kit (Peterbilt)'},

    # Accessories
    'pipe guard':        {'code': 'PG',  'category': 'accessory', 'name': 'Pipe Guard'},
    'muffler guard':     {'code': 'MG',  'category': 'accessory', 'name': 'Muffler Guard'},
    'heat shield':       {'code': 'UHS', 'category': 'accessory', 'name': 'Universal Heat Shield'},
    'aerodynamic shield':{'code': 'AHS', 'category': 'accessory', 'name': 'Aerodynamic Heat Shield'},
    'hanger':            {'code': 'H',   'category': 'accessory', 'name': 'Hanger'},
    'mounting bracket':  {'code': 'MB',  'category': 'accessory', 'name': 'Mounting Bracket'},
    'stainless mounting bracket': {'code': 'SMB', 'category': 'accessory', 'name': 'Stainless Mounting Bracket'},
    'stainless bracket': {'code': 'SMB', 'category': 'accessory', 'name': 'Stainless Mounting Bracket'},
    'gasket':            {'code': 'GASKET', 'category': 'accessory', 'name': 'Gasket'},

    # Flex hose
    'flex hose':         {'code': 'G',   'category': 'flex', 'name': 'Flex hose'},
    'galvanized flex':   {'code': 'G',   'category': 'flex', 'name': 'Galvanized flex hose'},
    'stainless flex':    {'code': 'SF',  'category': 'flex', 'name': 'Stainless flex hose'},
    'dss':               {'code': 'DSS', 'category': 'flex', 'name': 'Durable Stainless Steel flex hose'},
    'durable stainless steel': {'code': 'DSS', 'category': 'flex', 'name': 'Durable Stainless Steel flex hose'},
    'powerflow flex':    {'code': 'POWERFLOW', 'category': 'flex', 'name': 'PowerFlow flex hose'},

    # Clamps
    'ez seal':           {'code': 'EZ',  'category': 'clamp', 'name': 'EZ Seal clamp'},
    'ez seal clamp':     {'code': 'EZ',  'category': 'clamp', 'name': 'EZ Seal clamp'},
    'v-band':            {'code': 'VB',  'category': 'clamp', 'name': 'V-Band clamp'},
    'v band':            {'code': 'VB',  'category': 'clamp', 'name': 'V-Band clamp'},
    'vband':             {'code': 'VB',  'category': 'clamp', 'name': 'V-Band clamp'},
    'v-band clamp':      {'code': 'VB',  'category': 'clamp', 'name': 'V-Band clamp'},
    'clamp':             {'code': None,  'category': 'clamp', 'name': 'Clamp (family unspecified)'},

    # Tools (disregard for product analysis but recognize)
    'pipe expander':     {'code': 'EXPANDER', 'category': 'tool', 'name': 'Pipe expander tool'},
    'expander':          {'code': 'EXPANDER', 'category': 'tool', 'name': 'Pipe expander tool'},

    # ========================================================================
    # HIGH PRIORITY family additions (the SME vocabulary spec, 2026-05-11)
    # ========================================================================

    # DPU (Dual Pipe Universal kit) — 24 SKUs
    'dual pipe universal': {'code': 'DPU', 'category': 'kit',     'name': 'Dual Pipe Universal kit'},
    'universal dual pipe': {'code': 'DPU', 'category': 'kit',     'name': 'Dual Pipe Universal kit'},
    'dpu':                 {'code': 'DPU', 'category': 'kit',     'name': 'Dual Pipe Universal kit'},
    'dpu kit':             {'code': 'DPU', 'category': 'kit',     'name': 'Dual Pipe Universal kit'},

    # ED (End Dump) — 11 SKUs
    'end dump':            {'code': 'ED',  'category': 'stack',   'name': 'End Dump stack'},
    'end-dump':            {'code': 'ED',  'category': 'stack',   'name': 'End Dump stack'},
    'end dump stack':      {'code': 'ED',  'category': 'stack',   'name': 'End Dump stack'},

    # FB (Flat Bolt clamp) — 36 SKUs
    'flat bolt':           {'code': 'FB',  'category': 'clamp',   'name': 'Flat Bolt clamp'},
    'flat-bolt':           {'code': 'FB',  'category': 'clamp',   'name': 'Flat Bolt clamp'},
    'flatbolt':            {'code': 'FB',  'category': 'clamp',   'name': 'Flat Bolt clamp'},
    'flat bolt clamp':     {'code': 'FB',  'category': 'clamp',   'name': 'Flat Bolt clamp'},

    # P (Donaldson part) — bare 'don' intentionally excluded; ambiguous with name
    'donaldson':           {'code': 'P',   'category': 'pipe',    'name': 'Donaldson part'},
    'donaldson part':      {'code': 'P',   'category': 'pipe',    'name': 'Donaldson part'},
    'don part':            {'code': 'P',   'category': 'pipe',    'name': 'Donaldson part'},

    # PF (Preformed clamp) — 45 SKUs
    'preformed':           {'code': 'PF',  'category': 'clamp',   'name': 'Preformed clamp'},
    'pre-formed':          {'code': 'PF',  'category': 'clamp',   'name': 'Preformed clamp'},
    'pre formed':          {'code': 'PF',  'category': 'clamp',   'name': 'Preformed clamp'},
    'preform':             {'code': 'PF',  'category': 'clamp',   'name': 'Preformed clamp'},
    'preformed clamp':     {'code': 'PF',  'category': 'clamp',   'name': 'Preformed clamp'},

    # RB (Round Bolt / Saddle) — 35 SKUs
    'round bolt':          {'code': 'RB',  'category': 'clamp',   'name': 'Round Bolt clamp'},
    'round-bolt':          {'code': 'RB',  'category': 'clamp',   'name': 'Round Bolt clamp'},
    'roundbolt':           {'code': 'RB',  'category': 'clamp',   'name': 'Round Bolt clamp'},
    'saddle clamp':        {'code': 'RB',  'category': 'clamp',   'name': 'Saddle (Round Bolt) clamp'},
    'single saddle':       {'code': 'RB',  'category': 'clamp',   'name': 'Single Saddle (Round Bolt) clamp'},

    # SB (School Bus) — 839 SKUs; parser disambiguates SB at SKU start vs SB body code
    'school bus':          {'code': 'SB',  'category': 'pipe',    'name': 'School Bus part'},
    'schoolbus':           {'code': 'SB',  'category': 'pipe',    'name': 'School Bus part'},
    'school bus pipe':     {'code': 'SB',  'category': 'pipe',    'name': 'School Bus pipe'},
    'school bus stack':    {'code': 'SB',  'category': 'pipe',    'name': 'School Bus stack'},

    # WFC (World's Finest preformed clamp) — 16 SKUs
    "world's finest":      {'code': 'WFC', 'category': 'clamp',   'name': "World's Finest preformed clamp"},
    'worlds finest':       {'code': 'WFC', 'category': 'clamp',   'name': "World's Finest preformed clamp"},
    'wfc':                 {'code': 'WFC', 'category': 'clamp',   'name': "World's Finest preformed clamp"},
    "world's finest clamp":{'code': 'WFC', 'category': 'clamp',   'name': "World's Finest preformed clamp"},

    # Y (Y-pipe) — 31 SKUs (canonical 'y pipe' / 'wye')
    'y pipe':              {'code': 'Y',   'category': 'pipe',    'name': 'Y-pipe'},
    'y-pipe':              {'code': 'Y',   'category': 'pipe',    'name': 'Y-pipe'},
    'ypipe':               {'code': 'Y',   'category': 'pipe',    'name': 'Y-pipe'},
    'wye':                 {'code': 'Y',   'category': 'pipe',    'name': 'Y-pipe (wye)'},
    'wye pipe':            {'code': 'Y',   'category': 'pipe',    'name': 'Y-pipe (wye)'},

    # ========================================================================
    # MEDIUM PRIORITY family additions
    # ========================================================================

    # TR (Tube / cold rolled tube) — 12 SKUs
    'tube':                {'code': 'TR',  'category': 'pipe',    'name': 'Cold Rolled Tube'},
    'cold rolled tube':    {'code': 'TR',  'category': 'pipe',    'name': 'Cold Rolled Tube'},
    'cr tube':             {'code': 'TR',  'category': 'pipe',    'name': 'Cold Rolled Tube'},

    # HB (Diverter box) — 5 SKUs
    'diverter':            {'code': 'HB',  'category': 'accessory', 'name': 'Diverter Box'},
    'diverter box':        {'code': 'HB',  'category': 'accessory', 'name': 'Diverter Box'},
    'two position diverter': {'code': 'HB','category': 'accessory', 'name': '2-Position Diverter Box'},

    # AS (AccuSeal clamp) — 23 SKUs
    'accuseal':            {'code': 'AS',  'category': 'clamp',   'name': 'AccuSeal clamp'},
    'accu-seal':           {'code': 'AS',  'category': 'clamp',   'name': 'AccuSeal clamp'},
    'accu seal':           {'code': 'AS',  'category': 'clamp',   'name': 'AccuSeal clamp'},

    # QP (Quiet Performance insert) — 13 SKUs
    'quiet performance':   {'code': 'QP',  'category': 'muffler', 'name': 'Quiet Performance insert'},
    'qp insert':           {'code': 'QP',  'category': 'muffler', 'name': 'Quiet Performance insert'},
    'quiet performance insert': {'code': 'QP', 'category': 'muffler', 'name': 'Quiet Performance insert'},

    # HD (Heavy Duty / tail pipe / universal hanger) — 47 SKUs
    'hd hanger':           {'code': 'HD',  'category': 'accessory', 'name': 'HD Universal Hanger'},
    'tail pipe hanger':    {'code': 'HD',  'category': 'accessory', 'name': 'Tail Pipe Universal Hanger'},
    'universal hanger':    {'code': 'HD',  'category': 'accessory', 'name': 'Universal Hanger'},

    # WFF (Westfalia flex) — 19 SKUs
    'westfalia':           {'code': 'WFF', 'category': 'flex',    'name': 'Westfalia stainless flex'},
    'westfalia flex':      {'code': 'WFF', 'category': 'flex',    'name': 'Westfalia stainless flex'},
    'wff':                 {'code': 'WFF', 'category': 'flex',    'name': 'Westfalia stainless flex'},

    # TRF (Turbo repair flare) — 12 SKUs
    'turbo repair':        {'code': 'TRF', 'category': 'pipe',    'name': 'Turbo Repair Flare'},
    'turbo repair flare':  {'code': 'TRF', 'category': 'pipe',    'name': 'Turbo Repair Flare'},
    'trf':                 {'code': 'TRF', 'category': 'pipe',    'name': 'Turbo Repair Flare'},

    # TSL (Tapered elbow) — 7 SKUs
    'tapered elbow':       {'code': 'TSL', 'category': 'elbow',   'name': 'Tapered Elbow'},
    'tapered el':          {'code': 'TSL', 'category': 'elbow',   'name': 'Tapered Elbow'},
    'taper elbow':         {'code': 'TSL', 'category': 'elbow',   'name': 'Tapered Elbow'},

    # IM (Internal Baffle) — 22 SKUs
    'internal baffle':     {'code': 'IM',  'category': 'muffler', 'name': 'Internal Baffle insert'},
    'baffle':              {'code': 'IM',  'category': 'muffler', 'name': 'Internal Baffle insert'},
    'baffles':             {'code': 'IM',  'category': 'muffler', 'name': 'Internal Baffle insert'},

    # DC (Dodge Cummins kit) — 36 SKUs
    'dodge cummins':       {'code': 'DC',  'category': 'kit',     'name': 'Dodge Cummins kit'},
    'dodge kit':           {'code': 'DC',  'category': 'kit',     'name': 'Dodge Cummins kit'},
    'cummins kit':         {'code': 'DC',  'category': 'kit',     'name': 'Dodge Cummins kit'},

    # RE (Rubber Elbow) — 20 SKUs
    'rubber elbow':        {'code': 'RE',  'category': 'elbow',   'name': 'Rubber Elbow'},

    # OB (OD Bottom Dump stack) — 5 SKUs
    'od bottom dump':      {'code': 'OB',  'category': 'stack',   'name': 'OD Bottom Dump stack'},
    'od btm dump':         {'code': 'OB',  'category': 'stack',   'name': 'OD Bottom Dump stack'},

    # HW (Heat Wrap) — 2 SKUs
    'heat wrap':           {'code': 'HW',  'category': 'accessory', 'name': 'Heat Wrap'},
    'exhaust wrap':        {'code': 'HW',  'category': 'accessory', 'name': 'Exhaust Heat Wrap'},

    # CN (Connector) — 24 SKUs
    'connector':           {'code': 'CN',  'category': 'pipe',    'name': 'Connector'},
    'connectors':          {'code': 'CN',  'category': 'pipe',    'name': 'Connector'},

    # DTS (Dump Top Stack) — 8 SKUs
    'dump top':            {'code': 'DTS', 'category': 'stack',   'name': 'Dump Top Stack'},
    'dump top stack':      {'code': 'DTS', 'category': 'stack',   'name': 'Dump Top Stack'},
    'dts':                 {'code': 'DTS', 'category': 'stack',   'name': 'Dump Top Stack'},

    # HF (High Flow muffler) — 4 SKUs
    'high flow':           {'code': 'HF',  'category': 'muffler', 'name': 'High Flow muffler'},
    'high flow muffler':   {'code': 'HF',  'category': 'muffler', 'name': 'High Flow muffler'},
    'hf muffler':          {'code': 'HF',  'category': 'muffler', 'name': 'High Flow muffler'},

    # VK (Internal Dampner insert) — 11 SKUs
    'internal dampner':    {'code': 'VK',  'category': 'muffler', 'name': 'Internal Dampner insert'},
    'dampener insert':     {'code': 'VK',  'category': 'muffler', 'name': 'Internal Dampner insert'},
    'dampner insert':      {'code': 'VK',  'category': 'muffler', 'name': 'Internal Dampner insert'},

    # FK (Flex Pipe Kit) — 16 SKUs
    'flex pipe kit':       {'code': 'FK',  'category': 'kit',     'name': 'Flex Pipe Kit'},
    'flex kit':            {'code': 'FK',  'category': 'kit',     'name': 'Flex Pipe Kit'},

    # HS (Heat Sleeve) — 3 SKUs
    'heat sleeve':         {'code': 'HS',  'category': 'accessory', 'name': 'Heat Sleeve'},

    # HSK (Hood Stack Kit) — 3 SKUs
    'hood stack':          {'code': 'HSK', 'category': 'kit',     'name': 'Hood Stack Kit'},
    'hood stack kit':      {'code': 'HSK', 'category': 'kit',     'name': 'Hood Stack Kit'},

    # MMB (Powder Coat clamp) — 3 SKUs
    'powder coat clamp':   {'code': 'MMB', 'category': 'clamp',   'name': 'Powder Coat clamp'},

    # GH (Grab Handle) — 7 SKUs
    'grab handle':         {'code': 'GH',  'category': 'accessory', 'name': 'Grab Handle'},

    # SPU (Single Pipe Universal kit) — 12 SKUs
    'single pipe universal': {'code': 'SPU', 'category': 'kit',   'name': 'Single Pipe Universal kit'},
    'single stack kit':    {'code': 'SPU', 'category': 'kit',     'name': 'Single Pipe Universal (single stack) kit'},
    'spu':                 {'code': 'SPU', 'category': 'kit',     'name': 'Single Pipe Universal kit'},
    'spu kit':             {'code': 'SPU', 'category': 'kit',     'name': 'Single Pipe Universal kit'},

    # EKM (Multi / Universal Muffler) — 3 SKUs
    'multi muffler':       {'code': 'EKM', 'category': 'muffler', 'name': 'Multi / Universal Muffler'},
    'universal muffler':   {'code': 'EKM', 'category': 'muffler', 'name': 'Multi / Universal Muffler'},

    # AT (Air T-Bolt clamp) — 8 SKUs
    'air t-bolt':          {'code': 'AT',  'category': 'clamp',   'name': 'Air T-Bolt clamp'},
    't-bolt clamp':        {'code': 'AT',  'category': 'clamp',   'name': 'Air T-Bolt clamp'},
    'air tbolt':           {'code': 'AT',  'category': 'clamp',   'name': 'Air T-Bolt clamp'},

    # EB (Emission Bellow / SS bellows) — 8 SKUs
    'emission bellow':     {'code': 'EB',  'category': 'flex',    'name': 'Emission Bellows (SS)'},
    'bellows ss':          {'code': 'EB',  'category': 'flex',    'name': 'Emission Bellows (SS)'},

    # YC (Type C Y-pipe) — 8 SKUs
    'type c y':            {'code': 'YC',  'category': 'pipe',    'name': 'Type C Y-pipe'},
    'type c y pipe':       {'code': 'YC',  'category': 'pipe',    'name': 'Type C Y-pipe'},

    # QPM (Quiet Performance Muffler) — 25 SKUs
    'quiet performance muffler': {'code': 'QPM', 'category': 'muffler', 'name': 'Quiet Performance Muffler'},
    'qp muffler':          {'code': 'QPM', 'category': 'muffler', 'name': 'Quiet Performance Muffler'},
    'qpm':                 {'code': 'QPM', 'category': 'muffler', 'name': 'Quiet Performance Muffler'},

    # ACFM (Aerocab Frame bracket) — 4 SKUs
    'aerocab frame':       {'code': 'ACFM', 'category': 'accessory', 'name': 'Aerocab Frame bracket'},
    'acfm bracket':        {'code': 'ACFM', 'category': 'accessory', 'name': 'Aerocab Frame bracket'},

    # OS (Offset Stack Mount bracket) — 8 SKUs
    'offset stack mount':  {'code': 'OS',  'category': 'accessory', 'name': 'Offset Stack Mount bracket'},
    'offset stack bracket':{'code': 'OS',  'category': 'accessory', 'name': 'Offset Stack Mount bracket'},

    # RO (Round Open / saddle steel clamp) — 19 SKUs
    'round open':          {'code': 'RO',  'category': 'clamp',   'name': 'Round-Open Saddle clamp'},
    'round-open':          {'code': 'RO',  'category': 'clamp',   'name': 'Round-Open Saddle clamp'},
    'ro clamp':            {'code': 'RO',  'category': 'clamp',   'name': 'Round-Open Saddle clamp'},

    # DIM (KW Aerocab muffler kit) — 5 SKUs
    'kw aerocab kit':      {'code': 'DIM', 'category': 'kit',     'name': 'KW Aerocab Muffler Kit'},
    'aerocab muffler kit': {'code': 'DIM', 'category': 'kit',     'name': 'KW Aerocab Muffler Kit'},
    'dim kit':             {'code': 'DIM', 'category': 'kit',     'name': 'KW Aerocab Muffler Kit'},

    # PS (Ford Powerstroke kit) — 39 SKUs
    'powerstroke kit':     {'code': 'PS',  'category': 'kit',     'name': 'Ford Powerstroke kit'},
    'powerstroke':         {'code': 'PS',  'category': 'kit',     'name': 'Ford Powerstroke kit'},
    'ford powerstroke':    {'code': 'PS',  'category': 'kit',     'name': 'Ford Powerstroke kit'},
    'ps kit':              {'code': 'PS',  'category': 'kit',     'name': 'Ford Powerstroke kit'},

    # US (West Coast Cut stack) — 9 SKUs
    'west coast cut':      {'code': 'US',  'category': 'stack',   'name': 'West Coast Cut stack'},
    'westcoast cut':       {'code': 'US',  'category': 'stack',   'name': 'West Coast Cut stack'},
    'wcc stack':           {'code': 'US',  'category': 'stack',   'name': 'West Coast Cut stack'},

    # TODO(sme): UM/SW/JDS/FEC dump-stack variants — needs the SME input to
    # disambiguate which 'universal dump' / 'sweep' / 'JDS' / 'FEC' variant
    # reps mean. For now we ship UM only because it's the only one verified.
    # UM (Universal Dump stack) — 3 SKUs
    'universal dump':      {'code': 'UM',  'category': 'stack',   'name': 'Universal Dump stack'},
    'universal dump stack':{'code': 'UM',  'category': 'stack',   'name': 'Universal Dump stack'},

    # RMH (Round Muffler Hanger) — 9 SKUs
    'round muffler hanger':{'code': 'RMH', 'category': 'accessory', 'name': 'Round Muffler Hanger'},
    'rmh':                 {'code': 'RMH', 'category': 'accessory', 'name': 'Round Muffler Hanger'},

    # TODO(sme): AC has dual lines — Aerocab brackets AND emission
    # bellows/flex. the SME to confirm whether reps say "aerocab" for both or
    # only the bracket variant. Mapping both to AC for now; rep can narrow.
    # AC (Aerocab bracket / Emission bellows / Emission flex) — 20 SKUs
    'aero cab':            {'code': 'AC',  'category': 'accessory', 'name': 'Aerocab bracket'},
    'aerocab bracket':     {'code': 'AC',  'category': 'accessory', 'name': 'Aerocab bracket'},
    'emission bellows':    {'code': 'AC',  'category': 'flex',    'name': 'Emission Bellows (Aerocab line)'},
    'emission flex':       {'code': 'AC',  'category': 'flex',    'name': 'Emission Flex (Aerocab line)'},

    # ========================================================================
    # LOW PRIORITY family additions
    # ========================================================================

    # PH (Pipe Hanger) — 7 SKUs
    'pipe hanger':         {'code': 'PH',  'category': 'accessory', 'name': 'Pipe Hanger'},

    # HH (Hump Hose) — 11 SKUs
    'hump hose':           {'code': 'HH',  'category': 'flex',    'name': 'Hump Hose'},

    # TBE (Tilt Bell) — 3 SKUs
    'tilt bell':           {'code': 'TBE', 'category': 'pipe',    'name': 'Tilt Bell'},

    # GBS (Bellows Flex) — 4 SKUs
    'bellows flex':        {'code': 'GBS', 'category': 'flex',    'name': 'Bellows Flex'},

    # RHH (Rubber Reducer hose) — 4 SKUs
    'rubber reducer':      {'code': 'RHH', 'category': 'reducer', 'name': 'Rubber Reducer'},

    # RRE (Rubber Reducer Elbow) — 6 SKUs
    'rubber reducer elbow':{'code': 'RRE', 'category': 'elbow',   'name': 'Rubber Reducer Elbow'},

    # LH (Longhorn stack) — 2 SKUs
    'longhorn':            {'code': 'LH',  'category': 'stack',   'name': 'Longhorn stack'},
    'longhorn stack':      {'code': 'LH',  'category': 'stack',   'name': 'Longhorn stack'},

    # PGH (Pipe Grab Handle) — 3 SKUs
    'pipe grab handle':    {'code': 'PGH', 'category': 'accessory', 'name': 'Pipe Grab Handle'},

    # EBK (Bellows Kit) — 22 SKUs
    'bellows kit':         {'code': 'EBK', 'category': 'flex',    'name': 'Bellows Kit'},
    'bellow kit':          {'code': 'EBK', 'category': 'flex',    'name': 'Bellows Kit'},

    # RF (Relaxed Length flex) — 11 SKUs
    'relaxed length':      {'code': 'RF',  'category': 'flex',    'name': 'Relaxed Length flex'},
    'relaxed flex':        {'code': 'RF',  'category': 'flex',    'name': 'Relaxed Length flex'},

    # TODO(sme): RS, HDT, STC obsolete vocabulary — needs the SME input to
    # decide whether to expose these as aliases at all, or keep silent until
    # rep usage data shows demand.
}

# Family-words that don't pin a specific code on their own.
# When these appear without other family-disambiguating context, the entity
# extractor should trigger a `family_unspecified` ambiguity.
AMBIGUOUS_FAMILY_WORDS: dict[str, dict[str, Any]] = {
    'stack':   {
        'category': 'stack',
        'candidates': ['K', 'BH', 'BR', 'A', 'WCK', 'SS', 'SP', 'D'],
        'reason': "'stack' alone is generic; could be K (curved), BH (bullhorn), BR (brute), A (aussie), WCK (west coast), SS (straight), SP (spool), D (dump)",
    },
    'pipe':    {
        'category': 'pipe',
        'candidates': ['S', 'L', 'R', 'ZP', 'T'],
        'reason': "'pipe' alone is generic; could be S (straight), L (elbow), R (reducer), ZP (Z-series), T (turbo)",
    },
    # the SME vocabulary spec disambiguation rules (2026-05-11)
    'muffler hanger': {
        'category': 'accessory',
        'candidates': ['RMH', 'OMH', 'H'],
        'reason': "'muffler hanger' is generic; could be RMH (round muffler hanger), OMH (offset muffler hanger), or H (generic hanger)",
    },
    'bellows': {
        'category': 'flex',
        'candidates': ['EB', 'EBK'],
        'reason': "'bellows' alone could be EB (emission bellow / SS bellows) or EBK (bellows kit). Ask whether it's a kit or single bellow.",
    },
    'dump stack': {
        'category': 'stack',
        'candidates': ['D', 'DTS', 'ED', 'OB', 'JDS', 'SW', 'UM', 'FEC'],
        'reason': "'dump stack' is generic; could be D (dump), DTS (dump top), ED (end dump), OB (OD bottom dump), JDS/SW/FEC (variant lines), or UM (universal dump)",
    },
}


# Bare canonical family codes — recognized as family hints when they appear
# alone (uppercase, no surrounding letters/digits). Useful when a rep types
# a family code without the dimensional suffix.
BARE_FAMILY_CODES: dict[str, dict[str, Any]] = {
    'K':   {'code': 'K',   'category': 'stack',   'name': 'Curved-Top Stack'},
    'A':   {'code': 'A',   'category': 'stack',   'name': 'Aussie Stack'},
    'BR':  {'code': 'BR',  'category': 'stack',   'name': 'Brute Stack'},
    'BH':  {'code': 'BH',  'category': 'stack',   'name': 'Bullhorn Stack'},
    'WCK': {'code': 'WCK', 'category': 'stack',   'name': 'West Coast Curve Stack'},
    'SS':  {'code': 'SS',  'category': 'stack',   'name': 'Straight Stack'},
    'SP':  {'code': 'SP',  'category': 'stack',   'name': 'Spool / Mitre Pipe'},
    'D':   {'code': 'D',   'category': 'stack',   'name': 'Dump Stack'},
    'L':   {'code': 'L',   'category': 'elbow',   'name': 'Elbow'},
    'R':   {'code': 'R',   'category': 'reducer', 'name': 'Reducer'},
    'S':   {'code': 'S',   'category': 'pipe',    'name': 'Straight tube'},
    'T':   {'code': 'T',   'category': 'pipe',    'name': 'Turbo / tube fitting'},
    'P':   {'code': 'P',   'category': 'pipe',    'name': 'Pipe'},
    'Y':   {'code': 'Y',   'category': 'pipe',    'name': 'Y-pipe'},
    'M':   {'code': 'M',   'category': 'muffler', 'name': 'Muffler'},
    'CM':  {'code': 'CM',  'category': 'muffler', 'name': 'Chrome Muffler'},
    'G':   {'code': 'G',   'category': 'flex',    'name': 'Galvanized flex hose'},
    'SF':  {'code': 'SF',  'category': 'flex',    'name': 'Stainless flex hose'},
    'SBR': {'code': 'BR',  'category': 'stack',   'name': 'Brute Stack (reducing)', 'is_reducer': True},
    'SBH': {'code': 'BH',  'category': 'stack',   'name': 'Bullhorn Stack (reducing)', 'is_reducer': True},
    'SK':  {'code': 'K',   'category': 'stack',   'name': 'Curved Stack (reducing)', 'is_reducer': True},
    'SA':  {'code': 'A',   'category': 'stack',   'name': 'Aussie Stack (reducing)', 'is_reducer': True},
    'SWCK':{'code': 'WCK', 'category': 'stack',   'name': 'West Coast Curve Stack (reducing)', 'is_reducer': True},
    'SL':  {'code': 'L',   'category': 'elbow',   'name': 'Elbow (reducing)', 'is_reducer': True},
}


def normalize_family_word(text: str) -> dict[str, Any] | None:
    """Map natural-language family-word to family code or ambiguity record.

    Returns
    -------
    - ``{code, category, name}`` for unambiguous matches
    - ``{ambiguous: True, category, candidates, raw, reason}`` for generic
      words like 'stack' or 'pipe' that need narrowing
    - ``None`` if the input doesn't look like a family reference
    """
    if not text:
        return None
    cleaned = normalize_surface(text).lower().strip(' .,;:')
    if cleaned in FAMILY_WORD_ALIASES:
        return dict(FAMILY_WORD_ALIASES[cleaned])
    if cleaned in AMBIGUOUS_FAMILY_WORDS:
        rec = AMBIGUOUS_FAMILY_WORDS[cleaned]
        return {
            'ambiguous': True,
            'category': rec['category'],
            'candidates': list(rec['candidates']),
            'raw': cleaned,
            'reason': rec['reason'],
        }
    # Bare family codes: try uppercase form
    upper = cleaned.upper()
    if upper in BARE_FAMILY_CODES:
        return dict(BARE_FAMILY_CODES[upper])
    return None


# ============================================================================
# Section 5: Body-code normalization (SB / EX / XB)
# ============================================================================
# Body code captures the "fit" of the part: SB = OD-mating (slips over an
# existing pipe), EX = ID-mating (existing pipe slips into it). Reps say
# this many ways.


BODY_ALIASES: dict[str, dict[str, Any]] = {
    'sb':              {'code': 'SB', 'meaning': 'Straight Bottom (OD-fit)'},
    'straight bottom': {'code': 'SB', 'meaning': 'Straight Bottom (OD-fit)'},
    'od fit':          {'code': 'SB', 'meaning': 'Straight Bottom (OD-fit)'},
    'od mating':       {'code': 'SB', 'meaning': 'Straight Bottom (OD-fit)'},
    'od-fit':          {'code': 'SB', 'meaning': 'Straight Bottom (OD-fit)'},
    'od-mating':       {'code': 'SB', 'meaning': 'Straight Bottom (OD-fit)'},
    'slip over':       {'code': 'SB', 'meaning': 'Straight Bottom (OD-fit)'},

    'ex':              {'code': 'EX', 'meaning': 'Expanded bottom (ID-fit)'},
    'expanded':        {'code': 'EX', 'meaning': 'Expanded bottom (ID-fit)'},
    'expanded bottom': {'code': 'EX', 'meaning': 'Expanded bottom (ID-fit)'},
    'id fit':          {'code': 'EX', 'meaning': 'Expanded bottom (ID-fit)'},
    'id mating':       {'code': 'EX', 'meaning': 'Expanded bottom (ID-fit)'},
    'id-fit':          {'code': 'EX', 'meaning': 'Expanded bottom (ID-fit)'},
    'id-mating':       {'code': 'EX', 'meaning': 'Expanded bottom (ID-fit)'},
    'slip into':       {'code': 'EX', 'meaning': 'Expanded bottom (ID-fit)'},

    'xb':              {'code': 'XB', 'meaning': 'XB variant'},
}


def normalize_body(text: str) -> dict[str, Any] | None:
    """Map a body-code phrase to canonical SB / EX / XB."""
    if not text:
        return None
    cleaned = normalize_surface(text).lower().strip(' .,;:')
    if cleaned in BODY_ALIASES:
        return dict(BODY_ALIASES[cleaned])
    return None


# ============================================================================
# Section 6: Fit normalization (ID/OD inlet/outlet)
# ============================================================================
# Most common forms in the catalog: ID/ID (439), ID/OD (830), OD/ID (88),
# OD/OD (1273). Less common: I.D./O.D., id-od, etc.


FIT_RE = re.compile(
    r'\b(?P<inlet>ID|OD|I\.D\.|O\.D\.|id|od)'
    r'\s*[-/]\s*'
    r'(?P<outlet>ID|OD|I\.D\.|O\.D\.|id|od)\b',
)


def normalize_fit(text: str) -> dict[str, Any] | None:
    """Parse an inlet/outlet fit expression into ``{inlet, outlet}``.

    Examples
    --------
    >>> normalize_fit('ID/OD')
    {'inlet': 'ID', 'outlet': 'OD'}
    >>> normalize_fit('id-od')
    {'inlet': 'ID', 'outlet': 'OD'}
    >>> normalize_fit('I.D./O.D.')
    {'inlet': 'ID', 'outlet': 'OD'}
    """
    if not text:
        return None
    m = FIT_RE.search(text)
    if not m:
        return None
    def canon(s: str) -> str:
        return s.upper().replace('.', '')
    return {'inlet': canon(m.group('inlet')), 'outlet': canon(m.group('outlet'))}


# ============================================================================
# Section 7: OEM normalization
# ============================================================================
# Truck-make. Catalog uses both abbreviations (PB, KW) and full names
# (PETERBILT, KENWORTH). Reps mix both. We normalize to the abbreviation
# since that's what the SKU encodes.


OEM_ALIASES: dict[str, dict[str, Any]] = {
    # Peterbilt
    'pb':           {'code': 'PB', 'meaning': 'Peterbilt'},
    'pete':         {'code': 'PB', 'meaning': 'Peterbilt'},
    'peterbilt':    {'code': 'PB', 'meaning': 'Peterbilt'},

    # Kenworth
    'kw':           {'code': 'KW', 'meaning': 'Kenworth'},
    'kenworth':     {'code': 'KW', 'meaning': 'Kenworth'},

    # International / Navistar
    'ih':           {'code': 'IH', 'meaning': 'International / Navistar'},
    'international':{'code': 'IH', 'meaning': 'International / Navistar'},
    'navistar':     {'code': 'IH', 'meaning': 'International / Navistar'},

    # Freightliner
    'fl':           {'code': 'FL', 'meaning': 'Freightliner'},
    'freightliner': {'code': 'FL', 'meaning': 'Freightliner'},
    'freight':      {'code': 'FL', 'meaning': 'Freightliner'},

    # Mack
    'mk':           {'code': 'MK', 'meaning': 'Mack'},
    'mack':         {'code': 'MK', 'meaning': 'Mack'},

    # Volvo
    'vg':           {'code': 'VG', 'meaning': 'Volvo'},
    'volvo':        {'code': 'VG', 'meaning': 'Volvo'},

    # Western Star
    'ws':           {'code': 'WS', 'meaning': 'Western Star'},
    'western star': {'code': 'WS', 'meaning': 'Western Star'},
    'westernstar':  {'code': 'WS', 'meaning': 'Western Star'},

    # GM / Chevy
    'gm':           {'code': 'GM', 'meaning': 'General Motors / Chevy'},
    'gmc':          {'code': 'GM', 'meaning': 'General Motors / Chevy'},
    'chevy':        {'code': 'GM', 'meaning': 'General Motors / Chevy'},
    'chevrolet':    {'code': 'GM', 'meaning': 'General Motors / Chevy'},

    # Ford
    'ft':           {'code': 'FT', 'meaning': 'Ford Truck'},
    'ford':         {'code': 'FT', 'meaning': 'Ford Truck'},

    # Bluebird
    'bb':           {'code': 'BB', 'meaning': 'Bluebird (school bus)'},
    'bluebird':     {'code': 'BB', 'meaning': 'Bluebird (school bus)'},
    'blue bird':    {'code': 'BB', 'meaning': 'Bluebird (school bus)'},

    # Thomas
    'th':           {'code': 'TH', 'meaning': 'Thomas Built Buses'},
    'thomas':       {'code': 'TH', 'meaning': 'Thomas Built Buses'},
    'thomas built': {'code': 'TH', 'meaning': 'Thomas Built Buses'},
}


def normalize_oem(text: str) -> dict[str, Any] | None:
    """Map a truck-make word/phrase to canonical OEM code."""
    if not text:
        return None
    cleaned = normalize_surface(text).lower().strip(' .,;:')
    if cleaned in OEM_ALIASES:
        return dict(OEM_ALIASES[cleaned])
    return None


# ============================================================================
# Section 8: Truck-model recognition (opportunistic)
# ============================================================================
# Models aren't in SKUs but reps reference them ("for a 379"). We maintain a
# dictionary mapping known model identifiers to their make so the entity
# extractor can use models as a make-disambiguation signal even when the
# model itself can't be matched to a specific SKU.


TRUCK_MODELS: dict[str, str] = {
    # Peterbilt
    '359':   'PB',  '379':   'PB',  '389':   'PB',
    '567':   'PB',  '579':   'PB',  '589':   'PB',
    '337':   'PB',  '348':   'PB',  '386':   'PB',
    '388':   'PB',
    # Kenworth
    't660':  'KW',  't680':  'KW',  't800':  'KW',
    'w900':  'KW',  't880':  'KW',  't480':  'KW',
    'w990':  'KW',
    # Freightliner
    'cascadia': 'FL', 'coronado': 'FL', 'columbia': 'FL',
    'classic':  'FL', 'classicxl':'FL', 'classic xl':'FL',
    # International
    '9200':  'IH',  '9400':  'IH',  '9900':  'IH',
    'lonestar': 'IH', 'lone star': 'IH', 'prostar': 'IH',
    'lt':    'IH',  'rh':    'IH',
    # Mack
    'pinnacle':'MK', 'anthem':'MK', 'granite':'MK',
    'cv':    'MK',  'titan': 'MK',
    # Volvo
    'vnl':   'VG',  'vnr':   'VG',  'vah':   'VG',
    'vnx':   'VG',
    # Western Star
    '4900':  'WS',  '5700':  'WS',  '5700xe':'WS',
    '49x':   'WS',  '57x':   'WS',
    # Ford (mostly older)
    'l9000': 'FT',  'lts9000':'FT', 'l8000': 'FT',
    'lt9000':'FT', 'powerstroke': 'FT',
    # GM
    'topkick':'GM', 'kodiak': 'GM',
}


def normalize_truck_model(text: str) -> dict[str, Any] | None:
    """Recognize a truck model identifier and return its make.

    Returns ``{model, make_code, make_meaning}`` or None.
    Useful for narrowing OEM context when the rep mentions a model rather
    than a make.
    """
    if not text:
        return None
    cleaned = normalize_surface(text).lower().strip(' .,;:')
    if cleaned in TRUCK_MODELS:
        make_code = TRUCK_MODELS[cleaned]
        oem = OEM_ALIASES.get(make_code.lower(), {})
        return {
            'model': cleaned,
            'make_code': make_code,
            'make_meaning': oem.get('meaning', make_code),
        }
    return None


# ============================================================================
# Section 8b: SKU-fragment recognition
# ============================================================================
# When reps type partial SKUs like 'K5', 'L590', 'PG-VS', or 'K5-24SBC',
# we want to recognize the structural fragment without requiring the full
# canonical form. This sits between full-SKU parsing (handled by the
# part_number_parser module) and free-form attribute extraction.
#
# The strategy: try to match the input against known family-code prefixes
# from FAMILY_WORD_ALIASES, and decompose what follows.


# Family codes that can lead a SKU fragment. Built from the canonical codes
# in FAMILY_WORD_ALIASES plus a few that don't have natural-language aliases
# but appear in real SKUs (Z-series, OEM mirrors).
SKU_FAMILY_PREFIXES = sorted({
    rec['code'] for rec in FAMILY_WORD_ALIASES.values()
    if rec.get('code')
} | {
    # Additional family codes not always present as natural-language aliases
    'K', 'BH', 'BR', 'A', 'WCK', 'SS', 'SP', 'SK', 'D', 'CSP', 'BT', 'DTS', 'EXS',
    'L', 'R', 'S', 'ZP', 'ZM', 'M', 'CM', 'T', 'CP', 'CN', 'Y', 'SL',
    'PB', 'KW', 'IH', 'MK', 'VG', 'FL', 'WS', 'GM', 'PETE', 'FT', 'FTE',
    'PS', 'DC', 'PRK', 'KWK', 'PK', 'CK',
    'PG', 'MG', 'AHS', 'UHS', 'PF', 'H', 'HD', 'VB', 'RB', 'FB', 'RC', 'EBK',
    'ARG', 'ARP', 'G', 'SF', 'GR', 'GRE', 'GRDPFG', 'SB',
    'EZ', 'GRIEZ', 'STD',
    'MB', 'SMB',
    'DSS',
}, key=len, reverse=True)  # longest-first for greedy matching


# Recognizes a SKU fragment of the form {family}{diameter}[-{rest}].
# Examples: K5, K5-24, K5-24SBC, L590, PG-VS, MB-5KWS, ZP1234
SKU_FRAGMENT_RE = re.compile(
    r'^([A-Z]{1,5})'              # family prefix
    r'(\d+(?:\.\d+)?)?'           # optional diameter
    r'(-?[A-Z0-9.\-]*)?$',        # optional rest (dash + characters)
    re.IGNORECASE,
)


# Digit-prefixed family codes that need to be matched BEFORE the dimension
# regex (which would otherwise consume the leading digits as a number).
# Examples: 2K-48, 109-TIP1M, 50DD3101
DIGIT_PREFIXED_FAMILY_RE = re.compile(
    r'^(?P<family>2K|109|50)'
    r'(?P<rest>[-A-Z0-9.]*)',
    re.IGNORECASE,
)


def normalize_sku_fragment(text: str) -> dict[str, Any] | None:
    """Try to parse a SKU-fragment token like 'K5', 'K5-24', 'PG-VS', '2K-48'.

    Returns ``{family, diameter?, rest?}`` if the input plausibly looks like
    a partial a catalog SKU, or None otherwise.

    This is meant for tokens that survived earlier normalizer passes — i.e.,
    the input wasn't clearly a dimension, finish, family-word, etc., but
    might still be a structural fragment of a real SKU.
    """
    if not text:
        return None
    cleaned = normalize_surface(text).strip(' .,;:!?').upper()
    if not cleaned:
        return None

    # Must contain at least one digit OR a dash to be a "fragment". Plain
    # alpha tokens like 'long', 'need' should NOT match.
    if not (any(c.isdigit() for c in cleaned) or '-' in cleaned):
        return None

    # First check digit-prefixed family codes (2K, 109, 50)
    digit_match = DIGIT_PREFIXED_FAMILY_RE.match(cleaned)
    if digit_match:
        family = digit_match.group('family').upper()
        rest = digit_match.group('rest') or ''
        # Strip a leading dash for cleanliness
        if rest.startswith('-'):
            rest = rest[1:]
        result = {'family': family}
        if rest:
            result['rest'] = rest
        return result

    # Try to match a known family prefix (longest-first).
    for prefix in SKU_FAMILY_PREFIXES:
        if cleaned.startswith(prefix):
            # The rest must start with a digit, dash, or be empty
            rest = cleaned[len(prefix):]
            if not rest or rest[0].isdigit() or rest[0] == '-':
                # Try to extract a diameter
                diameter = None
                rest_after = rest
                m = re.match(r'^(\d+(?:\.\d+)?)(.*)$', rest)
                if m:
                    try:
                        diameter = float(m.group(1))
                    except ValueError:
                        diameter = None
                    rest_after = m.group(2)
                # Strip a leading dash from the trailing portion for cleanliness
                if rest_after.startswith('-'):
                    rest_after = rest_after[1:]
                result = {'family': prefix}
                if diameter is not None:
                    result['diameter'] = diameter
                if rest_after:
                    result['rest'] = rest_after
                return result
    return None


# ============================================================================
# Section 9: Whole-input normalizer
# ============================================================================
# Top-level function that applies every normalizer to an input string and
# returns a structured representation. Downstream entity extractor will use
# this to identify spec attributes.


@dataclass
class NormalizedToken:
    """A single token extracted and classified from input text."""
    raw: str
    kind: str  # 'dimension', 'finish', 'family', 'body', 'fit', 'oem',
               #  'truck_model', 'unknown'
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedInput:
    """Result of running normalize_input() on a free-form string."""
    raw: str
    surface_normalized: str
    tokens: list[NormalizedToken] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[NormalizedToken]:
        return [t for t in self.tokens if t.kind == kind]

    def has_ambiguity(self) -> bool:
        return any(t.data.get('ambiguous') for t in self.tokens)


# Compound-phrase aliases that need to be matched before single-word ones,
# because single-word matchers would otherwise consume their components.
# E.g., "curved stack" must be matched as one unit before "stack" matches
# as ambiguous.
COMPOUND_PHRASES = [
    # finish phrases
    'cold rolled', 'cold roll', '304 ss', '304ss', '304 stainless',
    '409 ss', '409ss', '409 stainless', 'stainless steel',
    'black series', 'matte black', 'black brushed chrome',
    'smooth raw', 'smooth joint', 'at plater',
    # family phrases
    'curved stack', 'curve stack', 'curved top stack', 'curve top stack',
    'k stack', 'bullhorn stack', 'brute stack', 'aussie stack', 'aus stack',
    'west coast curve', 'west coast stack', 'west coast', 'wc stack',
    'straight stack', 'spool pipe', 'dump stack', 'straight tube',
    'straight pipe', 'chrome muffler', 'turbo pipe',
    'turbo flare', 'turbo flares',
    'v band', 'v-band', 'v-band clamp',
    'type one', 'type 1', 'type-1',
    'pete retrofit', 'pete retro', 'peterbilt retrofit', 'complete kit',
    'pipe guard', 'muffler guard', 'heat shield', 'aerodynamic shield',
    'mounting bracket', 'stainless mounting bracket', 'stainless bracket',
    'flex hose', 'galvanized flex', 'stainless flex',
    'durable stainless steel', 'powerflow flex', 'ez seal', 'ez seal clamp',
    'pipe expander',
    # OEM phrases
    'western star', 'thomas built', 'blue bird', 'lone star',
    # body phrases
    'straight bottom', 'expanded bottom', 'od fit', 'od mating', 'od-fit',
    'od-mating', 'id fit', 'id mating', 'id-fit', 'id-mating',
    'slip over', 'slip into',
    # spelled-out numbers (multiword)
    'two and a half', 'three and a half', 'four and a half', 'five and a half',
    'three quarter', 'three quarters', 'one half',
    # ------------------------------------------------------------------
    # the SME vocabulary spec — high/medium/low priority family phrases
    # ------------------------------------------------------------------
    # HIGH
    'dual pipe universal', 'universal dual pipe', 'dpu kit',
    'end dump stack', 'end dump', 'end-dump',
    'flat bolt clamp', 'flat bolt', 'flat-bolt',
    'donaldson part', 'don part',
    'pre-formed', 'pre formed', 'preformed clamp',
    'round bolt', 'round-bolt', 'saddle clamp', 'single saddle',
    'school bus pipe', 'school bus stack', 'school bus',
    "world's finest clamp", "world's finest", 'worlds finest',
    'y pipe', 'y-pipe', 'wye pipe',
    # MEDIUM
    'cold rolled tube', 'cr tube',
    'diverter box', 'two position diverter',
    'accu-seal', 'accu seal',
    'quiet performance insert', 'quiet performance muffler', 'quiet performance', 'qp insert', 'qp muffler',
    'hd hanger', 'tail pipe hanger', 'universal hanger',
    'westfalia flex',
    'turbo repair flare', 'turbo repair',
    'tapered elbow', 'tapered el', 'taper elbow',
    'internal baffle',
    'dodge cummins', 'dodge kit', 'cummins kit',
    'rubber elbow',
    'od bottom dump', 'od btm dump',
    'heat wrap', 'exhaust wrap',
    'dump top stack', 'dump top',
    'high flow muffler', 'high flow', 'hf muffler',
    'internal dampner', 'dampener insert', 'dampner insert',
    'flex pipe kit', 'flex kit',
    'heat sleeve',
    'hood stack kit', 'hood stack',
    'powder coat clamp',
    'grab handle',
    'single pipe universal', 'single stack kit', 'spu kit',
    'multi muffler', 'universal muffler',
    'air t-bolt', 't-bolt clamp', 'air tbolt',
    'emission bellow', 'bellows ss', 'emission bellows', 'emission flex',
    'type c y pipe', 'type c y',
    'aerocab frame', 'acfm bracket', 'aero cab', 'aerocab bracket',
    'offset stack mount', 'offset stack bracket',
    'round open', 'round-open', 'ro clamp',
    'kw aerocab kit', 'aerocab muffler kit', 'dim kit',
    'powerstroke kit', 'ford powerstroke',
    'west coast cut', 'westcoast cut', 'wcc stack',
    'universal dump stack', 'universal dump',
    'round muffler hanger', 'muffler hanger',
    # LOW
    'pipe hanger', 'hump hose', 'tilt bell', 'bellows flex',
    'rubber reducer elbow', 'rubber reducer',
    'longhorn stack', 'longhorn',
    'pipe grab handle',
    'bellows kit', 'bellow kit',
    'relaxed length', 'relaxed flex',
]
# Sort by length descending so longer phrases match first
COMPOUND_PHRASES_SORTED = sorted(set(COMPOUND_PHRASES), key=len, reverse=True)


def normalize_input(text: str) -> NormalizedInput:
    """Top-level normalizer: tokenize a free-form string into typed tokens.

    Walks the text left-to-right, attempting compound-phrase matches first,
    then dimension matches, then single-word lookups. Tokens that don't
    match any normalizer are kept as ``kind='unknown'`` so the entity
    extractor can decide what to do with them.

    This is intentionally simple — no NLP, no embeddings. Real coverage
    comes from the alias dictionaries above, which were tuned against
    catalog description frequency. The entity extractor will layer
    contextual logic on top.
    """
    raw = text or ''
    surface = normalize_surface(raw)
    if not surface:
        return NormalizedInput(raw=raw, surface_normalized='')
    # Convert spoken-word forms (NATO phonetics, number words, fractions) into
    # compact alphanumeric tokens so the downstream pattern matchers can see
    # them as dimensions and SKU fragments.
    surface = _normalize_spoken_words(surface)

    tokens: list[NormalizedToken] = []
    # Lowercase view for matching, but preserve case mapping by tracking offsets
    cursor = 0
    surface_lower = surface.lower()

    while cursor < len(surface_lower):
        # Skip whitespace and common separators
        while cursor < len(surface_lower) and surface_lower[cursor] in ' \t,;':
            cursor += 1
        if cursor >= len(surface_lower):
            break

        matched = False

        # 1. Try compound phrases first (longest first)
        for phrase in COMPOUND_PHRASES_SORTED:
            phrase_len = len(phrase)
            window = surface_lower[cursor:cursor + phrase_len]
            if window == phrase:
                # Make sure it's a whole-token match (boundary on right)
                next_char = surface_lower[cursor + phrase_len:cursor + phrase_len + 1]
                if next_char == '' or not next_char.isalnum():
                    raw_text = surface[cursor:cursor + phrase_len]
                    token = _classify_phrase(raw_text)
                    if token:
                        tokens.append(token)
                        cursor += phrase_len
                        matched = True
                        break
        if matched:
            continue

        # 2. Try dimension match (compound or single)
        # First, peek for a digit-prefixed SKU-fragment (like '2K-48' or
        # '109-TIP1M'). These need to win over the dimension regex which
        # would otherwise consume the leading digits.
        word_peek = re.match(r'(\S+)', surface_lower[cursor:])
        if word_peek:
            peek_word = word_peek.group(1).strip(' .,;:!?')
            digit_prefix_match = DIGIT_PREFIXED_FAMILY_RE.match(peek_word.upper())
            if digit_prefix_match and (
                digit_prefix_match.group('rest') and
                # Only use digit-prefixed fragment recognition when there's
                # something after the family code (otherwise '50' on its own
                # is just a number)
                len(digit_prefix_match.group('rest')) > 0
            ):
                end = word_peek.end()
                raw_text = surface[cursor:cursor + end].strip(' .,;:!?')
                fragment = normalize_sku_fragment(raw_text)
                if fragment:
                    tokens.append(NormalizedToken(
                        raw=raw_text, kind='sku_fragment', data=fragment))
                    cursor += end
                    continue

        dim_match = COMPOUND_DIM_RE.match(surface_lower[cursor:])
        if dim_match:
            raw_text = surface[cursor:cursor + dim_match.end()]
            normalized = normalize_compound_dimension(raw_text)
            if normalized:
                tokens.append(NormalizedToken(
                    raw=raw_text, kind='compound_dimension', data=normalized))
                cursor += dim_match.end()
                continue

        # Single dimension: number + optional unit
        single_dim_match = re.match(
            r'([\d.]+)\s*("|in\b|inch(?:es)?\b|\'|ga\b|gauge\b|ft\b|feet\b|foot\b)?',
            surface_lower[cursor:],
        )
        if single_dim_match and single_dim_match.group(1):
            end = single_dim_match.end()
            raw_text = surface[cursor:cursor + end].rstrip()
            normalized = normalize_dimension(raw_text)
            if normalized:
                tokens.append(NormalizedToken(
                    raw=raw_text, kind='dimension', data=normalized))
                cursor += end
                continue

        # 3. Try fit pattern (ID/OD)
        fit_match = FIT_RE.match(surface_lower[cursor:])
        if fit_match:
            raw_text = surface[cursor:cursor + fit_match.end()]
            normalized = normalize_fit(raw_text)
            if normalized:
                tokens.append(NormalizedToken(
                    raw=raw_text, kind='fit', data=normalized))
                cursor += fit_match.end()
                continue

        # 4. Single-word match: take the next whitespace-delimited token
        word_match = re.match(r'(\S+)', surface_lower[cursor:])
        if not word_match:
            break
        word_len = word_match.end()
        raw_word = surface[cursor:cursor + word_len].strip(' .,;:!?')
        token = _classify_phrase(raw_word)
        if token:
            tokens.append(token)
        else:
            tokens.append(NormalizedToken(raw=raw_word, kind='unknown'))
        cursor += word_len

    return NormalizedInput(
        raw=raw,
        surface_normalized=surface,
        tokens=tokens,
    )


def _classify_phrase(raw: str) -> NormalizedToken | None:
    """Try each single-attribute normalizer in priority order. Returns the
    first match or None."""
    if not raw:
        return None
    cleaned = raw.lower().strip(' .,;:!?')

    # Order matters: more-specific first, more-generic last.
    # Truck model is most specific (numeric like '379' or compound like
    # 'cascadia'); finishes and OEMs are next; family-words last because
    # 'stack' is generic and we want to favor unambiguous interpretations
    # if they're available.

    truck = normalize_truck_model(cleaned)
    if truck:
        return NormalizedToken(raw=raw, kind='truck_model', data=truck)

    oem = normalize_oem(cleaned)
    if oem:
        return NormalizedToken(raw=raw, kind='oem', data=oem)

    finish = normalize_finish(cleaned)
    if finish:
        return NormalizedToken(raw=raw, kind='finish', data=finish)

    body = normalize_body(cleaned)
    if body:
        return NormalizedToken(raw=raw, kind='body', data=body)

    family = normalize_family_word(cleaned)
    if family:
        return NormalizedToken(raw=raw, kind='family', data=family)

    # Last resort: try as a SKU fragment. This catches tokens like 'K5',
    # 'L590', 'PG-VS', 'K5-24SBC' that weren't caught by any of the
    # natural-language matchers above.
    fragment = normalize_sku_fragment(cleaned)
    if fragment:
        return NormalizedToken(raw=raw, kind='sku_fragment', data=fragment)

    return None
