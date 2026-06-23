"""Structural guarantees for the gateway, by import graph.

1. The gateway does no date math of its own — availability derives entirely
   from fulfillment.ship_date (G4 DoD). answers.py may import fulfillment's
   API but must not compute dates itself.
2. The gateway pulls no network stack at import (Twilio/AssemblyAI/HTTP are
   loaded only in the live voice adapter / webhook, lazily).
3. No critical asserts in gateway production code (survive python -O).
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / 'src' / 'gateway'
_FORBIDDEN_NET = {'socket', 'http', 'http.client', 'urllib', 'requests',
                  'httpx', 'aiohttp', 'twilio', 'assemblyai'}


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or '')
    return found


def test_gateway_imports_no_network_stack():
    for p in SRC.glob('*.py'):
        bad = {i for i in _imports(p)
               if i in _FORBIDDEN_NET or i.split('.')[0] in _FORBIDDEN_NET}
        assert not bad, f'{p.name} imports a network/vendor stack at module ' \
                        f'level: {bad}'


def test_answers_does_no_date_arithmetic():
    """availability must call ship_date, never do its own date math."""
    src = (SRC / 'answers.py').read_text()
    # no timedelta / date / datetime arithmetic imported into answers
    imported = _imports(SRC / 'answers.py')
    assert 'datetime' not in imported, \
        'answers.py imports datetime — date math belongs in fulfillment'
    assert 'ship_date' in src   # it delegates


def test_no_asserts_in_gateway_production_source():
    offenders = []
    for p in SRC.glob('*.py'):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Assert):
                offenders.append(f'{p.name}:{node.lineno}')
    assert not offenders, f'asserts vanish under python -O: {offenders}'
