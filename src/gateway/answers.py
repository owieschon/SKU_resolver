"""G4 availability/lead-time (ungated) and G5 pricing (gated).

Availability derives entirely from fulfillment.ship_date — the gateway does
no date math (import-graph enforced by the gateway purity test). Pricing is
callable only with a granted AuthorizationDecision, re-checked here as a
second layer independent of the session gate (#10, defense in depth).
"""
from __future__ import annotations

from fulfillment import (
    CalendarHorizonError,
    InventoryRecord,
    PartialPolicy,
    ship_date,
)
from gateway.models import (
    AuthorizationDecision,
    AvailabilityAnswer,
    PriceAnswer,
)
from gateway.pricebook import PriceBook

_MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
           'August', 'September', 'October', 'November', 'December')


def _spoken_date(iso: str) -> str:
    """'2026-06-09' -> 'June 9th' — a date a person would say, not an ISO string
    a TTS engine reads as 'two thousand twenty-six'. String-only (no date math,
    so gateway purity holds)."""
    try:
        _, m, d = iso[:10].split('-')
        day = int(d)
        suffix = ('th' if 11 <= day <= 13
                  else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th'))
        return f'{_MONTHS[int(m) - 1]} {day}{suffix}'
    except Exception:
        return iso[:10]


def _plain_availability(sku: str, in_stock: bool, qty: int, ship_iso: str) -> str:
    # Spoken like a parts-counter person, not a shipping policy. Facts stay
    # exact; the structured AvailabilityAnswer carries the precise ISO date for
    # any caller that needs it.
    if in_stock:
        # BOOLEAN availability (CONVERSATION_STATE_SPEC §7, invariant 5): the
        # on-hand count is internal state and is never spoken — the structured
        # AvailabilityAnswer still carries qty for internal use; the say does not.
        return (f"Yep, the {sku} is in stock. It ships by "
                f"5 PM the next business day.")
    return (f"The {sku} is out of stock at the moment — it's on a restock. "
            f"The next ship date is {_spoken_date(ship_iso)}.")


def availability(sku: str, *, inventory: dict[str, InventoryRecord],
                 received_at, catalog_version: str, qty: int = 1
                 ) -> AvailabilityAnswer | None:
    rec = inventory.get(sku)
    if rec is None:
        return None
    try:
        res = ship_date(rec, qty, received_at,
                        partial_policy=PartialPolicy.SHIP_COMPLETE)
    except CalendarHorizonError:
        # Accurate refusal rather than a guessed date past the calendar table.
        return AvailabilityAnswer(
            sku=sku, in_stock=rec.qty_on_hand > 0,
            quantity_on_hand=rec.qty_on_hand, ship_by_iso='',
            basis='beyond_calendar_horizon',
            plain=f"I can't quote a ship date for {sku} that far out yet.",
            catalog_version=catalog_version)
    ship_iso = res.ship_by.isoformat()
    return AvailabilityAnswer(
        sku=sku, in_stock=rec.qty_on_hand > 0,
        quantity_on_hand=rec.qty_on_hand, ship_by_iso=ship_iso,
        basis=res.basis,
        plain=_plain_availability(sku, rec.qty_on_hand > 0,
                                  rec.qty_on_hand, ship_iso),
        catalog_version=catalog_version)


class PricingRefused(Exception):
    """Raised when pricing is requested without a granted authorization. The
    refusal is loud and named (regime: IRREVERSIBLE-ACTION/GUARD)."""


def pricing(sku: str, auth: AuthorizationDecision, *, pricebook: PriceBook,
            account_tier_of) -> PriceAnswer:
    # Second, independent gate (#10 defense-in-depth): even if the session
    # gate were bypassed, the pricing service itself refuses without a grant.
    if not auth.granted or auth.source in ('unverified', 'cross_account_denied'):
        raise PricingRefused(
            f'pricing refused for {sku}: authorization not granted '
            f'(source={auth.source})')
    tier = account_tier_of(auth.account_id)
    price = pricebook.price(sku, tier)
    if price is None:
        raise PricingRefused(f'no price on file for {sku} at tier {tier}')
    return PriceAnswer(
        sku=sku, account_id=auth.account_id, unit_price=price,
        source=auth.source,
        plain=f"For your account, {sku} is ${price:.2f} each.")
