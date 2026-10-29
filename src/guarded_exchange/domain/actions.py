"""Trading actions (SPEC 4.2, PLAN 5.1).

``ActionKind`` names every kind (the policy's confirmation rule uses it, FR-19). The action objects
arrive with their tickets: ``PlaceOrder`` (011) for now. An action holds exactly what the agent
asked for, normalised and checked for shape; sizing against the instrument and the market is the
order plan's job (``domain.orders``).
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from guarded_exchange.domain.account import OrderSide
from guarded_exchange.domain.errors import InvalidParams
from guarded_exchange.domain.instruments import Category, normalize_symbol, parse_category
from guarded_exchange.domain.money import parse_amount


class ActionKind(StrEnum):
    PLACE_ORDER = "place_order"
    AMEND_ORDER = "amend_order"
    CANCEL_ORDER = "cancel_order"
    CANCEL_ALL_ORDERS = "cancel_all_orders"
    SET_TRADING_STOP = "set_trading_stop"
    CLOSE_POSITION = "close_position"
    SET_LEVERAGE = "set_leverage"


ALL_ACTION_KINDS = frozenset(ActionKind)


class OrderKind(StrEnum):
    """What the agent asks for. A market order is sent as Limit IOC at the worst price (ADR-010)."""

    MARKET = "market"
    LIMIT = "limit"


@dataclass(frozen=True, slots=True)
class PlaceOrder:
    """A new order as requested: ``qty`` in the base asset, ``price`` (limit only) in USDT.

    ``reduce_only`` exists for perpetuals only (FR-4): such an order can only shrink a position.
    """

    symbol: str
    category: Category
    side: OrderSide
    order_type: OrderKind
    qty: Decimal
    price: Decimal | None
    reduce_only: bool = False

    kind = ActionKind.PLACE_ORDER

    def __post_init__(self) -> None:
        if not self.qty.is_finite() or self.qty <= 0:
            raise InvalidParams("qty must be greater than zero", details={"field": "qty"})
        if self.order_type is OrderKind.LIMIT and self.price is None:
            raise InvalidParams(
                "a limit order needs a price",
                next_step="Pass price (USDT, a multiple of the tick size), or use order_type market.",
                details={"field": "price", "reason": "required_for_limit"},
            )
        if self.order_type is OrderKind.MARKET and self.price is not None:
            raise InvalidParams(
                "a market order takes no price: it executes at the best price within the policy's slippage",
                next_step="Omit price for a market order, or use order_type limit to set the price.",
                details={"field": "price", "reason": "not_for_market"},
            )
        if self.price is not None and (not self.price.is_finite() or self.price <= 0):
            raise InvalidParams("price must be greater than zero", details={"field": "price"})
        if self.reduce_only and self.category is not Category.LINEAR:
            raise InvalidParams(
                "reduce_only applies to perpetuals (linear) only",
                next_step="Omit reduce_only for spot; to sell a spot holding, place a sell order.",
                details={"field": "reduce_only", "reason": "linear_only"},
            )


def parse_side(raw: str) -> OrderSide:
    try:
        return OrderSide(raw.strip().lower())
    except ValueError:
        raise InvalidParams("side must be buy or sell", details={"field": "side"}) from None


def parse_order_kind(raw: str) -> OrderKind:
    try:
        return OrderKind(raw.strip().lower())
    except ValueError:
        raise InvalidParams("order_type must be market or limit", details={"field": "order_type"}) from None


def parse_place_order(
    *,
    symbol: str,
    category: str,
    side: str,
    order_type: str,
    qty: str,
    price: str | None,
    reduce_only: bool,
) -> PlaceOrder:
    """A ``PlaceOrder`` from raw text input (every entrypoint goes through here)."""
    return PlaceOrder(
        symbol=normalize_symbol(symbol),
        category=parse_category(category),
        side=parse_side(side),
        order_type=parse_order_kind(order_type),
        qty=parse_amount("qty", qty),
        price=None if price is None else parse_amount("price", price),
        reduce_only=reduce_only,
    )
