"""
SKU Translator — Entity Extractor
==================================

Takes a list of normalized tokens (from `normalizer.py`) and assembles a
structured spec describing the part the human is asking about.

The normalizer recognizes typed tokens. The extractor figures out what
each token *means in context*: which dimension is the diameter, which is
the length, whether a number is a customer-program code or a measurement,
whether truck-model context implies an OEM, etc.

Output is a `PartSpec` — a dataclass with all the spec attributes the SKU
constructor or fuzzy matcher will need, plus a list of unresolved
ambiguities for the disambiguator to handle.

Design principles
-----------------
1. **Family-driven role assignment.** Family code tells us how many
   dimensions to expect and what they mean. K (stack) wants 2 dimensions.
   L (elbow) wants 4. R (reducer) wants 2 with bilateral fit.
2. **Don't lose information.** Every token gets either consumed into a
   spec field or recorded as `unconsumed_tokens`. Nothing silently dropped.
3. **Defer to existing parser when input looks like a full SKU.** Why
   reimplement decoding? The extractor hands SKU-shaped tokens to
   `part_number_parser.parse()` and uses the result.
4. **Ambiguities are first-class output.** The extractor doesn't pick
   silently — it surfaces every ambiguity it found, with classification
   matching the taxonomy from the disambiguator design.

Usage
-----
::

    from sku_translator.extractor import extract_spec

    spec = extract_spec("5 inch chrome curved stack 24 inches OD/OD")
    # spec.family == 'K'
    # spec.diameter == 5.0
    # spec.length == 24.0
    # spec.finish == 'C'
    # spec.fit == {'inlet': 'OD', 'outlet': 'OD'}
    # spec.ambiguities == []
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sku_translator.normalizer import (
    normalize_input,
    NormalizedInput,
    NormalizedToken,
)


# ============================================================================
# Section 1: Spec data model
# ============================================================================

@dataclass
class Ambiguity:
    """Something the extractor couldn't resolve. The disambiguator picks
    these up later and either auto-resolves (if confidence threshold met)
    or asks the human."""
    type: str                   # ambiguity type from taxonomy
    candidates: list[Any]       # possible resolutions
    raw_input: str              # the substring that caused the ambiguity
    reason: str                 # human-readable explanation


@dataclass
class PartSpec:
    """Structured representation of what the human is asking for.

    Fields use None to mean "not specified" and are filled in by the
    extractor as it processes tokens. Downstream components use this
    spec to either construct a canonical SKU, search the catalog, or
    surface ambiguities.
    """
    # The literal SKU if the input was a recognized canonical part number
    canonical_sku: str | None = None

    # Core spec attributes
    family: str | None = None              # K, L, R, PG, etc.
    family_name: str | None = None         # human-readable family name
    diameter: float | None = None
    length: float | None = None
    angle: int | None = None               # for elbows
    leg1: float | None = None              # for elbows
    leg2: float | None = None              # for elbows

    # Material / finish
    finish: str | None = None              # canonical code (A, C, S3, etc.)
    finish_meaning: str | None = None
    body: str | None = None                # SB, EX, XB
    body_meaning: str | None = None

    # Fit (inlet/outlet ID/OD)
    fit_inlet: str | None = None           # 'ID' or 'OD'
    fit_outlet: str | None = None          # 'ID' or 'OD'

    # Bilateral dimensions (for reducers)
    inlet_diameter: float | None = None
    outlet_diameter: float | None = None

    # Vehicle context
    oem: str | None = None                 # PB, KW, etc.
    oem_meaning: str | None = None
    truck_model: str | None = None         # '379', 'cascadia', etc.

    # Customer / program context
    customer: str | None = None            # 'Apex Diesel', etc.
    customer_program: str | None = None    # '548', '128xxx', '888EX', etc.

    # Pack / quantity (for clamps)
    pack_size: int | None = None

    # Subfamily prefix (e.g. 'L590' for 5" 90-degree elbow). Computed for
    # families with subfamilies encoded in the SKU prefix, currently L only.
    subfamily_prefix: str | None = None

    # Unresolved questions for the disambiguator
    ambiguities: list[Ambiguity] = field(default_factory=list)

    # Tokens that didn't fit any spec field (preserved for debugging /
    # downstream re-interpretation)
    unconsumed_tokens: list[NormalizedToken] = field(default_factory=list)

    # The original input, for traceability
    raw_input: str = ''

    def is_complete_for_construction(self) -> bool:
        """Is this spec complete enough to construct a canonical SKU?

        Different families need different attribute sets. This method
        checks the minimum required fields per family.
        """
        if self.canonical_sku:
            return True  # already a SKU
        if not self.family:
            return False
        # Stack/pipe families need diameter, length, body, finish
        if self.family in ('K', 'BH', 'BR', 'A', 'WCK', 'SS', 'SP', 'D', 'S'):
            return all([
                self.diameter is not None,
                self.length is not None,
                self.body is not None,
                self.finish is not None,
            ])
        # Elbows need diameter, angle, both legs, finish
        if self.family == 'L':
            return all([
                self.diameter is not None,
                self.angle is not None,
                self.leg1 is not None,
                self.leg2 is not None,
                self.finish is not None,
            ])
        # Reducers need both diameters and fit
        if self.family == 'R':
            return all([
                self.inlet_diameter is not None,
                self.outlet_diameter is not None,
                self.fit_inlet is not None,
                self.fit_outlet is not None,
            ])
        # Guards / accessories: depends on subgrammar; conservative for now
        return False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation for logging or transport."""
        result = {}
        for key, value in self.__dict__.items():
            if key == 'ambiguities':
                result[key] = [
                    {'type': a.type, 'candidates': a.candidates,
                     'raw_input': a.raw_input, 'reason': a.reason}
                    for a in value
                ]
            elif key == 'unconsumed_tokens':
                result[key] = [
                    {'kind': t.kind, 'raw': t.raw, 'data': t.data}
                    for t in value
                ]
            elif value is not None:
                result[key] = value
        return result


