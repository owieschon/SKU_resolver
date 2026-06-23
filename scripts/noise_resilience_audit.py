#!/usr/bin/env python3
"""Noise-resilience audit: how the resolver behaves on REALISTIC bad input.

The round-trip audit (scripts/roundtrip_audit.py) proves the grammar is
well-formed and invertible — but it feeds the engine *clean* canonical SKUs, so
its 96.96% is "high by construction". This audit answers the harder, separate
question a reviewer actually cares about: **when the input is noisy — a voice
transcription typo, an OCR slip, a half-spoken spec — does the engine stay
honest?** It must never invent a SKU, and it should degrade to a PENDING / read-
back rather than assert a wrong-but-plausible part number.

Three perturbation classes, each mapped to a real failure mode of THIS system:

  A. TYPO_MUTATIONS  — single/double char swap/delete/insert/replace
                       (mis-transcribed voice, fat-fingered typing).
  B. OCR_CONFUSION   — O<->0, I/l<->1, S<->5, Z<->2, B<->8, G<->6
                       (a scanned spec sheet or fax).
  C. PARTIAL_SPECS   — a real description with one word dropped
                       (the caller didn't say the length) -> expect PENDING.

Three HONEST metrics (no manufactured "accuracy"):
  1. resolution_rate        resolved / total.
  2. never_invent_failures  MUST be 0 — every resolved SKU and every surfaced
                            candidate is a real catalog row (the core guarantee,
                            under noise). A distance-1 typo that lands on a
                            DIFFERENT real SKU is NOT a failure: the engine marks
                            it confidence='medium' precisely so the conversational
                            layer reads it back before acting.
  3. graceful_degradation   (pending + unresolvable) / total — the share that
                            correctly declines to guess.

Exit 0 iff never_invent_failures == 0. Writes state/noise_resilience_audit.json.
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'src'))

from resolution import ResolutionService, catalog_content_version
from sku_translator import FixtureCatalogIndex, InMemoryStore

SEED = 20260623
_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-'
# Glyph pairs an OCR engine confuses most often, both directions.
_OCR = {'O': '0', '0': 'O', 'I': '1', '1': 'I', 'S': '5', '5': 'S',
        'Z': '2', '2': 'Z', 'B': '8', '8': 'B', 'G': '6', '6': 'G'}

CLASSES = ('typo', 'ocr', 'partial')


def typo_mutate(sku: str, rng: random.Random) -> str:
    """One or two char-level edits — the plausible-but-wrong class."""
    for _ in range(rng.randint(1, 2)):
        s = list(sku)
        i = rng.randrange(len(s))
        op = rng.randrange(4)
        if op == 0 and len(s) > 2:
            del s[i]
        elif op == 1:
            s.insert(i, rng.choice(_ALPHABET))
        elif op == 2:
            s[i] = rng.choice(_ALPHABET)
        elif len(s) > 2:
            j = rng.randrange(len(s))
            s[i], s[j] = s[j], s[i]
        sku = ''.join(s)
    return sku


def ocr_confuse(sku: str, rng: random.Random) -> str:
    """Substitute 1-2 OCR-confusable glyphs; falls back to a typo if none."""
    idxs = [i for i, c in enumerate(sku) if c.upper() in _OCR]
    if not idxs:
        return typo_mutate(sku, rng)
    s = list(sku)
    for i in rng.sample(idxs, min(len(idxs), rng.randint(1, 2))):
        s[i] = _OCR[s[i].upper()]
    return ''.join(s)


def partial_spec(description: str, rng: random.Random) -> str:
    """Drop one word from the description — the caller who under-specifies."""
    words = description.split()
    if len(words) <= 2:
        return description
    drop = rng.randrange(len(words))
    return ' '.join(w for i, w in enumerate(words) if i != drop)


def _classify(res, original_upper: str, catalog_upper: set[str]) -> tuple[str, bool]:
    """Return (outcome, invented). outcome in resolved_original / resolved_other
    / pending / unresolvable. invented=True iff a resolved SKU or any surfaced
    candidate is NOT a real catalog row (a never-invent failure)."""
    invented = False
    if res.state == 'resolved' and res.sku is not None:
        if res.sku.upper() not in catalog_upper:
            invented = True
    for c in res.candidates:
        if c.sku.upper() not in catalog_upper:
            invented = True
    if res.state == 'resolved' and res.sku is not None:
        same = res.sku.upper() == original_upper
        return ('resolved_original' if same else 'resolved_other'), invented
    if res.state == 'pending_disambiguation':
        return 'pending', invented
    return 'unresolvable', invented


def _blank() -> dict:
    return {'total': 0, 'resolved_original': 0, 'resolved_other': 0,
            'pending': 0, 'unresolvable': 0, 'never_invent_failures': 0}


def run_audit(catalog, svc, skus, rng: random.Random) -> dict:
    """Perturb each SKU once per class, resolve via the live service, tally."""
    catalog_upper = {s.upper() for s in catalog.all_skus()}
    per_class = {c: _blank() for c in CLASSES}
    invent_examples: list[str] = []

    for sku in skus:
        row = catalog.lookup(sku)
        desc = getattr(row, 'description', '') or ''
        noisy = {
            'typo': typo_mutate(sku, rng),
            'ocr': ocr_confuse(sku, rng),
            'partial': partial_spec(desc, rng) if desc else sku,
        }
        for cls, text in noisy.items():
            res = svc.resolve(text)
            outcome, invented = _classify(res, sku.upper(), catalog_upper)
            b = per_class[cls]
            b['total'] += 1
            b[outcome] += 1
            if invented:
                b['never_invent_failures'] += 1
                if len(invent_examples) < 10:
                    invent_examples.append(f'{cls}:{text!r}->{res.sku!r}')

    totals = _blank()
    for b in per_class.values():
        for k, v in b.items():
            totals[k] += v

    def rates(b: dict) -> dict:
        t = b['total'] or 1
        resolved = b['resolved_original'] + b['resolved_other']
        graceful = b['pending'] + b['unresolvable']
        return {**b,
                'resolution_rate': round(resolved / t, 4),
                'graceful_degradation_rate': round(graceful / t, 4)}

    return {
        'seed': SEED,
        'catalog_size': len(catalog_upper),
        'sampled_skus': len(skus),
        'overall': rates(totals),
        'by_class': {c: rates(per_class[c]) for c in CLASSES},
        'never_invent_failures': totals['never_invent_failures'],
        'invent_examples': invent_examples,
        'ok': totals['never_invent_failures'] == 0,
    }


def main() -> int:
    catalog_path = os.environ.get(
        'SKU_CATALOG_PATH', str(REPO_ROOT / 'data' / 'catalog.csv'))
    catalog = FixtureCatalogIndex(catalog_path, tenant_id='audit')
    svc = ResolutionService(catalog, InMemoryStore(),
                            catalog_version=catalog_content_version(catalog_path))
    rng = random.Random(SEED)
    all_skus = sorted(catalog.all_skus())
    # Deterministic sample: large enough to be representative, bounded for speed.
    sample_n = int(os.environ.get('SKU_NOISE_SAMPLE', '400'))
    skus = rng.sample(all_skus, min(sample_n, len(all_skus)))

    report = run_audit(catalog, svc, skus, rng)

    o = report['overall']
    print(f'Noise-resilience audit — {report["sampled_skus"]} SKUs x '
          f'{len(CLASSES)} classes = {o["total"]} noisy inputs '
          f'(catalog {report["catalog_size"]}, seed {SEED})')
    print(f'{"class":<10} {"total":>6} {"->orig":>7} {"->other":>8} '
          f'{"pending":>8} {"unres":>6} {"resolve%":>9} {"graceful%":>10} {"invent":>7}')
    for c in CLASSES:
        b = report['by_class'][c]
        print(f'{c:<10} {b["total"]:>6} {b["resolved_original"]:>7} '
              f'{b["resolved_other"]:>8} {b["pending"]:>8} {b["unresolvable"]:>6} '
              f'{b["resolution_rate"]:>8.1%} {b["graceful_degradation_rate"]:>9.1%} '
              f'{b["never_invent_failures"]:>7}')
    print(f'{"OVERALL":<10} {o["total"]:>6} {o["resolved_original"]:>7} '
          f'{o["resolved_other"]:>8} {o["pending"]:>8} {o["unresolvable"]:>6} '
          f'{o["resolution_rate"]:>8.1%} {o["graceful_degradation_rate"]:>9.1%} '
          f'{o["never_invent_failures"]:>7}')
    print(f'\nnever-invent under noise: '
          f'{"PASS (0 inventions)" if report["ok"] else "FAIL"}')
    for ex in report['invent_examples']:
        print(f'  INVENTED: {ex}')

    out = REPO_ROOT / 'state' / 'noise_resilience_audit.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f'Summary written: {out.relative_to(REPO_ROOT)}')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
