"""Cancelling and amending existing orders (SPEC FR-5, FR-10, FR-13, FR-15, FR-21, FR-43; PLAN D-21;
ticket 013).

Which orders an action may touch: only the ones this call has just read from the exchange and
labelled itself - one order by its exchange id, or the snapshot of plain open orders on an
instrument for "cancel all". External orders (placed by a person or another bot) are allowed, as
FR-5 says, and are marked as such in the answer and the journal (FR-21). Conditional orders and a
position's TP/SL are not plain orders: they are left alone here (TP/SL is ``set_trading_stop``,
ticket 014), so "cancel all" can never strip a position of its stop.

The plans are pure: an ``AmendPlan`` sizes and prices the change against the instrument rules and
classifies it (FR-43: a larger quantity or a less favourable price increases risk); a
``CancelPlan`` / ``CancelAllPlan`` name the orders and their origin.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from guarded_exchange.domain.account import (
    Order,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    Page,
    Position,
    PositionSide,
    QtyUnit,
)
from guarded_exchange.domain.actions import AmendOrder, CancelAllOrders, CancelOrder
from guarded_exchange.domain.audit import canonical_decimal
from guarded_exchange.domain.errors import (
    InstrumentRuleViolation,
    InvalidParams,
    NotFound,
    PositionNotFound,
)
from guarded_exchange.domain.instruments import Category, InstrumentRules
from guarded_exchange.domain.money import exact_product, round_down
from guarded_exchange.domain.orders import PriceBand, QtyAdjustment, check_limit_price, require_status
from guarded_exchange.domain.risk import RiskDirection, classify_amend

CANCEL_ALL_MAX: Final = 20
"""At most this many orders per ``cancel_all_orders`` call: each is one request to the exchange, sent
one after another within one overall deadline; more are reported (``has_more``), call again."""

OPEN_STATUSES: Final = frozenset({OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED})
"""A plain order that can still be cancelled or amended."""

_REDUCING_SIDE: Final = {PositionSide.LONG: OrderSide.SELL, PositionSide.SHORT: OrderSide.BUY}


def _s(value: Decimal) -> str:
    return canonical_decimal(value)


@dataclass(frozen=True, slots=True)
class TouchedOrder:
    """An order an action is about to change, as the exchange showed it to this call."""

    order_id: str
    symbol: str
    category: Category
    side: OrderSide
    origin: OrderOrigin
    status: OrderStatus
    qty: Decimal
    filled_qty: Decimal
    price: Decimal | None
    reduce_only: bool

    @property
    def external(self) -> bool:
        return self.origin is OrderOrigin.EXTERNAL

    @classmethod
    def of(cls, order: Order) -> "TouchedOrder":
        return cls(
            order_id=order.order_id,
            symbol=order.symbol,
            category=order.category,
            side=order.side,
            origin=order.origin,
            status=order.status,
            qty=order.qty,
            filled_qty=order.filled_qty,
            price=order.price,
            reduce_only=order.reduce_only,
        )


def require_changeable(order: Order | None, *, symbol: str, category: Category, order_id: str) -> Order:
    """The order if it is a plain open order of this instrument, else why not (nothing is sent).

    ``not_found`` covers both "no such order" and "no longer open": either way there is nothing to
    change, and the agent is told where to look.
    """
    if order is None or (order.order_id, order.symbol, order.category) != (order_id, symbol, category):
        raise NotFound(
            f"there is no order with this order_id on {symbol} ({category.value})",
            next_step="Check get_open_orders for the order_id, symbol and category of the order.",
            details={"reason": "order_not_found"},
        )
    if order.conditional is not None:
        raise InvalidParams(
            "this is a conditional order (a stop, take-profit or trailing stop); only plain open orders "
            "can be cancelled or amended here",
            next_step="Leave it, or tell the user to change it on the exchange; a position's TP/SL is "
            "changed with set_trading_stop.",
            details={"reason": "conditional_order", "conditional": order.conditional.value},
        )
    if order.status not in OPEN_STATUSES:
        raise NotFound(
            f"the order is not open any more (status {order.status.value}); nothing was changed",
            next_step="Check get_order_history for what happened to it; do not retry.",
            details={"reason": "order_not_open", "order_status": order.status.value},
        )
    return order


# --- cancel ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CancelPlan:
    """Cancel exactly this order. A cancel always reduces risk (FR-43)."""

    action: CancelOrder
    order: TouchedOrder

    risk = RiskDirection.REDUCES


def plan_cancel(action: CancelOrder, order: Order | None) -> CancelPlan:
    found = require_changeable(
        order, symbol=action.symbol, category=action.category, order_id=action.order_id
    )
    return CancelPlan(action, TouchedOrder.of(found))


@dataclass(frozen=True, slots=True)
class CancelAllPlan:
    """The snapshot of plain open orders this call cancels, one by one (never "whatever is open when
    the request arrives"): exactly the orders read and labelled here are touched."""

    action: CancelAllOrders
    orders: tuple[TouchedOrder, ...]
    has_more: bool
    """More plain open orders than ``CANCEL_ALL_MAX`` (or than one page): call again for the rest."""

    risk = RiskDirection.REDUCES

    @property
    def external_count(self) -> int:
        return sum(1 for order in self.orders if order.external)


def plan_cancel_all(action: CancelAllOrders, page: Page[Order]) -> CancelAllPlan:
    """The plain open orders of the instrument in ``page``, at most ``CANCEL_ALL_MAX``. The exchange
    was asked for plain orders only; anything else in the page (a conditional order, another
    instrument, a closed order) is left out here again rather than trusted."""
    eligible = [
        order
        for order in page.items
        if order.symbol == action.symbol
        and order.category is action.category
        and order.conditional is None
        and order.status in OPEN_STATUSES
    ]
    chosen = tuple(TouchedOrder.of(order) for order in eligible[:CANCEL_ALL_MAX])
    return CancelAllPlan(action, chosen, has_more=page.has_more or len(eligible) > CANCEL_ALL_MAX)


# --- amend ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AmendPlan:
    """The order after the change, and why. ``qty`` and ``price`` are the order's new total quantity
    and limit price (unchanged ones as they are now); ``notional = qty x price`` is what the risk
    limits and the confirmation threshold see (conservative: the whole order, filled part included).
    """

    action: AmendOrder
    order: TouchedOrder
    risk: RiskDirection
    qty: Decimal
    qty_adjustment: QtyAdjustment | None
    price: Decimal
    notional: Decimal
    position_size: Decimal | None = None

    @property
    def changes_qty(self) -> bool:
        return self.qty != self.order.qty

    @property
    def changes_price(self) -> bool:
        return self.price != self.order.price


def plan_amend(
    action: AmendOrder,
    order: Order | None,
    rules: InstrumentRules,
    *,
    band: PriceBand | None,
    position: Position | None,
) -> AmendPlan:
    """Size and price the change or raise (nothing is sent on any error).

    ``band`` is needed when the price changes, ``position`` when a reduce-only order grows.
    """
    if (rules.instrument.symbol, rules.instrument.category) != (action.symbol, action.category):
        raise ValueError("rules of another instrument")
    current = require_changeable(
        order, symbol=action.symbol, category=action.category, order_id=action.order_id
    )
    if current.order_type is not OrderType.LIMIT or current.price is None or current.qty_unit is not QtyUnit.BASE:
        raise InvalidParams(
            "only a resting limit order can be amended",
            next_step="Cancel the order instead, or place a new one.",
            details={"reason": "not_a_limit_order"},
        )
    price = current.price
    if action.price is not None:
        if band is None:
            raise ValueError("a new price needs the price band")
        price = check_limit_price(current.side, action.price, rules, band)
    qty, adjustment, position_size = current.qty, None, None
    if action.qty is not None:
        qty, adjustment, position_size = _new_qty(action.qty, current, rules, position)
    if qty == current.qty and price == current.price:
        raise InvalidParams(
            "the order already has this qty and price; nothing to change",
            next_step="Check get_open_orders; pass a different qty or price to change it.",
            details={"reason": "no_change"},
        )
    risk = classify_amend(
        side=current.side,
        reduce_only=current.reduce_only,
        current_qty=current.qty,
        current_price=current.price,
        new_qty=qty,
        new_price=price,
    )
    require_status(rules, risk)
    notional = exact_product(qty, price)
    if not current.reduce_only:  # as for a new order: closing a remainder must stay possible
        _require_min_notional(notional, rules)
    return AmendPlan(
        action=action,
        order=TouchedOrder.of(current),
        risk=risk,
        qty=qty,
        qty_adjustment=adjustment,
        price=price,
        notional=notional,
        position_size=position_size,
    )


def _new_qty(
    requested: Decimal, order: Order, rules: InstrumentRules, position: Position | None
) -> tuple[Decimal, QtyAdjustment | None, Decimal | None]:
    """Rounded down to the step (FR-10); a reduce-only order that grows is cut so that what is left of
    it stays within the position (FR-43): ``filled + position size`` at most."""
    wanted, adjustment, position_size = requested, None, None
    if order.reduce_only and requested > order.qty:
        if position is None or position.symbol != order.symbol or _REDUCING_SIDE[position.side] is not order.side:
            raise PositionNotFound(
                f"there is no {order.symbol} position this reduce-only order could reduce",
                next_step="Check get_positions; a reduce-only order can only grow up to the position.",
                details={"symbol": order.symbol, "reason": "no_position"},
            )
        position_size = position.size
        bound = order.filled_qty + position.size
        if wanted > bound:
            wanted, adjustment = bound, QtyAdjustment.CLAMPED_TO_POSITION
    qty = round_down(wanted, rules.qty_step)
    if qty < wanted and adjustment is None:
        adjustment = QtyAdjustment.ROUNDED_DOWN
    if qty <= 0 or qty < rules.min_qty:
        raise InstrumentRuleViolation(
            f"qty rounded down to the step {_s(rules.qty_step)} is {_s(qty)}, below the minimum "
            f"{_s(rules.min_qty)} {rules.instrument.base}",
            next_step=f"Use at least {_s(max(rules.min_qty, rules.qty_step))} {rules.instrument.base}, "
            f"a multiple of {_s(rules.qty_step)}.",
            details={
                "rule": "min_qty",
                "field": "qty",
                "min_qty": _s(rules.min_qty),
                "qty_step": _s(rules.qty_step),
                "rounded_qty": _s(qty),
            },
        )
    if qty <= order.filled_qty:
        raise InvalidParams(
            f"the new qty {_s(qty)} must be above what is already filled ({_s(order.filled_qty)})",
            next_step="qty is the order's new total quantity; to stop the rest from filling, cancel the "
            "order.",
            details={"field": "qty", "reason": "not_above_filled", "filled_qty": _s(order.filled_qty)},
        )
    return qty, adjustment, position_size


def _require_min_notional(order_value: Decimal, rules: InstrumentRules) -> None:
    minimum = rules.min_notional
    if minimum is not None and order_value < minimum:
        raise InstrumentRuleViolation(
            f"order value {_s(order_value)} {rules.instrument.quote} is below the instrument's minimum "
            f"{_s(minimum)}",
            next_step=f"Keep qty x price at least {_s(minimum)} {rules.instrument.quote}.",
            details={
                "rule": "min_notional",
                "field": "qty",
                "min_notional": _s(minimum),
                "notional": _s(order_value),
            },
        )


# --- what goes to the exchange (port types) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderAmendment:
    """One amend request: only the fields that change are sent (``None``: leave as is)."""

    symbol: str
    category: Category
    order_id: str
    qty: Decimal | None
    price: Decimal | None

    @classmethod
    def from_plan(cls, plan: AmendPlan) -> "OrderAmendment":
        return cls(
            symbol=plan.action.symbol,
            category=plan.action.category,
            order_id=plan.action.order_id,
            qty=plan.qty if plan.changes_qty else None,
            price=plan.price if plan.changes_price else None,
        )


def amend_applied(plan: AmendPlan, order: Order | None) -> bool:
    """D-21: the order as read back carries the new values (the exchange applies amends
    asynchronously: "accepted" is not "applied")."""
    if order is None or order.order_id != plan.order.order_id:
        return False
    return (not plan.changes_qty or order.qty == plan.qty) and (
        not plan.changes_price or order.price == plan.price
    )