# ============================================================================
# Section 2: Family-driven role assignment
# ============================================================================
# Each family expects a specific set of dimensional fields. When we have
# multiple dimension tokens, this table tells us how to assign them.

# Number of "primary" dimensions a family expects, in priority order.
# 'diameter' is always first; 'length' second for stacks/pipes; 'angle' and
# 'legs' for elbows; bilateral diameters for reducers.
FAMILY_DIMENSION_ORDER: dict[str, list[str]] = {
    # Stacks: diameter then length
    'K':   ['diameter', 'length'],
    'BH':  ['diameter', 'length'],
    'BR':  ['diameter', 'length'],
    'A':   ['diameter', 'length'],
    'WCK': ['diameter', 'length'],
    'SS':  ['diameter', 'length'],
    'SP':  ['diameter', 'length'],
    'D':   ['diameter', 'length'],
    'CSP': ['diameter', 'length'],
    'BT':  ['diameter', 'length'],
    'DTS': ['diameter', 'length'],
    'EXS': ['diameter', 'length'],
    'SK':  ['diameter', 'length'],

    # Pipes: diameter then length (or just diameter for some)
    'S':   ['diameter', 'length'],
    'ZP':  ['diameter', 'length'],
    'T':   ['diameter', 'length'],

    # Elbows: diameter, angle, leg1, leg2
    'L':   ['diameter', 'angle', 'leg1', 'leg2'],

    # Reducers: bilateral
    'R':   ['inlet_diameter', 'outlet_diameter'],

    # Mufflers: diameter and length
    'M':   ['diameter', 'length'],
    'ZM':  ['diameter', 'length'],
    'CM':  ['length'],  # CM is unique — leading dimension is length, not diameter

    # Application kits: usually just diameter
    'PRK': ['diameter'],
    'CK':  ['diameter'],
    'KWK': ['diameter'],

    # Guards: usually just slot pattern (no dims in SKU itself)
    'PG':  [],
    'MG':  [],
    'AHS': [],
    'UHS': [],

    # Hangers: handled separately (compound seq/finish)
    'H':   [],

    # Clamps: diameter
    'EZ':  ['diameter'],
    'GRIEZ': ['diameter'],
    'STD': ['diameter'],

    # Brackets: handled by description
    'MB':  [],
    'SMB': [],

    # Flex hose: diameter and length
    'G':   ['diameter', 'length'],
    'SF':  ['diameter', 'length'],
    'DSS': ['diameter', 'length'],
}


# Customer-program prefixes that come from numeric tokens. When one of
# these numbers shows up alongside customer-program context (Performance
# Diesel, etc.), it's NOT a dimension.
CUSTOMER_PROGRAM_NUMBERS: dict[str, dict[str, Any]] = {
    548:   {'customer': 'Apex Diesel', 'family': '548',
            'program': '548 series (charger/dump pipes)'},
    777:   {'customer': 'Apex Diesel', 'family': '777',
            'program': '777 series (turbo pipe)'},
    888:   {'customer': 'Apex Diesel', 'family': '888',
            'program': '888 series (mid pipe)'},
}


# Customer-program suffix codes (for the 548 family specifically)
PERF_DIESEL_548_SUFFIX = {
    'charger pipe lower': 'CPL',
    'charger lower':      'CPL',
    'cpl':                'CPL',
    'charger pipe upper': 'CPU',
    'charger upper':      'CPU',
    'cpu':                'CPU',
    'dump pipe':          'DP',
    'dp':                 'DP',
}


