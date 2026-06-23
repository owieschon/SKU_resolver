#!/usr/bin/env python3
"""Readiness gate: 'production-grade' must be demonstrable, not asserted.

Composes the repo's verification artifacts into a single blocking readiness
file (state/readiness.json). ready=true requires ALL of:

  1. CLEAN TREE + PINNED COMMIT (deploy-guard preflight):
     no uncommitted changes to src/tests/data/scripts/pyproject — a green
     stamped from a dirty tree is a claim about code that exists nowhere.
     Adapted from a prior agent stack's deploy-guard contract
     ("make 'running code != deployed code' impossible to miss"), which a
     2026-05-29 incident motivated: a full live-verification session ran
     against a stale process. CI trees are always clean; this guard exists
     for local runs.

  2. TEST SUITE GREEN: pytest JUnit XML at state/pytest.xml shows
     0 failures / 0 errors and a nonzero test count (collected count is
     recorded, never asserted from documentation).

  3. ROUND-TRIP AUDIT OK: state/roundtrip_audit.json (written by
     scripts/roundtrip_audit.py) has ok=true — identity 100%, no silent
     rewrites beyond the pinned baseline, coverage above floor.

Exit 0 and ready=true only when every gate holds. Anything else: ready=false,
exit 1, and the failing gate named in `blocking`.

Run order (locally or CI):
    pytest --junitxml=state/pytest.xml
    python scripts/roundtrip_audit.py
    python scripts/readiness.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# defusedxml over stdlib ET: the JUnit file is self-generated in this
# pipeline, but parse defensively anyway — stdlib XML is XXE/billion-laughs
# vulnerable by default and the safe parser costs nothing.
import defusedxml.ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = REPO_ROOT / 'state'
GUARDED_PATHS = ('src/', 'tests/', 'data/', 'scripts/', 'pyproject.toml')


def _git(*args: str) -> str:
    proc = subprocess.run(
        ['git', *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
    )
    # On error (e.g. rev-parse HEAD in a repo with no commits) stdout can
    # still carry the literal ref name — return '' so callers see "absent".
    return proc.stdout.strip() if proc.returncode == 0 else ''


def main() -> int:
    blocking: list[str] = []

    # Gate 1 — deploy-guard preflight
    head = _git('rev-parse', 'HEAD') or None
    porcelain = _git('status', '--porcelain')
    dirty = [
        line for line in porcelain.splitlines()
        if line[3:].startswith(GUARDED_PATHS)
    ]
    if head is None:
        blocking.append('preflight: no git HEAD (not a repo or no commits)')
    if dirty:
        blocking.append(f'preflight: {len(dirty)} uncommitted guarded change(s)')

    # Gate 2 — test suite
    tests = {'collected': 0, 'failures': None, 'errors': None}
    junit = STATE / 'pytest.xml'
    if not junit.exists():
        blocking.append('tests: state/pytest.xml missing (run pytest --junitxml=state/pytest.xml)')
    else:
        suite = ET.parse(junit).getroot()
        if suite.tag == 'testsuites':
            suite = suite[0]
        tests = {
            'collected': int(suite.get('tests', 0)),
            'failures': int(suite.get('failures', 0)),
            'errors': int(suite.get('errors', 0)),
        }
        if tests['collected'] == 0:
            blocking.append('tests: zero tests collected')
        if tests['failures'] or tests['errors']:
            blocking.append(
                f"tests: {tests['failures']} failures, {tests['errors']} errors"
            )

    # Gate 3 — round-trip audit
    audit = {}
    audit_file = STATE / 'roundtrip_audit.json'
    if not audit_file.exists():
        blocking.append('audit: state/roundtrip_audit.json missing (run scripts/roundtrip_audit.py)')
    else:
        audit = json.loads(audit_file.read_text())
        if not audit.get('ok'):
            blocking.append('audit: roundtrip_audit ok=false')

    ready = not blocking
    out = {
        'ready': ready,
        'blocking': blocking,
        'commit': head,
        'tree_clean_for_guarded_paths': not dirty,
        'tests': tests,
        'audit': {
            k: audit.get(k)
            for k in ('total_skus', 'identity_pass', 'silent_rewrites',
                      'roundtrip_coverage', 'ok')
        },
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    STATE.mkdir(exist_ok=True)
    (STATE / 'readiness.json').write_text(json.dumps(out, indent=2))

    print(json.dumps(out, indent=2))
    return 0 if ready else 1


if __name__ == '__main__':
    sys.exit(main())
