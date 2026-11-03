"""In-memory ``AccountHistoryReader`` and history builders (ticket 010).

Kept apart from ``tests/fakes.py`` so the history slice does not touch the shared fakes.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal

from guarded_exchange.domain.account import Order, OrderOrigin, OrderSide, OrderStatus, UntrustedText
from guarded_exchange.domain.credentials import ApiKeyPair
from guarded_exchange.domain.history import ExecutionKind, HistoryPage, HistoryPeriod, Trade
from guarded_exchange.domain.instruments import Category
from tests.fakes import make_order

_AT = datetime(2026, 10, 27, 11, 0, tzinfo=UTC)


def make_closed_order(
    order_id: str = "fa7b555a-dad9-444c-8f67-e4fc7572ac65",
    *,
    status: OrderStatus = OrderStatus.FILLED,
    client_order_id: str | None = "gx-0123456789abcdef0123456789abcdef",
    origin: OrderOrigin = OrderOrigin.SYSTEM,
    reject_reason: str | None = None,
) -> Order:
    order = make_order(order_id, client_order_id=client_order_id, origin=origin)
    return replace(
        order,
        status=status,
        filled_qty=order.qty if status is OrderStatus.FILLED else Decimal(0),
        reject_reason=None if reject_reason is None else UntrustedText(reject_reason),
    )


def make_trade(
    trade_id: str = "9c8e7f6a-1b2c-4d3e-8f9a-0b1c2d3e4f5a",
    *,
    client_order_id: str | None = "gx-0123456789abcdef0123456789abcdef",
    origin: OrderOrigin = OrderOrigin.SYSTEM,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        order_id="fa7b555a-dad9-444c-8f67-e4fc7572ac65",
        symbol="BTCUSDT",
        category=Category.LINEAR,
        side=OrderSide.BUY,
        kind=ExecutionKind.TRADE,
        price=Decimal("68797.6"),
        qty=Decimal("0.001"),
        value=Decimal("68.7976"),
        fee=Decimal("0.0378"),
        fee_currency="USDT",
        fee_rate=Decimal("0.00055"),
        is_maker=False,
        closed_size=None,
        executed_at=_AT,
        origin=origin,
        client_order_id=None if client_order_id is None else UntrustedText(client_order_id),
    )


@dataclass
class FakeHistory:
    """``AccountHistoryReader`` serving fixed pages keyed by exchange cursor (``None``: the first page).

    ``calls`` records ``(method, keys, category, symbol, period, exchange_cursor, limit)``.
    """

    orders: dict[str | None, HistoryPage[Order]] = field(default_factory=dict)
    trades: dict[str | None, HistoryPage[Trade]] = field(default_factory=dict)
    fail_with: Exception | None = None
    calls: list[tuple[object, ...]] = field(default_factory=list)

    async def get_order_history(
        self,
        keys: ApiKeyPair,
        category: Category,
        *,
        symbol: str | None,
        period: HistoryPeriod,
        exchange_cursor: str | None,
        limit: int,
    ) -> HistoryPage[Order]:
        self.calls.append(("get_order_history", keys, category, symbol, period, exchange_cursor, limit))
        if self.fail_with is not None:
            raise self.fail_with
        return self.orders.get(exchange_cursor, HistoryPage(items=(), next_exchange_cursor=None))

    async def get_trades(
        self,
        keys: ApiKeyPair,
        category: Category,
        *,
        symbol: str | None,
        period: HistoryPeriod,
        exchange_cursor: str | None,
        limit: int,
    ) -> HistoryPage[Trade]:
        self.calls.append(("get_trades", keys, category, symbol, period, exchange_cursor, limit))
        if self.fail_with is not None:
            raise self.fail_with
        return self.trades.get(exchange_cursor, HistoryPage(items=(), next_exchange_cursor=None))