# ============================================================================
# Section 3: SKU-fragment hand-off
# ============================================================================
# When the normalizer found a SKU-like token, we hand it to the existing
# part_number_parser to do full structural decoding rather than reimplementing.

def _parse_with_existing_parser(sku_text: str) -> dict[str, Any] | None:
    """Try to parse a token as a full canonical SKU using the existing parser.

    Returns the parser's enriched dict if classification != 'unstructured'
    (real pattern matched), or None otherwise.

    Imported lazily to avoid circular dependencies and to make the SKU
    translator usable independent of the parser if needed.
    """
    try:
        # Lazy import — package-qualified first; flat-module fallback kept for
        # script contexts. BUG FIX 2026-06-06: the original code imported only
        # the top-level name 'part_number_parser', which resolves solely when
        # cwd contains the module file. In any other deployment layout the
        # ImportError was swallowed and the canonical-SKU passthrough silently
        # disabled — a silent-degradation failure caught by the import-time
        # round-trip selftest during the src-layout migration.
        # See docs/MIGRATION_NOTES.md.
        from importlib import import_module
        try:
            parser = import_module('sku_translator.part_number_parser')
        except ImportError:
            parser = import_module('part_number_parser')
    except ImportError:
        return None
    try:
        result = parser.parse(sku_text)
    except Exception:
        return None
    pattern = result.get('pattern')
    # Only trust the parser's result if it found a real structural pattern.
    # 'unstructured', 'legacy_undocumented', 'freetext_or_admin' don't help.
    if pattern in ('unstructured', 'empty', 'legacy_undocumented',
                   'freetext_or_admin', 'family_numeric'):
        return None
    return result


def _spec_from_parser_result(parser_result: dict[str, Any], raw_input: str) -> PartSpec:
    """Convert a part_number_parser result into a PartSpec."""
    spec = PartSpec(raw_input=raw_input)
    spec.canonical_sku = parser_result.get('part_number')
    spec.family = parser_result.get('family')
    spec.family_name = parser_result.get('family_meaning')

    # Diameter, length, angle from parser fields (varies by pattern)
    if 'diameter' in parser_result:
        try:
            spec.diameter = float(parser_result['diameter'])
        except (ValueError, TypeError):
            pass
    if 'length' in parser_result:
        try:
            spec.length = float(parser_result['length'])
        except (ValueError, TypeError):
            pass
    if 'angle' in parser_result:
        try:
            spec.angle = int(parser_result['angle'])
        except (ValueError, TypeError):
            pass
    if 'leg1' in parser_result:
        try:
            spec.leg1 = float(parser_result['leg1'])
        except (ValueError, TypeError):
            pass
    if 'leg2' in parser_result:
        try:
            spec.leg2 = float(parser_result['leg2'])
        except (ValueError, TypeError):
            pass

    # Finish/body
    spec.finish = parser_result.get('finish')
    spec.finish_meaning = parser_result.get('finish_meaning')
    spec.body = parser_result.get('body')
    spec.body_meaning = parser_result.get('body_meaning')

    # OEM
    spec.oem = parser_result.get('oem')
    spec.oem_meaning = parser_result.get('oem_meaning')

    # Customer
    if parser_result.get('proprietary_customer'):
        spec.customer = parser_result['proprietary_customer']

    return spec


# ============================================================================
# Section 4: Token consumption helpers
# ============================================================================

def _is_dimension_inch(token: NormalizedToken) -> bool:
    """A dimension token whose unit is explicitly inches or unspecified."""
    if token.kind != 'dimension':
        return False
    unit = token.data.get('unit')
    return unit == 'inch' or unit is None


def _is_pure_number(token: NormalizedToken) -> bool:
    """A dimension token without a unit (just a number, like '90' or '548')."""
    if token.kind != 'dimension':
        return False
    return token.data.get('unit') is None


def _looks_like_angle(value: float) -> bool:
    """Is this value plausibly an angle (15-180)?"""
    return value in (15, 30, 45, 60, 70, 75, 90, 180)


def _looks_like_diameter(value: float) -> bool:
    """Is this value plausibly a pipe diameter (1-12 inches)?"""
    return 1.0 <= value <= 12.0


def _looks_like_length(value: float) -> bool:
    """Is this value plausibly a length (>5 typically, often 18-150)?"""
    return value >= 5.0


# ============================================================================
# Section 5: Main extractor
# ============================================================================

def extract_spec(text: str | None) -> PartSpec:
    """Extract a structured PartSpec from free-form human text.

    Pipeline:
      1. Normalize input into typed tokens (delegate to normalizer)
      2. If the input is a single recognized SKU, use the existing parser
      3. Otherwise, walk the tokens and assign each to spec fields based
         on family context and dimensional priorities
      4. Record any ambiguities the disambiguator should resolve
    """
    if text is None or not str(text).strip():
        return PartSpec(raw_input='')

    raw_input = str(text)
    normalized = normalize_input(raw_input)

    # Step 1: If the whole input is one SKU-fragment token, try the existing parser
    spec = _try_full_sku_passthrough(normalized, raw_input)
    if spec is not None:
        return spec

    # Step 2: Walk tokens and build a spec
    return _build_spec_from_tokens(normalized, raw_input)


