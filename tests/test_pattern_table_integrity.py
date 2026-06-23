"""Structural integrity of the grammar dispatch table — a cheap guard on the
~5,900-line pattern file.

The BEHAVIORAL guarantee (every pattern correctly decodes its catalog rows) is
enforced by scripts/roundtrip_audit.py, which runs the whole catalog through the
grammar on every commit. This complements it by catching the *structural* foot-
guns that an audit over existing rows would not localize: a malformed entry, or
a half-wired pattern (regex added but decoder forgotten) that silently never
fires at dispatch (_dispatch.py skips any entry whose regex OR decoder is None).
"""
from __future__ import annotations

import re

from sku_translator.part_number_parser import _patterns as P


def test_every_pattern_entry_is_well_formed():
    for i, entry in enumerate(P.PATTERNS):
        assert isinstance(entry, tuple) and len(entry) == 3, \
            f'PATTERNS[{i}] is not a (name, regex, decoder) triple: {entry!r}'
        name, regex, decoder = entry
        assert isinstance(name, str) and name, f'PATTERNS[{i}] has an empty name'
        assert regex is None or isinstance(regex, re.Pattern), \
            f'{name!r}: regex is not a compiled pattern'
        assert decoder is None or callable(decoder), \
            f'{name!r}: decoder is not callable'


def test_no_half_wired_patterns():
    # Dispatch skips an entry if EITHER regex or decoder is None, so a half-set
    # entry would silently never fire. Both-None (a deliberate placeholder) is ok.
    half = [e[0] for e in P.PATTERNS if (e[1] is None) != (e[2] is None)]
    assert not half, f'half-wired patterns (silently skipped at dispatch): {half}'


def test_pattern_table_is_substantial():
    # Guards against an import/registration regression silently emptying the table
    # (which would make every SKU fall through to 'unstructured' yet still parse).
    assert len(P.PATTERNS) > 300
