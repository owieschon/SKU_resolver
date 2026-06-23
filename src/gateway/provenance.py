"""Tool provenance — the structural truth a turn discloses (Phase 5 containment).

The fabrication-containment design routes every agent turn by what the TOOL
disclosed, never by reading the agent's prose. This module extracts that
provenance from a TurnResponse STRUCTURALLY:

  surfaced_skus   : every part number the turn put in play (tier-1 authoritative)
  surfaced_values : the BINDING-FACT values it disclosed, TYPED and this-turn
                    ({'unit_price': ..} | {'qty': .., 'ship_by': ..}); empty on
                    identify / candidates / refused (no-disclosure turns)

Containment-critical invariant (`assert_complete`): a reply whose text states a
BINDING-FACT value (a price, an N-on-hand/in-stock quantity, or a ship date) MUST
carry non-empty surfaced_values. Otherwise the router could see a disclosure as a
free turn and let the model author the fact. Incidental numbers — dimensions like
"5 by 24 inch" — are NOT binding facts and must not trip the invariant.
"""
from __future__ import annotations

import re

# BINDING-FACT value tokens only (not bare dimensions): a $ price, a spelled price,
# an N-on-hand / N in-stock quantity, or a ship date (Month Day / ISO).
_PRICE = r'\$\s?\d[\d,]*(?:\.\d{1,2})?|\b\d+(?:\.\d{1,2})?\s*(?:dollars?|cents?)\b'
_QTY = r'\b\d+\s*(?:on hand|in stock|available|left)\b'
_SHIP = (r'\b(?:january|february|march|april|may|june|july|august|september|'
         r'october|november|december)\s+\d{1,2}\b|\b\d{4}-\d{2}-\d{2}\b')
_BINDING_VALUE = re.compile(f'{_PRICE}|{_QTY}|{_SHIP}', re.I)


def has_binding_value_token(text: str) -> bool:
    return bool(_BINDING_VALUE.search(text or ''))


def surfaced(resp) -> tuple[tuple[str, ...], dict]:
    """Return (surfaced_skus, surfaced_values) for a TurnResponse — structurally,
    never by parsing the reply text."""
    skus: list[str] = []
    values: dict = {}
    av = getattr(resp, 'availability', None)
    if av is not None:
        skus.append(av.sku)
        values = {'qty': av.quantity_on_hand, 'in_stock': av.in_stock,
                  'ship_by': (av.ship_by_iso or '')[:10]}
    pr = getattr(resp, 'price', None)
    if pr is not None:
        skus.append(pr.sku)
        values = {'unit_price': pr.unit_price}        # pricing turn: the value is the price
    for c in (getattr(resp, 'candidates', None) or ()):
        skus.append(c.sku)
    # readback / single-pending identify carries its sku in meta (set structurally
    # by the orchestrator, not parsed from the readback text).
    meta_sku = (getattr(resp, 'meta', None) or {}).get('surfaced_sku')
    if meta_sku:
        skus.append(meta_sku)
    # dedup, preserve order
    seen, out = set(), []
    for s in skus:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out), values


def assert_complete(resp) -> None:
    """Provenance-completeness invariant: a reply that STATES a binding-fact value
    must carry non-empty surfaced_values. Raises if a disclosure under-reports —
    a disclosure must never be able to masquerade as a free turn."""
    _, values = surfaced(resp)
    if has_binding_value_token(getattr(resp, 'text', '') or '') and not values:
        raise AssertionError(
            'provenance under-report: reply states a binding-fact value but '
            f'surfaced_values is empty — kind={getattr(resp, "kind", "?")!r} '
            f'text={getattr(resp, "text", "")[:80]!r}')
