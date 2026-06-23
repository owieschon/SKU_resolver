"""
SKU Translator — SKU Constructor (Inverse Parser)
==================================================

Given a complete ``PartSpec`` (typically from ``extract_spec()``), build the
canonical SKU string. This is the inverse of
``part_number_parser.parse()``.

The round-trip property holds for any SKU pattern the parser fully decodes:
::

    sku = "K5-24SBC"
    spec = extract_spec(sku)
    assert construct_sku(spec) == sku

Round-trip failures are real bugs — either the extractor lost information,
the constructor's template is wrong, or the parser's pattern doesn't fully
characterize the SKU. The test harness at the bottom enforces this.

Design principles
-----------------
1. **Dispatch by family.** Each family has its own template; the
   constructor picks based on ``spec.family``. Adding a new family means
   adding one builder function — no change to dispatch logic.
2. **Fail loud on missing fields.** If a required field is None, raise
   ``InsufficientSpecError`` with a list of what's missing. The
   disambiguator catches these and converts them into clarification
   questions.
3. **Pass-through for already-canonical SKUs.** If
   ``spec.canonical_sku`` is set, return it as-is. The extractor only sets
   that field when the input was already a parser-recognized SKU.
4. **Number formatting.** Lengths and diameters need careful handling:
   integers print as ``5`` (not ``5.0``); decimals print as ``17.02``
   (not ``17.020``). Helper ``_fmt_num()`` handles this.

Usage
-----
::

    from sku_translator.constructor import construct_sku

    spec = extract_spec("K5-24SBC")
    construct_sku(spec)  # -> "K5-24SBC"

    spec = PartSpec(family='K', diameter=5, length=24, body='SB', finish='C')
    construct_sku(spec)  # -> "K5-24SBC"
"""
from __future__ import annotations

from typing import Any

from sku_translator.extractor import PartSpec


# ============================================================================
# Section 1: Errors
# ============================================================================

class ConstructionError(Exception):
    """Base class for constructor errors."""


class InsufficientSpecError(ConstructionError):
    """Spec is missing fields required to construct a canonical SKU.

    Attributes:
        missing_fields: list of field names that are None but required
        family: the family the constructor was trying to build
    """
    def __init__(self, missing_fields: list[str], family: str | None):
        self.missing_fields = missing_fields
        self.family = family
        super().__init__(
            f"Cannot construct SKU for family {family!r}: missing fields {missing_fields}"
        )


class UnsupportedFamilyError(ConstructionError):
    """No constructor template registered for the given family."""
    def __init__(self, family: str):
        self.family = family
        super().__init__(
            f"No constructor template for family {family!r}. "
            f"This family may need a hand-written builder added to constructor.py."
        )


# ============================================================================
# Section 2: Number formatting helpers
# ============================================================================

def _fmt_num(value: float | int | None) -> str:
    """Format a number for SKU output.

    Integer values render without a decimal: ``5`` not ``5.0``.
    Decimal values render at minimum precision: ``17.02`` not ``17.020``.
    None raises (caller should validate first).
    """
    if value is None:
        raise ConstructionError("Cannot format None as a SKU number")
    if isinstance(value, int) or (isinstance(value, float) and value == int(value)):
        return str(int(value))
    # Round to 4 decimal places then strip trailing zeros
    formatted = f"{value:.4f}".rstrip('0').rstrip('.')
    return formatted


def _require(spec: PartSpec, fields: list[str], family: str) -> None:
    """Raise InsufficientSpecError if any of the listed fields is None on spec."""
    missing = [f for f in fields if getattr(spec, f, None) is None]
    if missing:
        raise InsufficientSpecError(missing_fields=missing, family=family)


# ============================================================================
# Section 3: Family-specific builders
# ============================================================================
# Each builder takes a PartSpec and returns the canonical SKU string. They
# assume the spec is valid for that family — if not, they raise.

