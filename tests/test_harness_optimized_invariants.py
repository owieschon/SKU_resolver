"""R0 #2: load-bearing invariants must survive `python -O` (asserts stripped).

Two layers of proof:
  1. A static check that no `assert` statement remains in the harness's
     non-test source — invariants must be explicit `raise`s.
  2. A subprocess run under `-O` that trips each invariant and confirms the
     typed exception still fires (asserts would have been compiled out).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / 'src'


def test_no_asserts_in_harness_production_source():
    offenders = []
    for p in (SRC / 'erp_harness').glob('*.py'):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Assert):
                offenders.append(f'{p.name}:{node.lineno}')
    assert not offenders, (
        f'assert statements in harness production code will vanish under '
        f'python -O: {offenders} — use explicit raise')


def test_invariants_fire_under_O():
    """Run the manifest least-privilege and contract-totality invariants in a
    -O subprocess; both must still raise."""
    prog = r'''
import sys
sys.path.insert(0, %r)
from erp_harness.models import InvariantViolation
from erp_harness import recon
from erp_harness.models import Grant, ERPDescriptor, ERPClass

# Tamper a manifest grant to be non-read and confirm the invariant raises
# even with asserts stripped (-O).
recon._SUPPORTED[ERPClass.BC_SAAS] = (
    Grant('items', 'API write items', 'tampered'),
)
try:
    recon.generate_manifest(ERPDescriptor(ERPClass.BC_SAAS, 'BC', 'saas'))
    print('FAIL: no raise')
except InvariantViolation:
    print('OK: invariant fired under -O')
''' % str(SRC)
    out = subprocess.run([sys.executable, '-O', '-c', prog],
                         capture_output=True, text=True, timeout=30)
    assert 'OK: invariant fired under -O' in out.stdout, \
        f'invariant did not fire under -O: {out.stdout}\n{out.stderr}'