def _try_full_sku_passthrough(
    normalized: NormalizedInput,
    raw_input: str,
) -> PartSpec | None:
    """If the input is a single recognized SKU, return a spec from the parser.

    Triggers when:
      - The input is a single token (no spaces) AND the parser recognizes it
      - OR the only significant normalizer token is an sku_fragment AND the
        parser recognizes the original input
    """
    # Quick path: input has no whitespace and is a recognizable SKU
    stripped = raw_input.strip()
    if stripped and ' ' not in stripped:
        parser_result = _parse_with_existing_parser(stripped)
        if parser_result is not None:
            return _spec_from_parser_result(parser_result, raw_input)

    # Slow path: filter to non-noise tokens, look for single-fragment case
    significant = [t for t in normalized.tokens if t.kind != 'unknown']
    if len(significant) != 1:
        return None
    token = significant[0]
    if token.kind != 'sku_fragment':
        return None
    parser_result = _parse_with_existing_parser(token.raw)
    if parser_result is None:
        return None
    return _spec_from_parser_result(parser_result, raw_input)


def _build_spec_from_tokens(
    normalized: NormalizedInput,
    raw_input: str,
) -> PartSpec:
    """Walk normalized tokens and assemble a PartSpec.

    Strategy:
      Pass 1: collect everything unambiguous (family, finish, body, fit, oem,
              truck_model). Detect customer-program context.
      Pass 2: assign dimension tokens to roles based on family expectations.
      Pass 3: record ambiguities for the disambiguator.
    """
    spec = PartSpec(raw_input=raw_input)

    # Track which tokens we've consumed so we can report leftovers
    consumed: set[int] = set()

    # ------------------------------------------------------------------
    # Pass 1: unambiguous attribute extraction
    # ------------------------------------------------------------------

    # First: any SKU fragments? They override family-words because they're
    # more specific. If a fragment carries diameter, that wins too.
    fragment_seen = False
    for i, t in enumerate(normalized.tokens):
        if t.kind != 'sku_fragment':
            continue
        consumed.add(i)
        fragment_seen = True
        if spec.family is None:
            spec.family = t.data['family']
        if spec.diameter is None and 'diameter' in t.data:
            spec.diameter = t.data['diameter']
        # If the fragment has a 'rest', try to decode it via the existing parser
        rest = t.data.get('rest', '')
        if rest:
            # Reconstruct the full SKU and let the parser take it
            full_sku = t.raw
            parser_result = _parse_with_existing_parser(full_sku)
            if parser_result and parser_result.get('pattern') not in (
                'unstructured', 'family_numeric',
            ):
                # The parser decoded it — pull what we can into spec
                pr_spec = _spec_from_parser_result(parser_result, raw_input)
                # Merge non-None fields
                _merge_spec(spec, pr_spec)

    # Family from unambiguous family-word (if no fragment-derived family)
    for i, t in enumerate(normalized.tokens):
        if t.kind != 'family' or t.data.get('ambiguous'):
            continue
        if spec.family is None:
            spec.family = t.data['code']
            spec.family_name = t.data.get('name')
            consumed.add(i)
        else:
            # Already have a family — this token is redundant or conflicting
            existing = spec.family
            new_code = t.data['code']
            if existing == new_code:
                consumed.add(i)  # exact duplicate, drop silently
            else:
                # Conflict: record ambiguity but keep first-seen
                spec.ambiguities.append(Ambiguity(
                    type='family_conflict',
                    candidates=[existing, new_code],
                    raw_input=t.raw,
                    reason=f"Multiple family codes mentioned: {existing} (kept) vs {new_code}",
                ))
                consumed.add(i)

    # Finish (unambiguous)
    for i, t in enumerate(normalized.tokens):
        if t.kind != 'finish' or t.data.get('ambiguous'):
            continue
        if spec.finish is None:
            spec.finish = t.data['code']
            spec.finish_meaning = t.data.get('meaning')
            consumed.add(i)
        else:
            existing = spec.finish
            new_code = t.data['code']
            if existing == new_code:
                consumed.add(i)
            else:
                spec.ambiguities.append(Ambiguity(
                    type='finish_conflict',
                    candidates=[existing, new_code],
                    raw_input=t.raw,
                    reason=f"Multiple finishes mentioned: {existing} (kept) vs {new_code}",
                ))
                consumed.add(i)

    # Body
    for i, t in enumerate(normalized.tokens):
        if t.kind != 'body':
            continue
        if spec.body is None:
            spec.body = t.data['code']
            spec.body_meaning = t.data.get('meaning')
            consumed.add(i)
        else:
            consumed.add(i)  # duplicates dropped silently

    # Fit
    for i, t in enumerate(normalized.tokens):
        if t.kind != 'fit':
            continue
        if spec.fit_inlet is None:
            spec.fit_inlet = t.data['inlet']
            spec.fit_outlet = t.data['outlet']
            consumed.add(i)
        else:
            consumed.add(i)

    # OEM (with deduplication: 'Peterbilt' and 'Pete' both → PB)
    for i, t in enumerate(normalized.tokens):
        if t.kind != 'oem':
            continue
        if spec.oem is None:
            spec.oem = t.data['code']
            spec.oem_meaning = t.data.get('meaning')
            consumed.add(i)
        elif spec.oem == t.data['code']:
            consumed.add(i)  # duplicate, fine
        else:
            spec.ambiguities.append(Ambiguity(
                type='oem_conflict',
                candidates=[spec.oem, t.data['code']],
                raw_input=t.raw,
                reason=f"Multiple OEMs mentioned: {spec.oem} (kept) vs {t.data['code']}",
            ))
            consumed.add(i)

    # Truck model — implies make if make wasn't already set
    for i, t in enumerate(normalized.tokens):
        if t.kind != 'truck_model':
            continue
        spec.truck_model = t.data['model']
        if spec.oem is None:
            spec.oem = t.data['make_code']
            spec.oem_meaning = t.data.get('make_meaning')
        elif spec.oem != t.data['make_code']:
            spec.ambiguities.append(Ambiguity(
                type='oem_conflict',
                candidates=[spec.oem, t.data['make_code']],
                raw_input=t.raw,
                reason=f"Truck model {t.data['model']} is a {t.data['make_code']} but input also says {spec.oem}",
            ))
        consumed.add(i)

    # ------------------------------------------------------------------
    # Pass 1.5: customer-program detection (numbers as program codes)
    # ------------------------------------------------------------------
    # If a pure-number dimension token matches a known customer-program code,
    # AND we don't already have a strong family signal, treat it as a program.
    for i, t in enumerate(normalized.tokens):
        if i in consumed:
            continue
        if t.kind != 'dimension' or not _is_pure_number(t):
            continue
        value = t.data['value']
        if value not in CUSTOMER_PROGRAM_NUMBERS:
            continue
        # Check the rest of the input for confirming customer name
        program_info = CUSTOMER_PROGRAM_NUMBERS[int(value)]
        text_upper = raw_input.upper()
        # Only adopt if customer name appears or if there's no competing family
        confirmed_by_name = (
            program_info['customer'].upper() in text_upper
            or 'PERF' in text_upper
        )
        if confirmed_by_name and spec.family is None:
            spec.family = program_info['family']
            spec.family_name = program_info['program']
            spec.customer = program_info['customer']
            spec.customer_program = program_info['family']
            consumed.add(i)
            # Try to find a matching suffix code in the raw input
            for phrase, code in PERF_DIESEL_548_SUFFIX.items():
                if phrase in raw_input.lower():
                    spec.canonical_sku = f"{program_info['family']}{code}"
                    break

    # ------------------------------------------------------------------
    # Pass 1.6: numeric tokens as truck models
    # ------------------------------------------------------------------
    # Some pure-number dimension tokens are actually truck model identifiers
    # (e.g., '379' for Peterbilt). The normalizer can't distinguish without
    # context, but we can: if the value is in our truck-model dictionary AND
    # the implied make is consistent with any OEM already in the spec (or no
    # OEM is set), treat the number as a truck model rather than a dimension.
    from sku_translator.normalizer import TRUCK_MODELS
    for i, t in enumerate(normalized.tokens):
        if i in consumed:
            continue
        if t.kind != 'dimension' or not _is_pure_number(t):
            continue
        value = t.data['value']
        # Truck-model keys are strings (sometimes alpha like 'cascadia', sometimes numeric)
        # For numeric models, check if the integer form matches
        if value != int(value):
            continue
        model_key = str(int(value))
        if model_key not in TRUCK_MODELS:
            continue
        implied_make = TRUCK_MODELS[model_key]
        # Reclassify only if it's consistent with existing OEM context
        # (or no OEM is set yet)
        if spec.oem is None or spec.oem == implied_make:
            spec.truck_model = model_key
            if spec.oem is None:
                from sku_translator.normalizer import OEM_ALIASES
                oem_info = OEM_ALIASES.get(implied_make.lower(), {})
                spec.oem = implied_make
                spec.oem_meaning = oem_info.get('meaning', implied_make)
            consumed.add(i)

    # ------------------------------------------------------------------
    # Pass 2: dimension role assignment
    # ------------------------------------------------------------------
    dim_tokens = [
        (i, t) for i, t in enumerate(normalized.tokens)
        if i not in consumed and t.kind == 'dimension'
    ]

    # Special handling per family. If we don't know the family, fall back
    # to heuristics by value range.
    if spec.family and spec.family in FAMILY_DIMENSION_ORDER:
        roles = FAMILY_DIMENSION_ORDER[spec.family]
        _assign_dimensions_by_family(spec, dim_tokens, roles, consumed)
    else:
        _assign_dimensions_heuristic(spec, dim_tokens, consumed)

    # ------------------------------------------------------------------
    # Pass 3: ambiguity collection
    # ------------------------------------------------------------------

    # Ambiguous family
    for i, t in enumerate(normalized.tokens):
        if i in consumed:
            continue
        if t.kind == 'family' and t.data.get('ambiguous'):
            if spec.family is None:
                # Family is ambiguous AND we couldn't resolve from elsewhere
                spec.ambiguities.append(Ambiguity(
                    type='family_unspecified',
                    candidates=t.data['candidates'],
                    raw_input=t.raw,
                    reason=t.data.get('reason', 'Family code unclear'),
                ))
            consumed.add(i)
        elif t.kind == 'finish' and t.data.get('ambiguous'):
            if spec.finish is None:
                spec.ambiguities.append(Ambiguity(
                    type='finish_synonym_disambiguation',
                    candidates=t.data['candidates'],
                    raw_input=t.raw,
                    reason=t.data.get('reason', 'Finish code unclear'),
                ))
            consumed.add(i)

    # ------------------------------------------------------------------
    # Pass 4: completeness checks → spec-level ambiguities
    # ------------------------------------------------------------------
    _check_spec_completeness(spec)

    # ------------------------------------------------------------------
    # Final: collect unconsumed tokens for traceability
    # ------------------------------------------------------------------
    spec.unconsumed_tokens = [
        t for i, t in enumerate(normalized.tokens)
        if i not in consumed and t.kind != 'unknown'
    ]
    return spec