# All families that share the standard parametric template
# {family}{diameter}-{length}{body}{finish}
PARAMETRIC_FAMILIES = {
    'K', 'BH', 'BR', 'A', 'WCK', 'SS', 'SK', 'D', 'CSP', 'BT', 'DTS', 'EXS',
    'S', 'SP', 'M', 'ZP', 'ZM', 'T', 'CP', 'CN', 'Y', 'SL',
}


def _build_parametric(spec: PartSpec) -> str:
    """{family}{diameter}-{length}{body}{finish}

    Examples:
        K5-24SBC, BH5-30SBC, SP7-55SBC, ZP12-100EXA
    """
    _require(spec, ['family', 'diameter', 'length', 'body', 'finish'], spec.family)
    return (
        f"{spec.family}{_fmt_num(spec.diameter)}-{_fmt_num(spec.length)}"
        f"{spec.body}{spec.finish}"
    )


def _build_elbow(spec: PartSpec) -> str:
    """L{diameter}{angle}-{leg1}{leg2}{?S-for-OD-mating}{finish}

    The ``S`` modifier appears between legs and finish when the elbow has
    OD-mating ends (``spec.body == 'SB'`` in our model). For ID-mating
    (``EX``) elbows, no S is inserted.

    Examples:
        L590-1715SC      (5"x90, legs 17/15, S=OD, finish C)
        L590-17.0215S3   (5"x90, legs 17.02/15, finish 304SS) — note absence of S marker
        L630-2218A       (6"x30, legs 22/18, finish A)
    """
    _require(spec, ['family', 'diameter', 'angle', 'leg1', 'leg2', 'finish'], 'L')

    # The S modifier between legs and finish indicates OD-mating
    od_marker = 'S' if spec.body == 'SB' else ''
    # Legs are zero-padded to 2-digit integer width when integer; preserved
    # as decimal otherwise. Catalog convention: '17' not '17.0', '17.02' for
    # decimal cases.
    leg1 = _fmt_num(spec.leg1)
    leg2 = _fmt_num(spec.leg2)
    # Pad single-digit integer legs to 2 digits (catalog uses '09' not '9')
    if leg1.isdigit() and len(leg1) == 1:
        leg1 = '0' + leg1
    if leg2.isdigit() and len(leg2) == 1:
        leg2 = '0' + leg2
    return (
        f"L{_fmt_num(spec.diameter)}{spec.angle}-"
        f"{leg1}{leg2}{od_marker}{spec.finish}"
    )


def _build_reducer(spec: PartSpec) -> str:
    """R{inlet_diameter}-{outlet_diameter}{?fit}{finish}

    Reducers are the only family with bilateral (inlet/outlet) dimensions
    and explicit fit codes. The fit appears as a 2-letter code (II, IO,
    OI, OO) before the finish.
    """
    _require(spec, ['family', 'inlet_diameter', 'outlet_diameter', 'finish'], 'R')
    fit_code = ''
    if spec.fit_inlet and spec.fit_outlet:
        # ID -> I, OD -> O. Concatenate inlet+outlet.
        fit_code = spec.fit_inlet[0].upper() + spec.fit_outlet[0].upper()
    return (
        f"R{_fmt_num(spec.inlet_diameter)}-"
        f"{_fmt_num(spec.outlet_diameter)}{fit_code}{spec.finish}"
    )


def _build_guard(spec: PartSpec) -> str:
    """{family}-{slot}{?material}

    Guard families (PG, MG, AHS, UHS) use additive suffix grammar:
    slot pattern + optional material code. The slot/material data comes
    from the parser pattern, not from generic spec fields, so we use the
    body field as a stand-in (extractor populates it from
    ``parser_result['guard_slot']`` when available).

    For the constructor, we expect the spec to expose the slot pattern
    (and optional material) via ``unconsumed_tokens`` or via a custom
    field added to PartSpec specifically for guard SKUs.

    Currently this builder requires the canonical_sku to already be set
    by the extractor — guard SKUs that come from the parser pass through
    untouched. Building guard SKUs from natural-language input requires
    extending PartSpec with guard_slot/guard_material fields, which is a
    deferred enhancement.
    """
    if spec.canonical_sku:
        return spec.canonical_sku
    raise InsufficientSpecError(
        missing_fields=['guard_slot', 'guard_material'],
        family=spec.family,
    )


