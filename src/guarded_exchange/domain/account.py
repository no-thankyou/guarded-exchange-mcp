"""Account state as the agent reads it: balances, perpetual positions, open orders (SPEC FR-2,
FR-3, FR-41; PLAN D-26).

Money and quantities are ``Decimal``, time is UTC-aware. Every text value here is either one of
our own enums, a symbol or coin code that passed a strict pattern, or an exchange identifier. Text
that someone outside the system chose (an order's client id set by another bot or a person) is
wrapped in ``UntrustedText`` so the interface can only show it as labelled data (FR-41).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.market import bounded_count

DEFAULT_ACCOUNT_ITEMS: Final = 20
MAX_ACCOUNT_ITEMS: Final = 50
"""FR-3: more positions or open orders than this in one answer is noise; narrow by symbol instead."""

MAX_BALANCE_COINS: Final = 50
"""FR-3: coins beyond the 50 largest by USD value are counted, not listed (dust from airdrops etc.)."""


def account_items_limit(raw: object) -> int:
    """Validate the requested number of positions or orders (1..``MAX_ACCOUNT_ITEMS``)."""
    return bounded_count(raw, "limit", MAX_ACCOUNT_ITEMS)


@dataclass(frozen=True, slots=True)
class UntrustedText:
    """Free text chosen outside this system (FR-41). Never part of our own messages or decisions."""

    value: str


# --- balances --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoinBalance:
    """One asset of the trading account. Amounts are in units of ``coin``, ``usd_value`` in USD.

    ``locked`` is held by open spot orders; ``equity`` includes unrealised PnL of perpetuals settled
    in this coin. ``None``: the exchange did not report the value for this account mode.
    """

    coin: str
    wallet_balance: Decimal
    equity: Decimal | None
    locked: Decimal | None
    unrealised_pnl: Decimal | None
    usd_value: Decimal | None


@dataclass(frozen=True, slots=True)
class AccountBalances:
    """The trading account: totals in USD and the coins it holds, largest USD value first."""

    total_equity_usd: Decimal | None
    total_wallet_balance_usd: Decimal | None
    total_available_usd: Decimal | None
    coins: tuple[CoinBalance, ...]
    observed_at: datetime

    def largest(self, count: int) -> "tuple[tuple[CoinBalance, ...], int]":
        """The ``count`` coins with the highest USD value (unknown value last) and how many are left out."""
        ranked = sorted(self.coins, key=lambda c: (c.usd_value is None, -(c.usd_value or Decimal(0)), c.coin))
        return tuple(ranked[:count]), max(0, len(ranked) - count)


# --- positions -------------------------------------------------------------------------------


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True, slots=True)
class Position:
    """An open USDT perpetual position in one-way mode (v1). Prices and PnL in USDT, ``size`` in the
    base asset, always positive (``side`` gives the direction)."""

    symbol: str
    side: PositionSide
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal | None
    liquidation_price: Decimal | None
    unrealised_pnl: Decimal | None
    position_value: Decimal | None
    leverage: Decimal | None
    take_profit: Decimal | None
    stop_loss: Decimal | None
    updated_at: datetime | None

    def __post_init__(self) -> None:
        if not self.size.is_finite() or self.size <= 0:
            raise ValueError("an open position has a positive size")


# --- orders ----------------------------------------------------------------------------------


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    UNKNOWN = "unknown"


class OrderStatus(StrEnum):
    """Open: ``NEW``, ``PARTIALLY_FILLED``, ``UNTRIGGERED``; the rest are final (order history)."""

    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    UNTRIGGERED = "untriggered"
    """A conditional order (stop, take-profit) waiting for its trigger price."""
    FILLED = "filled"
    CANCELLED = "cancelled"
    """Cancelled without any fill (by the user, the exchange or an IOC/FOK remainder)."""
    PARTIALLY_FILLED_CANCELLED = "partially_filled_cancelled"
    """Cancelled after part of it was filled; ``filled_qty`` says how much."""
    REJECTED = "rejected"
    TRIGGERED = "triggered"
    """A conditional order whose trigger fired; it became a plain order."""
    DEACTIVATED = "deactivated"
    """A conditional order (or attached TP/SL) cancelled before it was triggered."""
    UNKNOWN = "unknown"


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    POST_ONLY = "post_only"
    UNKNOWN = "unknown"


class ConditionalKind(StrEnum):
    """Why an order waits for a trigger; ``None`` on the order means a plain order."""

    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    STOP = "stop"
    OTHER = "other"


class OrderOrigin(StrEnum):
    """Who created an order (FR-2): this system, or anyone else on the account (a person, a bot).

    Today ``SYSTEM`` means "the client order id has this system's prefix"; it is not yet verified
    against our own record of sent orders (tickets 011/012).
    """

    SYSTEM = "system"
    EXTERNAL = "external"


class QtyUnit(StrEnum):
    """What an order's ``qty`` counts. A spot market buy is usually sized in the quote currency
    (spend 20 USDT); ``UNKNOWN`` when the exchange did not say - never guessed as base."""

    BASE = "base"
    QUOTE = "quote"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Order:
    """An open or (order history) closed order. Prices in the quote currency of the pair, ``qty``
    in ``qty_unit``, ``filled_qty`` in the base asset.

    ``client_order_id`` is untrusted: an external order's id is free text chosen by whoever placed
    it, and even our own prefix can be imitated (``origin`` says how it was classified). So is
    ``reject_reason``: the exchange's code for why it refused the order, passed on as data.
    """

    order_id: str
    symbol: str
    category: Category
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    qty: Decimal
    qty_unit: QtyUnit
    filled_qty: Decimal
    price: Decimal | None
    avg_fill_price: Decimal | None
    time_in_force: TimeInForce
    reduce_only: bool
    conditional: ConditionalKind | None
    trigger_price: Decimal | None
    take_profit: Decimal | None
    stop_loss: Decimal | None
    created_at: datetime
    updated_at: datetime | None
    origin: OrderOrigin
    client_order_id: UntrustedText | None
    reject_reason: UntrustedText | None = None

    def __post_init__(self) -> None:
        if self.qty < 0 or self.filled_qty < 0:
            raise ValueError("order quantities are non-negative")


@dataclass(frozen=True, slots=True)
class Page[T]:
    """At most the requested number of items; ``has_more`` means the exchange holds further ones.

    ``omitted``: items of this page left out because v1 does not support them (e.g. a dated
    future whose symbol is not a plain perpetual/spot symbol); counted, never dropped silently.
    """

    items: tuple[T, ...]
    has_more: bool
    omitted: int = 0