def _merge_spec(into: PartSpec, from_spec: PartSpec) -> None:
    """Merge non-None fields from `from_spec` into `into` (in-place)."""
    for field_name in ('canonical_sku', 'family', 'family_name', 'diameter',
                       'length', 'angle', 'leg1', 'leg2', 'finish',
                       'finish_meaning', 'body', 'body_meaning', 'fit_inlet',
                       'fit_outlet', 'inlet_diameter', 'outlet_diameter',
                       'oem', 'oem_meaning', 'truck_model', 'customer',
                       'customer_program', 'subfamily_prefix'):
        existing = getattr(into, field_name)
        new_value = getattr(from_spec, field_name)
        if existing is None and new_value is not None:
            setattr(into, field_name, new_value)


def _assign_dimensions_by_family(
    spec: PartSpec,
    dim_tokens: list[tuple[int, NormalizedToken]],
    roles: list[str],
    consumed: set,
) -> None:
    """Assign dimension tokens to spec fields, guided by family expectations.

    Strategy: each dimension token gets scored against each open role, and
    we greedily assign best matches first. This handles out-of-order tokens
    like '24 inches 5 inch' (length-then-diameter).
    """
    if not dim_tokens or not roles:
        return

    # Drop diameter from roles if already set (e.g., from a SKU fragment)
    open_roles = [r for r in roles if getattr(spec, r, None) is None]

    # Special handling for elbows
    if spec.family == 'L':
        _assign_elbow_dimensions(spec, dim_tokens, consumed)
        return

    def role_fit_score(role: str, value: float) -> int:
        """Higher = better fit. -1 = doesn't fit at all (skip)."""
        if role == 'diameter':
            return 10 if _looks_like_diameter(value) else -1
        if role == 'length':
            # Length must be at least 1 (sanity); typical is 18-150
            if value < 1:
                return -1
            # Prefer values clearly above the diameter range
            return 10 if value > 12 else 5
        if role == 'angle':
            return 10 if _looks_like_angle(value) else -1
        if role in ('leg1', 'leg2', 'inlet_diameter', 'outlet_diameter'):
            return 5  # accept any positive value
        return 0

    # Build a priority table: for each (role, token), what's the score?
    candidates = []  # (score, role_idx, token_idx, value)
    for role_idx, role in enumerate(open_roles):
        for tok_idx, (i, t) in enumerate(dim_tokens):
            value = t.data['value']
            score = role_fit_score(role, value)
            if score >= 0:
                candidates.append((score, role_idx, tok_idx, i, role, value))

    # Greedy assignment: sort by score descending, assign each role to the
    # best-fit unconsumed token, in role order (so diameter assigns before
    # length).
    used_token_indices = set()
    used_roles = set()
    # Sort by role priority (role_idx ascending) then by score (desc) so
    # earlier-listed roles fill first, but with a strong fit-fit signal.
    candidates.sort(key=lambda c: (c[1], -c[0]))
    for score, role_idx, tok_idx, i, role, value in candidates:
        if role in used_roles:
            continue
        if tok_idx in used_token_indices:
            continue
        setattr(spec, role, value)
        used_roles.add(role)
        used_token_indices.add(tok_idx)
        consumed.add(i)


