"""Risk direction of a trading action and the reduce-only clamp (SPEC FR-43, FR-15, FR-16; GP-2, GP-5).

The direction decides which limits apply: risk-increasing actions face the allowlist, the order
size and leverage limits and the kill switch; risk-reducing ones only the position size.
A reduce-only order can never grow or flip a position: its quantity is cut to the position.
"""

from decimal import Decimal
from enum import StrEnum

from guarded_exchange.domain.account import OrderSide, Position, PositionSide
from guarded_exchange.domain.actions import PlaceOrder
from guarded_exchange.domain.errors import PositionNotFound


class RiskDirection(StrEnum):
    INCREASES = "increases"
    REDUCES = "reduces"


def classify(action: PlaceOrder) -> RiskDirection:
    """Only a reduce-only order reduces risk. Any spot order counts as increasing (GP-5, strict:
    a spot sell swaps one asset for another and is not modelled as closing anything)."""
    return RiskDirection.REDUCES if action.reduce_only else RiskDirection.INCREASES


def classify_amend(
    *,
    side: OrderSide,
    reduce_only: bool,
    current_qty: Decimal,
    current_price: Decimal,
    new_qty: Decimal,
    new_price: Decimal,
) -> RiskDirection:
    """FR-43: changing an order increases risk if it gets a larger quantity or a less favourable
    price (a buy pays more, a sell receives less), otherwise it reduces risk. A reduce-only order
    stays reduce-only when changed: it can never grow or flip a position, whatever its new size.
    """
    if reduce_only:
        return RiskDirection.REDUCES
    worse_price = new_price > current_price if side is OrderSide.BUY else new_price < current_price
    if new_qty > current_qty or worse_price:
        return RiskDirection.INCREASES
    return RiskDirection.REDUCES


_REDUCING_SIDE = {PositionSide.LONG: OrderSide.SELL, PositionSide.SHORT: OrderSide.BUY}


def reduce_only_qty(action: PlaceOrder, position: Position | None) -> Decimal:
    """The quantity a reduce-only order may have: ``min(requested, position size)`` (FR-43).

    Raises ``PositionNotFound`` if there is no position on the symbol or the order's side would grow
    it (a buy against a long), since then there is nothing to reduce.
    """
    if position is None or position.symbol != action.symbol:
        raise PositionNotFound(
            f"there is no open {action.symbol} position to reduce",
            next_step="Check get_positions; a reduce-only order needs an open position on the symbol.",
            details={"symbol": action.symbol, "reason": "no_position"},
        )
    if _REDUCING_SIDE[position.side] is not action.side:
        raise PositionNotFound(
            f"a reduce-only {action.side.value} cannot reduce the {position.side.value} "
            f"{action.symbol} position",
            next_step=f"To reduce a {position.side.value} position use side "
            f"{_REDUCING_SIDE[position.side].value}.",
            details={
                "symbol": action.symbol,
                "reason": "side_would_increase",
                "position_side": position.side.value,
            },
        )
    return min(action.qty, position.size)
