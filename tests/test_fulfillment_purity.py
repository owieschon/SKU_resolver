"""Purity proof for the fulfillment package — by import graph, not by claim.

The M1 definition of done says ship_date() is pure with "zero LLM anywhere
near it (verify by import graph)". This test IS that verification: it walks
the AST of every module in src/fulfillment/ and asserts the import closure
stays inside an explicit stdlib whitelist. No LLM SDKs, no network stacks,
no subprocess, no I/O in the decision path (json/pathlib are permitted ONLY
for load_inventory, which runs at startup, not at quote time — see the
per-module table below), and no dependency on the translator package: the
fulfillment engine is independently testable and reusable.
"""
from __future__ import annotations

import ast
from pathlib import Path

FULFILLMENT = Path(__file__).resolve().parent.parent / 'src' / 'fulfillment'

# Whitelist per module. Anything imported outside this table fails the test —
# including indirect creep like `requests`, `openai`, `anthropic`,
# `subprocess`, `socket`, or `sku_translator`.
ALLOWED = {
    '__init__.py': {'fulfillment.calendar', 'fulfillment.engine'},
    'calendar.py': {'__future__', 'datetime', 'zoneinfo'},
    'engine.py': {'__future__', 'json', 'dataclasses', 'datetime', 'enum',
                  'pathlib', 'fulfillment.calendar'},
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or '')
    return found


def test_fulfillment_import_closure_is_whitelisted():
    files = sorted(p.name for p in FULFILLMENT.glob('*.py'))
    assert files == sorted(ALLOWED), (
        f'fulfillment modules changed ({files}); update the whitelist '
        f'DELIBERATELY — this table is the purity contract'
    )
    for name in files:
        extra = _imports(FULFILLMENT / name) - ALLOWED[name]
        assert not extra, f'{name} imports outside the purity whitelist: {extra}'


def test_fulfillment_does_not_import_the_translator():
    for p in FULFILLMENT.glob('*.py'):
        assert 'sku_translator' not in p.read_text(), (
            f'{p.name} references sku_translator; the fulfillment engine '
            f'must stay independent'
        )


def test_quote_path_does_no_file_io():
    # json/pathlib are whitelisted for load_inventory (startup) only; the
    # decision path itself must not touch the filesystem. Approximation that
    # fails loudly on drift: ship_date's function body may not call open()
    # or Path(), and engine.py may use them only inside load_inventory.
    src = (FULFILLMENT / 'engine.py').read_text()
    tree = ast.parse(src)
    offenders = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        if fn.name == 'load_inventory':
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                callee = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
                if callee in {'open', 'Path', 'read_text', 'write_text'}:
                    offenders.append(f'{fn.name}: {callee}')
    assert not offenders, f'file I/O in the decision path: {offenders}'
