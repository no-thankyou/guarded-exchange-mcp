"""Market data value objects. Money and quantities are ``Decimal``, time is UTC-aware."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from guarded_exchange.domain.errors import InvalidParams
from guarded_exchange.domain.instruments import Category


@dataclass(frozen=True, slots=True)
class Ticker:
    """A 24h market summary for one instrument.

    Prices are in the quote currency of the symbol (USDT for ``BTCUSDT``). Percentages are
    already multiplied by 100 (``Decimal("0.7")`` means 0.7 %). ``None`` means the exchange
    did not report the value (e.g. no bid on an illiquid book, funding on spot).
    """

    symbol: str
    category: Category
    last_price: Decimal
    bid_price: Decimal | None
    ask_price: Decimal | None
    high_24h: Decimal | None
    low_24h: Decimal | None
    change_24h_pct: Decimal | None
    volume_24h_base: Decimal | None
    turnover_24h_quote: Decimal | None
    observed_at: datetime
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    funding_rate_pct: Decimal | None = None
    next_funding_at: datetime | None = None


# --- order book ------------------------------------------------------------------------------

DEFAULT_ORDERBOOK_DEPTH: Final = 10
MAX_ORDERBOOK_DEPTH: Final = 50
"""FR-3: a book deeper than 50 levels per side is noise for a model and costs tokens."""


def orderbook_depth(raw: object) -> int:
    """Validate the requested number of levels per side (1..``MAX_ORDERBOOK_DEPTH``)."""
    return bounded_count(raw, "depth", MAX_ORDERBOOK_DEPTH)


@dataclass(frozen=True, slots=True)
class OrderbookLevel:
    """One price level: ``price`` in the quote currency, ``qty`` in the base asset."""

    price: Decimal
    qty: Decimal


@dataclass(frozen=True, slots=True)
class Orderbook:
    """Best bids (highest first) and asks (lowest first). An empty side is allowed (thin market)."""

    symbol: str
    category: Category
    bids: tuple[OrderbookLevel, ...]
    asks: tuple[OrderbookLevel, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        _check_side("bids", self.bids, descending=True)
        _check_side("asks", self.asks, descending=False)

    def top(self, depth: int) -> "Orderbook":
        """The same book cut to at most ``depth`` levels per side."""
        if len(self.bids) <= depth and len(self.asks) <= depth:
            return self
        return replace(self, bids=self.bids[:depth], asks=self.asks[:depth])


def _check_side(side: str, levels: tuple[OrderbookLevel, ...], *, descending: bool) -> None:
    prices = [level.price for level in levels]
    if any(p <= 0 for p in prices) or any(level.qty < 0 for level in levels):
        raise ValueError(f"{side}: non-positive price or negative quantity")
    if prices != sorted(prices, reverse=descending) or len(set(prices)) != len(prices):
        raise ValueError(f"{side} are not strictly {'descending' if descending else 'ascending'}")


# --- candles ---------------------------------------------------------------------------------

DEFAULT_CANDLES: Final = 50
MAX_CANDLES: Final = 200
"""FR-3: about a week of hourly or half a year of daily candles in one compact answer."""


def candles_limit(raw: object) -> int:
    """Validate the requested number of candles (1..``MAX_CANDLES``)."""
    return bounded_count(raw, "limit", MAX_CANDLES)


class CandleInterval(StrEnum):
    """Candle length as the agent passes it: minutes ``m``, hours ``h``, ``1d``, ``1w``, ``1mo``."""

    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H12 = "12h"
    D1 = "1d"
    W1 = "1w"
    MO1 = "1mo"


def parse_interval(raw: str) -> CandleInterval:
    """Return the ``CandleInterval`` for e.g. ``"1h"`` (case-insensitive) or raise ``InvalidParams``."""
    try:
        return CandleInterval(raw.strip().lower())
    except ValueError:
        allowed = ", ".join(i.value for i in CandleInterval)
        raise InvalidParams(
            f"interval must be one of: {allowed}",
            next_step="Pick one of the listed intervals, e.g. 1h or 1d.",
            details={"field": "interval"},
        ) from None


@dataclass(frozen=True, slots=True)
class Candle:
    """OHLCV of one interval. Prices in the quote currency, ``volume_base`` in the base asset.

    The newest candle may still be open: its ``close`` is the last traded price so far.
    """

    start_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_base: Decimal
    turnover_quote: Decimal

    def __post_init__(self) -> None:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("candle low/high do not bound open/close")
        if self.volume_base < 0 or self.turnover_quote < 0:
            raise ValueError("negative candle volume")


@dataclass(frozen=True, slots=True)
class Candles:
    """Consecutive candles of one instrument, oldest first (the natural reading order)."""

    symbol: str
    category: Category
    interval: CandleInterval
    candles: tuple[Candle, ...]

    def __post_init__(self) -> None:
        starts = [c.start_at for c in self.candles]
        if starts != sorted(starts) or len(set(starts)) != len(starts):
            raise ValueError("candles are not strictly ordered oldest first")

    def latest(self, limit: int) -> "Candles":
        """The same series cut to the newest ``limit`` candles."""
        if len(self.candles) <= limit:
            return self
        return replace(self, candles=self.candles[-limit:])


def bounded_count(raw: object, field: str, maximum: int) -> int:
    # Only a real int: bool is an int subclass (`true` is a mistake, not 1), and a float or
    # Decimal such as 2.5 must not be silently truncated or compared as a count.
    if type(raw) is not int or not 1 <= raw <= maximum:
        raise InvalidParams(
            f"{field} must be an integer from 1 to {maximum}",
            next_step=f"Ask for at most {maximum}.",
            details={"field": field},
        )
    return raw