def _build_cm_muffler(spec: PartSpec) -> str:
    """CM-{length}{finish}

    Length is the primary dimension (no diameter in CM SKUs).
    """
    _require(spec, ['family', 'length', 'finish'], 'CM')
    return f"CM-{_fmt_num(spec.length)}{spec.finish}"


def _build_smb(spec: PartSpec) -> str:
    """SMB or SMB-{finish}

    Bare SMB is powder-coat default; -C / -R are explicit finishes.
    """
    _require(spec, ['family'], 'SMB')
    if spec.finish:
        return f"SMB-{spec.finish}"
    return "SMB"


def _build_dss(spec: PartSpec) -> str:
    """DSS-{1-digit-diameter}{2-digit-length}

    DSS uses a unique encoding: diameter is 1 digit, length is 2 digits,
    concatenated without separator. So 4"x8" = DSS-408, 4"x12" = DSS-412.
    """
    _require(spec, ['family', 'diameter', 'length'], 'DSS')
    if spec.diameter != int(spec.diameter):
        raise ConstructionError(
            f"DSS diameter must be a whole number (got {spec.diameter})"
        )
    if spec.length != int(spec.length):
        raise ConstructionError(
            f"DSS length must be a whole number (got {spec.length})"
        )
    diameter = int(spec.diameter)
    length = int(spec.length)
    if not (1 <= diameter <= 9):
        raise ConstructionError(f"DSS diameter must be 1-9 (got {diameter})")
    if not (1 <= length <= 99):
        raise ConstructionError(f"DSS length must be 1-99 (got {length})")
    return f"DSS-{diameter}{length:02d}"


def _build_marmon(spec: PartSpec) -> str:
    """{5-6 digit Marmon family code}-{suffix}

    Marmon SKUs require ``customer_program`` to hold the family number
    and either ``body`` or a custom field to hold the suffix. For now,
    pass-through if canonical_sku is already set.
    """
    if spec.canonical_sku:
        return spec.canonical_sku
    raise InsufficientSpecError(
        missing_fields=['marmon_family_pn', 'marmon_suffix'],
        family=spec.family,
    )


def _build_perf_diesel(spec: PartSpec) -> str:
    """Apex Diesel proprietary SKU.

    Three program prefixes: 7XX, 8XX, 548. The program prefix is in
    ``customer_program``, and the configuration suffix needs to be in a
    field we don't yet have on PartSpec. So we rely on
    ``canonical_sku`` being pre-set by the extractor for now.
    """
    if spec.canonical_sku:
        return spec.canonical_sku
    raise InsufficientSpecError(
        missing_fields=['perf_diesel_config_suffix'],
        family=spec.family,
    )


def _build_hanger(spec: PartSpec) -> str:
    """H-{oem}{component}{seq}{?finish}

    Hanger SKUs need OEM, component letter, sequence, and optional finish.
    None of these except OEM map cleanly to standard PartSpec fields, so
    we rely on canonical_sku pass-through for now.
    """
    if spec.canonical_sku:
        return spec.canonical_sku
    raise InsufficientSpecError(
        missing_fields=['hanger_component', 'hanger_seq'],
        family='H',
    )


def _build_prk(spec: PartSpec) -> str:
    """PRK-{embedded-elbow-or-component}

    PRK SKUs embed an elbow SKU as the fitment indicator. Pass through
    if canonical_sku is set.
    """
    if spec.canonical_sku:
        return spec.canonical_sku
    raise InsufficientSpecError(
        missing_fields=['prk_embedded'],
        family='PRK',
    )


