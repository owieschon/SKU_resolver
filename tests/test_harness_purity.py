"""Structural guarantees by import graph, same pattern as fulfillment purity.

1. erp_harness imports NO network stack — the Backend protocol is the only
   wire boundary, so the enforcer is provably the only transport.
2. erp_harness never imports the twin — the harness cannot 'know' it is
   being tested (no twin-aware branches possible).
3. The twin is the only module allowed to know both sides (it implements
   the Backend protocol and seeds from the catalog fixture).
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / 'src'
_FORBIDDEN_NET = {'socket', 'http', 'http.client', 'urllib', 'urllib.request',
                  'requests', 'httpx', 'aiohttp', 'subprocess'}


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or '')
    return found


def test_harness_imports_no_network_stack():
    for p in (SRC / 'erp_harness').glob('*.py'):
        bad = {i for i in _imports(p)
               if i in _FORBIDDEN_NET or i.split('.')[0] in _FORBIDDEN_NET}
        assert not bad, f'{p.name} imports a network stack: {bad} — the '\
                        f'Backend protocol is the only wire boundary'


def test_harness_never_imports_the_twin():
    for p in (SRC / 'erp_harness').glob('*.py'):
        twin_refs = {i for i in _imports(p) if i.startswith('erp_twin')}
        assert not twin_refs, f'{p.name} imports the twin: the harness must '\
                              f'not know it is being tested'


def test_fulfillment_still_independent_of_harness():
    # The harness build must not have coupled the ship-date engine to it.
    for p in (SRC / 'fulfillment').glob('*.py'):
        refs = {i for i in _imports(p)
                if i.startswith(('erp_harness', 'erp_twin', 'resolution'))}
        assert not refs, f'fulfillment/{p.name} grew a dependency: {refs}'
