"""ship_date(): a pure, total function from (inventory record, quantity,
receipt timestamp) to a definitive ship-by promise.

For every catalog SKU and every tz-aware timestamp inside the calendar
horizon it returns a ShipDateResult; invalid input (naive timestamp, qty < 1,
an internally inconsistent record) raises ValueError loudly rather than
guessing. Each result names the rule that produced it via ``basis``, so a
promise is auditable back to a decision-log entry.

Rules (docs/DECISION_LOG.md, fixed before this module was written):
  D1  enough on hand   -> SHIP_BY_HOUR on the business day after receipt
  D2  partial on hand  -> SHIP_COMPLETE (default): whole line on the restock
                          path; SPLIT_SHIP: on-hand portion next business day,
                          remainder on the restock path, both dates carried
  D4  none on hand     -> SHIP_BY_HOUR on (receipt + lead_time_days BDs)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from fulfillment.calendar import (
    add_business_days,
    normalize_receipt,
    ship_by_datetime,
)

# A partial line that must restock has no SKU-specific lead time to use:
# in-stock records carry lead_time_days=None. Until inventory grows a restock
# lead for stocked SKUs (a deliberately-deferred schema change), partials fall
# back to the midpoint of the standard restock band.
DEFAULT_RESTOCK_LEAD_DAYS = 7


class PartialPolicy(Enum):
    SHIP_COMPLETE = 'ship_complete'
    SPLIT_SHIP = 'split_ship'


@dataclass(frozen=True)
class InventoryRecord:
    sku: str
    qty_on_hand: int
    lead_time_days: int | None  # None iff qty_on_hand > 0

    def __post_init__(self) -> None:
        if self.qty_on_hand < 0:
            raise ValueError(f'{self.sku}: negative qty_on_hand')
        if self.qty_on_hand > 0:
            if self.lead_time_days is not None:
                raise ValueError(
                    f'{self.sku}: in-stock record must not carry a lead time'
                )
        elif self.lead_time_days is None or self.lead_time_days < 1:
            raise ValueError(
                f'{self.sku}: out-of-stock record requires lead_time_days >= 1'
            )


@dataclass(frozen=True)
class SplitDetail:
    in_stock_qty: int
    in_stock_ship_by: datetime
    backorder_qty: int
    backorder_ship_by: datetime


@dataclass(frozen=True)
class ShipDateResult:
    sku: str
    qty: int
    ship_by: datetime          # the single definitive promise (line complete)
    basis: str                 # which rule fired — auditable to DECISION_LOG
    policy: str
    split: SplitDetail | None  # populated only for SPLIT_SHIP partials


def ship_date(
    record: InventoryRecord,
    qty: int,
    received_at: datetime,
    *,
    partial_policy: PartialPolicy = PartialPolicy.SHIP_COMPLETE,
) -> ShipDateResult:
    if qty < 1:
        raise ValueError(f'qty must be >= 1, got {qty}')
    receipt_day = normalize_receipt(received_at)  # raises on naive input (D3)

    def _result(ship_by: datetime, basis: str,
                split: SplitDetail | None = None) -> ShipDateResult:
        return ShipDateResult(record.sku, qty, ship_by, basis,
                              partial_policy.value, split)

    # D1 — enough on hand: next business day.
    if qty <= record.qty_on_hand:
        next_bd = ship_by_datetime(add_business_days(receipt_day, 1))
        return _result(next_bd, 'in_stock_next_business_day_1700')

    # D4 — none on hand: the SKU's restock lead time governs.
    if record.qty_on_hand == 0:
        restock = ship_by_datetime(
            add_business_days(receipt_day, record.lead_time_days)
        )
        return _result(restock, 'restock_lead_time')

    # D2 — partial on hand (0 < qty_on_hand < qty). The line-complete promise
    # is always the restock date; SPLIT_SHIP additionally carries the earlier
    # date the on-hand portion can leave on.
    backorder_ship_by = ship_by_datetime(
        add_business_days(receipt_day, DEFAULT_RESTOCK_LEAD_DAYS)
    )
    if partial_policy is PartialPolicy.SHIP_COMPLETE:
        return _result(backorder_ship_by, 'partial_ship_complete_restock')

    in_stock_ship_by = ship_by_datetime(add_business_days(receipt_day, 1))
    split = SplitDetail(
        in_stock_qty=record.qty_on_hand,
        in_stock_ship_by=in_stock_ship_by,
        backorder_qty=qty - record.qty_on_hand,
        backorder_ship_by=backorder_ship_by,
    )
    return _result(backorder_ship_by, 'partial_split_ship', split)


def load_inventory(path: str | Path) -> dict[str, InventoryRecord]:
    """Load an inventory JSON file into validated records.

    Validation happens in InventoryRecord.__post_init__, so a corrupt file
    fails loudly here at startup rather than at quote time.
    """
    raw = json.loads(Path(path).read_text())
    return {
        sku: InventoryRecord(sku=sku, qty_on_hand=rec['qty_on_hand'],
                             lead_time_days=rec['lead_time_days'])
        for sku, rec in raw['records'].items()
    }
