"""``OrderChanges`` fake: orders by id, cancels and amends recorded, failures configurable (ticket 013)."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal

import anyio

from guarded_exchange.domain.account import (
    ConditionalKind,
    Order,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    Page,
)
from guarded_exchange.domain.credentials import ApiKeyPair
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.order_changes import OrderAmendment
from tests.fakes import InMemoryDatabase, make_order


def plain_order(
    order_id: str,
    *,
    side: OrderSide = OrderSide.BUY,
    qty: str = "0.002",
    price: str = "80000",
    origin: OrderOrigin = OrderOrigin.SYSTEM,
    category: Category = Category.LINEAR,
    status: OrderStatus = OrderStatus.NEW,
    filled: str = "0",
    reduce_only: bool = False,
    conditional: ConditionalKind | None = None,
) -> Order:
    base = make_order(
        order_id,
        category=category,
        origin=origin,
        client_order_id="gx-abc" if origin is OrderOrigin.SYSTEM else "manual-1",
    )
    return replace(
        base,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        status=status,
        filled_qty=Decimal(filled),
        reduce_only=reduce_only,
        conditional=conditional,
    )


@dataclass
class FakeOrderChanges:
    """The account's orders by id. ``cancel_order`` / ``amend_order`` record the request and, unless
    ``apply`` is false, change the stored order the way the exchange would (``apply_after_error``:
    also when they raise - an "unknown" that did happen). ``errors`` by order id makes them raise.

    With ``db`` set, ``calls_in_transaction`` counts calls made while a unit of work was open (D-10),
    and ``audit_at_send`` keeps the journal event types committed when each request was sent.
    """

    orders: dict[str, Order] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    apply: bool = True
    apply_after_error: bool = False
    hide_after_send: bool = False
    """The read-back does not list the order (yet): the exchange's lists lag behind."""
    list_error: Exception | None = None
    read_error: Exception | None = None
    has_more: bool = False
    cancels: list[str] = field(default_factory=list)
    amends: list[OrderAmendment] = field(default_factory=list)
    calls: list[tuple[object, ...]] = field(default_factory=list)
    db: InMemoryDatabase | None = None
    calls_in_transaction: int = 0
    audit_at_send: list[list[str]] = field(default_factory=list)
    hold_send: anyio.Event | None = None
    on_send: Callable[[], None] | None = None
    sent: bool = False
    sending: anyio.Event = field(default_factory=anyio.Event)
    """Set when a cancel or amend has been entered."""

    def add(self, *orders: Order) -> None:
        for order in orders:
            self.orders[order.order_id] = order

    async def get_order_by_id(
        self, keys: ApiKeyPair, category: Category, symbol: str, order_id: str
    ) -> Order | None:
        self._call("get_order_by_id", order_id)
        if self.read_error is not None and self.sent:
            raise self.read_error
        if self.hide_after_send and self.sent:
            return None
        order = self.orders.get(order_id)
        if order is None or (order.symbol, order.category) != (symbol, category):
            return None
        return order

    async def list_cancellable_orders(
        self, keys: ApiKeyPair, category: Category, symbol: str, limit: int
    ) -> Page[Order]:
        self._call("list_cancellable_orders", category, symbol, limit)
        if self.list_error is not None and self.sent:
            raise self.list_error
        found = tuple(
            o
            for o in self.orders.values()
            if o.symbol == symbol
            and o.category is category
            and o.conditional is None
            and o.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
        )
        return Page(items=found[:limit], has_more=self.has_more or len(found) > limit)

    async def cancel_order(self, keys: ApiKeyPair, category: Category, symbol: str, order_id: str) -> None:
        self._sending("cancel_order", order_id)
        self.cancels.append(order_id)
        if self.hold_send is not None:
            await self.hold_send.wait()
        error = self.errors.get(order_id)
        if self.apply and (error is None or self.apply_after_error) and order_id in self.orders:
            order = self.orders[order_id]
            status = OrderStatus.PARTIALLY_FILLED_CANCELLED if order.filled_qty > 0 else OrderStatus.CANCELLED
            self.orders[order_id] = replace(order, status=status)
        if error is not None:
            raise error

    async def amend_order(self, keys: ApiKeyPair, amendment: OrderAmendment) -> None:
        self._sending("amend_order", amendment.order_id)
        self.amends.append(amendment)
        if self.hold_send is not None:
            await self.hold_send.wait()
        error = self.errors.get(amendment.order_id)
        if self.apply and (error is None or self.apply_after_error) and amendment.order_id in self.orders:
            order = self.orders[amendment.order_id]
            self.orders[amendment.order_id] = replace(
                order,
                qty=order.qty if amendment.qty is None else amendment.qty,
                price=order.price if amendment.price is None else amendment.price,
            )
        if error is not None:
            raise error

    def _sending(self, *call: object) -> None:
        self._call(*call)
        self.sent = True
        self.sending.set()
        if self.db is not None:
            self.audit_at_send.append([r.event_type for r in self.db.all_audit()])
        if self.on_send is not None:
            self.on_send()

    def _call(self, *call: object) -> None:
        self.calls.append(call)
        if self.db is not None and self.db.open_transactions:
            self.calls_in_transaction += 1