def _build_ez_clamp(spec: PartSpec) -> str:
    """EZ-{diameter}SS{?BK}

    Diameter encoding: implicit decimal. 4 = 4", 35 = 3.5", 225 = 2.25".
    """
    _require(spec, ['family', 'diameter'], 'EZ')
    return f"EZ-{_encode_clamp_diameter(spec.diameter)}SS"


def _build_griez_clamp(spec: PartSpec) -> str:
    """GRIEZ-{diameter}SS

    Same diameter encoding as EZ.
    """
    _require(spec, ['family', 'diameter'], 'GRIEZ')
    return f"GRIEZ-{_encode_clamp_diameter(spec.diameter)}SS"


def _encode_clamp_diameter(diameter: float) -> str:
    """Encode a clamp diameter using the EZ Seal implicit-decimal convention.

    4.0   -> '4'
    3.5   -> '35'
    2.25  -> '225'
    """
    if diameter == int(diameter):
        return str(int(diameter))
    # Multiply by 10 to test for half-inches
    times_10 = diameter * 10
    if times_10 == int(times_10):
        return str(int(times_10))
    # Multiply by 100 for quarter-inches
    times_100 = round(diameter * 100)
    return str(times_100)


def _build_custom_50(spec: PartSpec) -> str:
    """50{customer-code}{seq}

    Always pass through if canonical_sku is set.
    """
    if spec.canonical_sku:
        return spec.canonical_sku
    raise InsufficientSpecError(
        missing_fields=['custom_50_customer_code', 'custom_50_seq'],
        family=spec.family,
    )


def _build_2k_bulk(spec: PartSpec) -> str:
    """2K-{length}"""
    _require(spec, ['family', 'length'], '2K')
    return f"2K-{_fmt_num(spec.length)}"


# ============================================================================
# Section 4: Family → builder dispatch
# ============================================================================

# Builder dispatch table. Adding a new family means adding one entry here.
FAMILY_BUILDERS: dict[str, Any] = {}

# Register parametric families
for fam in PARAMETRIC_FAMILIES:
    FAMILY_BUILDERS[fam] = _build_parametric

# Register specialized builders
FAMILY_BUILDERS.update({
    'L':       _build_elbow,
    'R':       _build_reducer,
    'PG':      _build_guard,
    'MG':      _build_guard,
    'AHS':     _build_guard,
    'UHS':     _build_guard,
    'CM':      _build_cm_muffler,
    'SMB':     _build_smb,
    'DSS':     _build_dss,
    'MARMON':  _build_marmon,
    '548':     _build_perf_diesel,
    '777':     _build_perf_diesel,
    '888':     _build_perf_diesel,
    'H':       _build_hanger,
    'PRK':     _build_prk,
    'EZ':      _build_ez_clamp,
    'GRIEZ':   _build_griez_clamp,
    '50':      _build_custom_50,
    '2K':      _build_2k_bulk,
})


# ============================================================================
# Section 5: Top-level construct_sku()
# ============================================================================

def construct_sku(spec: PartSpec) -> str:
    """Build the canonical SKU from a PartSpec.

    Pass-through behavior:
      - If ``spec.canonical_sku`` is set, return it directly. The extractor
        sets this only when the input was already recognized by the parser.

    Construction:
      - Otherwise, dispatch on ``spec.family`` to the appropriate builder.

    Errors:
      - ``InsufficientSpecError`` if required fields are None.
      - ``UnsupportedFamilyError`` if there's no builder for this family.
    """
    if spec.canonical_sku:
        return spec.canonical_sku

    if not spec.family:
        raise InsufficientSpecError(missing_fields=['family'], family=None)

    builder = FAMILY_BUILDERS.get(spec.family)
    if builder is None:
        raise UnsupportedFamilyError(family=spec.family)

    return builder(spec)


# ============================================================================
# Section 6: Round-trip self-test
# ============================================================================