# Elbow subfamily prefix is `L{diameter}{angle}` where angle equals the
# integer degree value. Derived from the fixture catalog: every L-family
# row's SKU prefix consists of L + literal diameter + literal angle.
# Mapping limited to angles that actually appear in the catalog so unknown
# angles surface as ambiguities rather than producing zero-match buckets.
ELBOW_ANGLE_TO_PREFIX: dict[int, str] = {
    15: '15',
    20: '20',
    25: '25',
    30: '30',
    35: '35',
    40: '40',
    45: '45',
    50: '50',
    55: '55',
    60: '60',
    65: '65',
    70: '70',
    75: '75',
    80: '80',
    85: '85',
    90: '90',
    120: '120',
}


def _format_diameter_for_elbow_prefix(d: float) -> str:
    if float(d).is_integer():
        return str(int(d))
    return f'{d:g}'


def compute_elbow_subfamily_prefix(spec: 'PartSpec') -> str | None:
    """Return the L-family SKU prefix (e.g. 'L590') for an elbow spec.

    Returns None when family != 'L', when angle is unknown, when angle is
    not catalog-recognized, or when diameter is unknown.
    """
    if spec.family != 'L' or spec.angle is None:
        return None
    angle_part = ELBOW_ANGLE_TO_PREFIX.get(int(spec.angle))
    if angle_part is None:
        return None
    if spec.diameter is None:
        return None
    return f'L{_format_diameter_for_elbow_prefix(spec.diameter)}{angle_part}'


