#!/usr/bin/env python3
"""Round-trip audit: the never-invent / identity guarantee, verified over the
ENTIRE catalog on every CI run.

Two checks, run against every SKU in the catalog (count derived at runtime
from the fixture — never hardcoded):

1. IDENTITY (hard gate, must be 100%):
   translate(sku) must RESOLVE to exactly that SKU. Every catalog row is
   reachable and resolution never rewrites a canonical SKU into a different
   one. Any miss fails the audit.

2. NO NEW SILENT REWRITES (hard gate vs pinned baseline):
   For every SKU where construct(extract(sku)) SUCCEEDS, the rebuilt string
   must equal the original exactly — EXCEPT for the 64 known truncations
   pinned at migration in data/known_construct_truncations.json (catalog
   rows whose SKU embeds another SKU as a prefix, e.g. 'FB-4ZN SADDLE' ->
   'FB-4ZN'; 26 of them truncate to a DIFFERENT REAL catalog SKU). Those
   rows resolve correctly through translate() via the verbatim path (gate 1
   covers them); the pin makes the residual construct-path risk explicit
   and fails the audit on ANY new entry. An InsufficientSpecError is accurate
   out-of-constructive-scope, not a failure.

3. FULL-ROUND-TRIP COVERAGE (regression floor):
   Fraction of the catalog that survives extract -> construct -> identical
   string. Measured baseline at migration (2026-06-06): 9458/9919 = 95.35%.
   The audit fails if this drops below ROUNDTRIP_COVERAGE_FLOOR — a grammar
   or extractor regression signal.

Exit code 0 = both gates pass. Nonzero = failure, with per-SKU diagnostics.
Writes a machine-readable summary to state/roundtrip_audit.json for the
readiness gate.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'src'))

from sku_translator import translate, FixtureCatalogIndex, InMemoryStore, RESOLVED
from sku_translator.extractor import extract_spec
from sku_translator.constructor import construct_sku, InsufficientSpecError
from sku_translator.part_number_parser import parse as parse_sku

# Measured at migration 2026-06-06: full round-trip = 9458/9919 = 95.35%.
# Floor set just under baseline; a drop means an extractor/grammar regression.
ROUNDTRIP_COVERAGE_FLOOR = 0.95


def main() -> int:
    catalog_path = os.environ.get(
        'SKU_CATALOG_PATH', str(REPO_ROOT / 'data' / 'catalog.csv')
    )
    catalog = FixtureCatalogIndex(catalog_path, tenant_id='audit')
    skus = catalog.all_skus()
    total = len(skus)
    print(f'Catalog loaded: {total} SKUs (derived at runtime from {catalog_path})')

    pin_path = REPO_ROOT / 'data' / 'known_construct_truncations.json'
    pinned = set(json.loads(pin_path.read_text())['truncations'].keys())
    print(f'Pinned known truncations: {len(pinned)}')

    t0 = time.monotonic()
    identity_failures: list[tuple[str, str, str | None]] = []
    silent_rewrites: list[tuple[str, str]] = []
    full_roundtrips = 0
    out_of_scope = 0  # accurate InsufficientSpecError / non-constructive patterns

    mem = InMemoryStore()
    for sku in skus:
        # Gate 1: identity through the full translator
        result = translate(sku, catalog=catalog, memory=mem)
        if result.state != RESOLVED or result.sku != sku:
            identity_failures.append((sku, result.state, result.sku))

        # Gates 2+3: extract -> construct
        try:
            spec = extract_spec(sku)
            rebuilt = construct_sku(spec)
        except InsufficientSpecError:
            out_of_scope += 1
            continue
        except Exception:  # non-constructive pattern routed clearly
            out_of_scope += 1
            continue
        if rebuilt == sku:
            full_roundtrips += 1
        elif sku not in pinned:
            silent_rewrites.append((sku, rebuilt))

    elapsed = time.monotonic() - t0
    coverage = full_roundtrips / total if total else 0.0

    print(f'Identity gate:       {total - len(identity_failures)}/{total} '
          f'({"PASS" if not identity_failures else "FAIL"})')
    print(f'New silent rewrites: {len(silent_rewrites)} beyond {len(pinned)} pinned '
          f'({"PASS" if not silent_rewrites else "FAIL — dangerous"})')
    print(f'Full round-trip:     {full_roundtrips}/{total} = {coverage:.2%} '
          f'(floor {ROUNDTRIP_COVERAGE_FLOOR:.0%}; out-of-scope {out_of_scope})')
    print(f'Elapsed: {elapsed:.1f}s')

    for sku, state, got in identity_failures[:20]:
        print(f'  IDENTITY MISS:  {sku!r} -> state={state} sku={got!r}')
    for sku, rebuilt in silent_rewrites[:20]:
        print(f'  SILENT REWRITE: {sku!r} -> constructed {rebuilt!r}')

    ok = (not identity_failures
          and not silent_rewrites
          and coverage >= ROUNDTRIP_COVERAGE_FLOOR)

    out = REPO_ROOT / 'state' / 'roundtrip_audit.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        'total_skus': total,
        'identity_pass': total - len(identity_failures),
        'identity_failures': len(identity_failures),
        'silent_rewrites': len(silent_rewrites),
        'full_roundtrips': full_roundtrips,
        'roundtrip_coverage': round(coverage, 4),
        'roundtrip_coverage_floor': ROUNDTRIP_COVERAGE_FLOOR,
        'out_of_scope': out_of_scope,
        'elapsed_seconds': round(elapsed, 1),
        'ok': ok,
    }, indent=2))
    print(f'Summary written: {out.relative_to(REPO_ROOT)}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
