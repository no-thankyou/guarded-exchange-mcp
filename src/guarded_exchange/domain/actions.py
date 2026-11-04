"""Trading actions (SPEC 4.2, PLAN 5.1).

``ActionKind`` names every kind (the policy's confirmation rule uses it, FR-19). The action objects
arrive with their tickets: ``PlaceOrder`` (011), ``CancelOrder``, ``AmendOrder`` and
``CancelAllOrders`` (013). An action holds exactly what the agent asked for, normalised and checked
for shape; sizing against the instrument and the market is the plan's job (``domain.orders``,
``domain.order_changes``).
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

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


# --- changing existing orders (ticket 013: FR-5, FR-43) -------------------------------------------

ORDER_ID_PATTERN: Final = r"^[A-Za-z0-9-]{1,64}$"
"""An exchange order id as ``get_open_orders`` shows it (Bybit: a UUID; spot: digits)."""
_ORDER_ID_RE: Final = re.compile(ORDER_ID_PATTERN)


def parse_order_id(raw: str) -> str:
    """The exchange order id as given (exact) or ``InvalidParams``; the message never quotes it."""
    value = raw.strip()
    if not _ORDER_ID_RE.fullmatch(value):
        raise InvalidParams(
            "order_id must be the exchange order id: 1-64 letters, digits or -",
            next_step="Take order_id from get_open_orders (not the client_order_id).",
            details={"field": "order_id"},
        )
    return value


@dataclass(frozen=True, slots=True)
class CancelOrder:
    """Cancel one open order of the account, ours or external (FR-5)."""

    symbol: str
    category: Category
    order_id: str

    kind = ActionKind.CANCEL_ORDER


@dataclass(frozen=True, slots=True)
class AmendOrder:
    """Change the quantity and/or the limit price of one open order (FR-5). ``None``: unchanged.

    ``qty`` is the order's new **total** quantity in the base asset (what is already filled counts).
    """

    symbol: str
    category: Category
    order_id: str
    qty: Decimal | None
    price: Decimal | None

    kind = ActionKind.AMEND_ORDER

    def __post_init__(self) -> None:
        if self.qty is None and self.price is None:
            raise InvalidParams(
                "nothing to change: pass qty, price or both",
                next_step="Pass the new total qty and/or the new limit price of the order.",
                details={"reason": "no_change"},
            )
        for name, value in (("qty", self.qty), ("price", self.price)):
            if value is not None and (not value.is_finite() or value <= 0):
                raise InvalidParams(f"{name} must be greater than zero", details={"field": name})


@dataclass(frozen=True, slots=True)
class CancelAllOrders:
    """Cancel every plain open order on one instrument, ours and external (FR-5).

    Conditional orders and a position's TP/SL are not plain orders and are left alone (ticket 013).
    """

    symbol: str
    category: Category

    kind = ActionKind.CANCEL_ALL_ORDERS


def parse_cancel_order(*, symbol: str, category: str, order_id: str) -> CancelOrder:
    return CancelOrder(
        symbol=normalize_symbol(symbol), category=parse_category(category), order_id=parse_order_id(order_id)
    )


def parse_amend_order(
    *, symbol: str, category: str, order_id: str, qty: str | None, price: str | None
) -> AmendOrder:
    return AmendOrder(
        symbol=normalize_symbol(symbol),
        category=parse_category(category),
        order_id=parse_order_id(order_id),
        qty=None if qty is None else parse_amount("qty", qty),
        price=None if price is None else parse_amount("price", price),
    )


def parse_cancel_all_orders(*, symbol: str, category: str) -> CancelAllOrders:
    return CancelAllOrders(symbol=normalize_symbol(symbol), category=parse_category(category))