def _assign_elbow_dimensions(
    spec: PartSpec,
    dim_tokens: list[tuple[int, NormalizedToken]],
    consumed: set,
) -> None:
    """Elbow family L wants diameter, angle, leg1, leg2 — handle specially.

    Strategy: identify the angle first (90, 45, etc. are unmistakable).
    Diameter is the small value (1-12). Remaining values are legs.
    """
    angles = []
    diameters = []
    legs = []
    indexed_tokens = list(dim_tokens)
    for i, t in indexed_tokens:
        value = t.data['value']
        if _looks_like_angle(value) and spec.angle is None:
            angles.append((i, value))
        elif _looks_like_diameter(value) and spec.diameter is None:
            diameters.append((i, value))
        else:
            legs.append((i, value))

    if angles:
        spec.angle = int(angles[0][1])
        consumed.add(angles[0][0])
    if diameters and spec.diameter is None:
        spec.diameter = diameters[0][1]
        consumed.add(diameters[0][0])
    if legs and spec.leg1 is None:
        spec.leg1 = legs[0][1]
        consumed.add(legs[0][0])
    if len(legs) > 1 and spec.leg2 is None:
        spec.leg2 = legs[1][1]
        consumed.add(legs[1][0])


def _assign_dimensions_heuristic(
    spec: PartSpec,
    dim_tokens: list[tuple[int, NormalizedToken]],
    consumed: set,
) -> None:
    """When family is unknown, use value-range heuristics to assign dimensions.

    Rules:
      - 1-12: probably diameter
      - 15-180 with angle-like value: probably angle (only meaningful for elbows)
      - >12: probably length
    """
    for i, t in dim_tokens:
        value = t.data['value']
        if _looks_like_diameter(value) and spec.diameter is None:
            spec.diameter = value
            consumed.add(i)
        elif _looks_like_length(value) and spec.length is None:
            spec.length = value
            consumed.add(i)


