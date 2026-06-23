"""Pattern dispatch + the public parse() entry point: walk PATTERNS in order,
return the first match's decoded fields."""
from __future__ import annotations

from typing import Any

from ._patterns import EXPLICIT_DISREGARD_REVIEW, FREETEXT_PREFIXES, PATTERNS


def _try_patterns(sku: str) -> dict[str, Any] | None:
    """Try each pattern in order. Return the first match's decoded dict.

    Three special non-regex checks run first:
      1. EXPLICIT_DISREGARD_REVIEW — hardcoded one-off SKUs
      2. FREETEXT_PREFIXES — non-product line items (PA-, RESTOCK-, etc.)
      3. NPI/test SKUs
    """
    # 1. Explicit disregard list
    if sku in EXPLICIT_DISREGARD_REVIEW:
        return {
            'pattern': 'explicit_disregard',
            'family': 'DISREGARD',
            'family_meaning': 'Explicit disregard (per SME)',
            'disregard': True,
            'requires_human_review': True,
            'disregard_reason': EXPLICIT_DISREGARD_REVIEW[sku],
        }

    # 2. Freetext / admin prefixes (non-product line items)
    sku_upper = sku.upper()
    for prefix in FREETEXT_PREFIXES:
        if sku_upper.startswith(prefix):
            return {
                'pattern': 'freetext_or_admin',
                'family': 'ADMIN',
                'family_meaning': 'Non-product line item',
                'disregard': True,
                'admin_prefix': prefix.strip(),
            }

    # 3. Regex pattern dispatch
    for name, regex, decoder in PATTERNS:
        if regex is None or decoder is None:
            continue  # placeholder entries
        m = regex.match(sku)
        if m:
            result = decoder(m)
            if result is not None:
                return result
    return None


# ============================================================================
# Public API
# ============================================================================

def parse(sku: str) -> dict[str, Any]:
    """Decode a catalog SKU string into a structured dict.

    Always returns a dict. Unrecognized inputs get pattern='unstructured'.
    """
    if not sku or not isinstance(sku, str):
        return {'part_number': sku, 'pattern': 'empty'}

    sku = sku.strip().upper()
    if not sku:
        return {'part_number': sku, 'pattern': 'empty'}

    result = _try_patterns(sku)
    if result is None:
        return {'part_number': sku, 'pattern': 'unstructured'}

    result['part_number'] = sku
    return result
