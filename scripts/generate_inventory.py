#!/usr/bin/env python3
"""Generate the synthetic inventory layer (decision D4 in docs/DECISION_LOG.md).

Seeded and re-runnable: same catalog + same seed => byte-identical output.
Real signal in, synthetic state out: in-stock probability and stocking depth
are weighted by each SKU's REAL sales_count from the ERP export; the
quantity-on-hand and lead-time values are synthetic.

Output: data/inventory.json
    {
      "_meta": {seed, generated_from, total, in_stock, oos, ...},
      "records": {sku: {"qty_on_hand": int, "lead_time_days": int|null}}
    }
  qty_on_hand > 0  <=> lead_time_days is null   (in stock)
  qty_on_hand == 0 <=> lead_time_days >= 1      (restock path)
Every catalog SKU appears exactly once. No third state.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'src'))

from sku_translator import FixtureCatalogIndex

SEED = 20260606
TARGET_IN_STOCK = 0.85

# Lead-time bands (business days) per D4
STANDARD_BAND = (5, 10)
CUSTOM_BAND = (15, 30)


def main() -> int:
    catalog_path = REPO_ROOT / 'data' / 'catalog.csv'
    catalog = FixtureCatalogIndex(str(catalog_path), tenant_id='inventory_gen')
    rows = list(catalog.parsed_rows())
    total = len(rows)

    # Percentile-rank each SKU by real sales_count (ties broken by SKU string
    # for determinism). High velocity -> high rank -> stocked deeper, more
    # likely in stock.
    ordered = sorted(rows, key=lambda r: (r.raw_erp_row.get('sales_count') or 0, r.sku))
    rank = {r.sku: i / max(1, total - 1) for i, r in enumerate(ordered)}

    rng = random.Random(SEED)
    records: dict[str, dict] = {}
    in_stock = 0
    forced_oos = 0

    for r in sorted(rows, key=lambda r: r.sku):  # deterministic iteration
        needs_review = bool(
            (r.raw_parser_result or {}).get('requires_human_review')
        )
        if r.is_obsolete:
            # Obsolete-flagged: never in stock, long band (D4)
            lead = rng.randint(*CUSTOM_BAND)
            records[r.sku] = {'qty_on_hand': 0, 'lead_time_days': lead}
            forced_oos += 1
            continue

        # In-stock probability: linear in velocity rank, calibrated so the
        # mean across ranks ~= TARGET_IN_STOCK. p = base + spread*rank with
        # mean = base + spread/2.
        spread = 0.26
        base = TARGET_IN_STOCK - spread / 2
        if rng.random() < base + spread * rank[r.sku]:
            # Stock depth scales with velocity: top-velocity SKUs hold weeks
            # of supply, slow movers a handful of units.
            depth_scale = 1 + int(48 * rank[r.sku] ** 2)
            qty = max(1, int(rng.lognormvariate(0.0, 0.6) * depth_scale))
            records[r.sku] = {'qty_on_hand': qty, 'lead_time_days': None}
            in_stock += 1
        else:
            # ~15% of out-of-stock SKUs are long-lead custom/restock items
            # (CUSTOM_BAND), the rest are standard restock.
            custom = needs_review or rng.random() < 0.15
            band = CUSTOM_BAND if custom else STANDARD_BAND
            records[r.sku] = {'qty_on_hand': 0,
                              'lead_time_days': rng.randint(*band)}

    oos = total - in_stock
    meta = {
        'seed': SEED,
        'generated_from': 'data/catalog.csv (sales_count weighting)',
        'total': total,
        'in_stock': in_stock,
        'in_stock_fraction': round(in_stock / total, 4),
        'out_of_stock': oos,
        'obsolete_forced_oos': forced_oos,
        'lead_time_bands': {'standard': STANDARD_BAND, 'custom': CUSTOM_BAND},
    }
    out = REPO_ROOT / 'data' / 'inventory.json'
    out.write_text(json.dumps({'_meta': meta, 'records': records},
                              indent=1, sort_keys=True))
    print(json.dumps(meta, indent=2))
    print(f'written: {out.relative_to(REPO_ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