def _check_spec_completeness(spec: PartSpec) -> None:
    """If spec is partially filled but missing required fields, add an ambiguity.

    Different families have different completeness rules. For now, we only
    surface the most common: stack/pipe/muffler missing one of {body, finish,
    length}. The disambiguator is responsible for asking the human.
    """
    if spec.canonical_sku:
        return  # already complete

    if not spec.family:
        return  # caller should know they need a family before we can ask further

    if spec.family in ('K', 'BH', 'BR', 'A', 'WCK', 'SS', 'SP', 'D', 'S'):
        if spec.body is None:
            spec.ambiguities.append(Ambiguity(
                type='body_unspecified',
                candidates=['SB', 'EX', 'XB'],
                raw_input=spec.raw_input,
                reason=f'Family {spec.family} requires a body code (SB=OD-fit, EX=ID-fit, XB=variant)',
            ))
        if spec.finish is None:
            spec.ambiguities.append(Ambiguity(
                type='finish_unspecified',
                candidates=['A', 'C', 'P', 'S3', 'S4', 'BS'],
                raw_input=spec.raw_input,
                reason=f'Family {spec.family} requires a finish code',
            ))
        if spec.length is None:
            spec.ambiguities.append(Ambiguity(
                type='length_unspecified',
                candidates=[],
                raw_input=spec.raw_input,
                reason=f'Family {spec.family} requires a length',
            ))

    if spec.family == 'L':
        if spec.angle is None:
            spec.ambiguities.append(Ambiguity(
                type='angle_unspecified',
                candidates=[15, 30, 45, 60, 70, 75, 90, 180],
                raw_input=spec.raw_input,
                reason='Elbow family requires an angle',
            ))
        elif int(spec.angle) not in ELBOW_ANGLE_TO_PREFIX:
            spec.ambiguities.append(Ambiguity(
                type='angle_not_in_catalog',
                candidates=sorted(ELBOW_ANGLE_TO_PREFIX.keys()),
                raw_input=spec.raw_input,
                reason=(
                    f'Elbow angle {int(spec.angle)} has no catalog L-family '
                    'subfamily; closest catalog angles must be confirmed'
                ),
            ))
        if spec.leg1 is None or spec.leg2 is None:
            spec.ambiguities.append(Ambiguity(
                type='elbow_legs_unspecified',
                candidates=[],
                raw_input=spec.raw_input,
                reason='Elbow family requires both leg lengths',
            ))
        # Derive the subfamily prefix (e.g. 'L590') from angle + diameter
        # so downstream disambiguation can scope to L-subfamilies.
        spec.subfamily_prefix = compute_elbow_subfamily_prefix(spec)

    if spec.family == 'R':
        if spec.fit_inlet is None or spec.fit_outlet is None:
            spec.ambiguities.append(Ambiguity(
                type='fit_unspecified',
                candidates=['ID/ID', 'ID/OD', 'OD/ID', 'OD/OD'],
                raw_input=spec.raw_input,
                reason='Reducer requires both inlet and outlet fit (ID or OD)',
            ))


# ============================================================================
# Section 6: Self-test
# ============================================================================

def _selftest() -> None:
    # Full SKU pass-through
    spec = extract_spec('K5-24SBC')
    assert spec.canonical_sku == 'K5-24SBC' or (
        spec.family == 'K' and spec.diameter == 5.0
    ), f"Full SKU pass-through failed: {spec.to_dict()}"

    # Full natural-language input
    spec = extract_spec('5 inch chrome curved stack 24 inches OD/OD')
    assert spec.family == 'K', f"Expected K, got {spec.family}"
    assert spec.diameter == 5.0
    assert spec.length == 24.0
    assert spec.finish == 'C'
    assert spec.fit_inlet == 'OD'
    assert spec.fit_outlet == 'OD'

    # Word-order independence
    spec = extract_spec('curved stack 24 inches 5 inch chrome')
    assert spec.family == 'K'
    assert spec.diameter == 5.0
    assert spec.length == 24.0

    # Elbow with angle
    spec = extract_spec('5 inch 90 degree elbow chrome')
    assert spec.family == 'L'
    assert spec.diameter == 5.0
    assert spec.angle == 90
    assert spec.finish == 'C'

    # Reducer with bilateral
    spec = extract_spec('6 to 5 reducer ID/OD')
    assert spec.family == 'R'
    assert spec.fit_inlet == 'ID'
    assert spec.fit_outlet == 'OD'

    # Customer-program detection
    spec = extract_spec('Apex Diesel 548 charger pipe lower')
    assert spec.customer == 'Apex Diesel', f"Customer: {spec.customer}"
    assert spec.family == '548'
    assert spec.canonical_sku == '548CPL'

    # Truck model implies make
    spec = extract_spec('cascadia 5 inch elbow 90')
    assert spec.oem == 'FL'
    assert spec.truck_model == 'cascadia'
    assert spec.family == 'L'

    # OEM dedup
    spec = extract_spec('Peterbilt 379 chrome stack for a Pete')
    assert spec.oem == 'PB'
    # No oem_conflict ambiguity
    assert not any(a.type == 'oem_conflict' for a in spec.ambiguities), \
        f"Unexpected conflict: {[a.type for a in spec.ambiguities]}"

    # Ambiguous family
    spec = extract_spec('5 chrome stack 24')
    assert spec.family is None  # couldn't resolve
    assert any(a.type == 'family_unspecified' for a in spec.ambiguities)

    # Completeness check: stack with no finish
    spec = extract_spec('5 inch curved stack 24')
    assert spec.family == 'K'
    assert any(a.type == 'finish_unspecified' for a in spec.ambiguities)
    assert any(a.type == 'body_unspecified' for a in spec.ambiguities)


_selftest()