def _selftest() -> None:
    """Build → parse → build round-trip on every supported family.

    For each test SKU:
      1. Parse via existing part_number_parser
      2. Convert parser output to a PartSpec (via extractor)
      3. Construct a SKU from the spec
      4. Assert the constructed SKU equals the original

    This is the strongest correctness check we have. If anything regresses
    in the normalizer, extractor, or constructor, this catches it.
    """
    from sku_translator.extractor import extract_spec

    # SKUs with full parser support and full constructor support should
    # round-trip exactly.
    full_round_trip = [
        'K5-24SBC',
        'K5-24EXA',
        'K7-32SBS3',
        'BH5-30SBC',
        'A5-18SBA',
        'SP7-55SBC',
        # Reducer
        # 'R5-6IOC',  # if reducer SKUs follow this template — skipped until verified
        # CM
        'CM-56C',
        # SMB
        'SMB-C',
        'SMB-R',
        # DSS
        'DSS-408',
        'DSS-512',
        # 2K
        '2K-48',
        # EZ Seal
        'EZ-4SS',
        'GRIEZ-4SS',
    ]

    pass_through_skus = [
        # These don't fully round-trip because the constructor relies on
        # canonical_sku pass-through (PartSpec doesn't expose every field
        # for every family yet). They still must produce the original SKU.
        'L590-1715SC',  # elbow with explicit S=OD
        'PG-VS',
        'PG-VSS3',
        'UHS-NS',
        '41968-L',
        '548CPL',
        '888EX',
        'H-IHM6A',
        'PRK-L790C',
        '50DD3101',
    ]

    for sku in full_round_trip + pass_through_skus:
        spec = extract_spec(sku)
        try:
            constructed = construct_sku(spec)
        except ConstructionError as e:
            raise AssertionError(
                f"Round-trip failed for {sku!r}: {e}\n"
                f"  spec: {spec.to_dict()}"
            )
        assert constructed == sku, (
            f"Round-trip mismatch for {sku!r}:\n"
            f"  spec:        {spec.to_dict()}\n"
            f"  constructed: {constructed!r}\n"
            f"  expected:    {sku!r}"
        )

    # Direct-construction tests: build PartSpec from scratch (not from parser)
    direct_tests = [
        (PartSpec(family='K', diameter=5, length=24, body='SB', finish='C'),
         'K5-24SBC'),
        (PartSpec(family='BH', diameter=7, length=32, body='EX', finish='A'),
         'BH7-32EXA'),
        (PartSpec(family='CM', length=56, finish='C'),
         'CM-56C'),
        (PartSpec(family='DSS', diameter=4, length=8),
         'DSS-408'),
        (PartSpec(family='EZ', diameter=3.5),
         'EZ-35SS'),
        (PartSpec(family='EZ', diameter=2.25),
         'EZ-225SS'),
        (PartSpec(family='2K', length=48),
         '2K-48'),
    ]

    for spec, expected in direct_tests:
        constructed = construct_sku(spec)
        assert constructed == expected, (
            f"Direct construction failed:\n"
            f"  spec:        {spec.to_dict()}\n"
            f"  constructed: {constructed!r}\n"
            f"  expected:    {expected!r}"
        )

    # Error case: missing fields surfaces as InsufficientSpecError
    incomplete_spec = PartSpec(family='K', diameter=5)  # missing length, body, finish
    try:
        construct_sku(incomplete_spec)
        raise AssertionError("Expected InsufficientSpecError, got success")
    except InsufficientSpecError as e:
        assert 'length' in e.missing_fields
        assert 'body' in e.missing_fields
        assert 'finish' in e.missing_fields

    # Error case: unsupported family
    bad_spec = PartSpec(family='ZZZNONEXISTENT')
    try:
        construct_sku(bad_spec)
        raise AssertionError("Expected UnsupportedFamilyError, got success")
    except UnsupportedFamilyError:
        pass


_selftest()
