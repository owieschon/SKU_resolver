"""G5 price source — seeded synthetic price book (same discipline as the
synthetic inventory, D4): no real-tenant pricing in the repo. Keyed by
(sku, account_tier). Real per-account pricing is an ERP read (the Value Entry
gap — custom_api_page_required), gated in the production-validation doc.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class PriceBook(Protocol):
    def price(self, sku: str, tier: str) -> float | None: ...


@dataclass
class SyntheticPriceBook:
    """Deterministic synthetic prices. Generated from a seed + the sku string
    so the same catalog yields the same book — reproducible, not real."""
    base_by_sku: dict[str, float]
    tier_multiplier: dict[str, float]

    def price(self, sku: str, tier: str) -> float | None:
        base = self.base_by_sku.get(sku)
        if base is None:
            return None
        return round(base * self.tier_multiplier.get(tier, 1.0), 2)

    @classmethod
    def seeded(cls, skus: list[str], *, seed: int = 20260607) -> 'SyntheticPriceBook':
        # Deterministic pseudo-price from a stable hash of the sku (no RNG
        # state, so order-independent and reproducible).
        import hashlib
        base = {}
        for s in skus:
            h = int(hashlib.sha256(f'{seed}:{s}'.encode()).hexdigest()[:6], 16)
            base[s] = round(5.0 + (h % 50000) / 100.0, 2)   # $5.00–$505.00
        return cls(base_by_sku=base,
                   tier_multiplier={'standard': 1.0, 'preferred': 0.9,
                                    'distributor': 0.8})

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(
            {'base_by_sku': self.base_by_sku,
             'tier_multiplier': self.tier_multiplier}, indent=1, sort_keys=True))

    @classmethod
    def load(cls, path: Path) -> 'SyntheticPriceBook':
        d = json.loads(Path(path).read_text())
        return cls(base_by_sku=d['base_by_sku'],
                   tier_multiplier=d['tier_multiplier'])
