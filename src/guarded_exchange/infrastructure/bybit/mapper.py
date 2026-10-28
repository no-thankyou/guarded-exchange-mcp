"""Bybit V5 response DTOs and their mapping to domain objects.

DTOs mirror the wire format (camelCase strings) and never leave this package: the rest of
the system only sees ``guarded_exchange.domain`` types (ENGINEERING.md, PLAN ADR-002).
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from guarded_exchange.domain.errors import ExchangeUnavailable, InvalidParams, NotFound
from guarded_exchange.domain.instruments import Category, Instrument, InstrumentRules, InstrumentStatus
from guarded_exchange.domain.market import (
    Candle,
    CandleInterval,
    Candles,
    Orderbook,
    OrderbookLevel,
    Ticker,
)
from guarded_exchange.infrastructure.bybit.errors import unavailable

_HUNDRED = Decimal(100)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Envelope(BaseModel):
    """Common V5 response wrapper: ``{retCode, retMsg, result, time}``."""

    model_config = ConfigDict(extra="ignore")

    ret_code: int = Field(alias="retCode")
    ret_msg: str = Field(default="", alias="retMsg")
    result: dict[str, Any] = Field(default_factory=dict)
    time_ms: int | None = Field(default=None, alias="time")


class TickerDTO(BaseModel):
    """One item of ``GET /v5/market/tickers`` (spot and linear share most fields)."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    last_price: str = Field(alias="lastPrice")
    bid1_price: str = Field(default="", alias="bid1Price")
    ask1_price: str = Field(default="", alias="ask1Price")
    high_price_24h: str = Field(default="", alias="highPrice24h")
    low_price_24h: str = Field(default="", alias="lowPrice24h")
    price_24h_pcnt: str = Field(default="", alias="price24hPcnt")
    volume_24h: str = Field(default="", alias="volume24h")
    turnover_24h: str = Field(default="", alias="turnover24h")
    mark_price: str = Field(default="", alias="markPrice")
    index_price: str = Field(default="", alias="indexPrice")
    funding_rate: str = Field(default="", alias="fundingRate")
    next_funding_time: str = Field(default="", alias="nextFundingTime")


class TickersResultDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: str = ""
    items: list[TickerDTO] = Field(default_factory=list, alias="list")


def parse_envelope(payload: object) -> Envelope:
    try:
        return Envelope.model_validate(payload)
    except ValidationError as exc:
        raise unavailable("unexpected response format") from exc


def ticker_from_result(
    result: dict[str, Any], *, symbol: str, category: Category, time_ms: int | None
) -> Ticker:
    """Map the ``result`` of a successful tickers call to a domain ``Ticker``."""
    try:
        parsed = TickersResultDTO.model_validate(result)
    except ValidationError as exc:
        raise unavailable("unexpected tickers format") from exc
    item = next((i for i in parsed.items if i.symbol == symbol), None)
    if item is None:
        raise NotFound(
            f"no {category.value} instrument {symbol}",
            next_step="Check the symbol and category; spot and linear list different instruments.",
            details={"symbol": symbol, "category": category.value},
        )
    try:
        return _ticker(item, category=category, time_ms=time_ms)
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("unparsable number in tickers response") from exc


def _ticker(item: TickerDTO, *, category: Category, time_ms: int | None) -> Ticker:
    last_price = _dec(item.last_price)
    if last_price is None:
        raise ExchangeUnavailable(
            f"the exchange returned no last price for {item.symbol}",
            next_step="Retry later; the instrument may not be trading right now.",
        )
    observed_at = _ts(time_ms)
    if observed_at is None:
        raise unavailable("response without a server timestamp")
    linear = category is Category.LINEAR
    change = _dec(item.price_24h_pcnt)
    funding = _dec(item.funding_rate) if linear else None
    return Ticker(
        symbol=item.symbol,
        category=category,
        last_price=last_price,
        bid_price=_dec(item.bid1_price),
        ask_price=_dec(item.ask1_price),
        high_24h=_dec(item.high_price_24h),
        low_24h=_dec(item.low_price_24h),
        change_24h_pct=None if change is None else change * _HUNDRED,
        volume_24h_base=_dec(item.volume_24h),
        turnover_24h_quote=_dec(item.turnover_24h),
        observed_at=observed_at,
        mark_price=_dec(item.mark_price) if linear else None,
        index_price=_dec(item.index_price) if linear else None,
        funding_rate_pct=None if funding is None else funding * _HUNDRED,
        next_funding_at=_ts(_int(item.next_funding_time)) if linear else None,
    )


def _dec(raw: str) -> Decimal | None:
    """Bybit sends numbers as strings and ``""`` for "not applicable"; never go through float."""
    raw = raw.strip()
    if not raw:
        return None
    value = Decimal(raw)
    if not value.is_finite():
        raise ValueError(f"non-finite number: {raw!r}")
    return value


def _required_dec(raw: str) -> Decimal:
    value = _dec(raw)
    if value is None:
        raise ValueError("missing number")
    return value


def _int(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw else None


def _ts(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return _EPOCH + timedelta(milliseconds=ms)


# --- order book ------------------------------------------------------------------------------


class OrderbookResultDTO(BaseModel):
    """``result`` of ``GET /v5/market/orderbook``: levels are ``[price, size]`` string pairs,
    bids highest first, asks lowest first; ``ts`` is when the book snapshot was generated (ms)."""

    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(alias="s")
    bids: list[tuple[str, str]] = Field(alias="b")
    asks: list[tuple[str, str]] = Field(alias="a")
    ts: int | None = None


def orderbook_from_result(result: dict[str, Any], *, symbol: str, category: Category) -> Orderbook:
    try:
        parsed = OrderbookResultDTO.model_validate(result)
    except ValidationError as exc:
        raise unavailable("unexpected orderbook format") from exc
    if parsed.symbol != symbol:
        raise unavailable("orderbook for a different symbol")
    observed_at = _ts(parsed.ts)
    if observed_at is None:
        raise unavailable("orderbook without a timestamp")
    try:
        return Orderbook(
            symbol=symbol,
            category=category,
            bids=tuple(_level(price, qty) for price, qty in parsed.bids),
            asks=tuple(_level(price, qty) for price, qty in parsed.asks),
            observed_at=observed_at,
        )
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("inconsistent orderbook levels") from exc


def _level(price: str, qty: str) -> OrderbookLevel:
    return OrderbookLevel(price=_required_dec(price), qty=_required_dec(qty))


# --- candles ---------------------------------------------------------------------------------

KLINE_INTERVALS: Final[dict[CandleInterval, str]] = {
    CandleInterval.M1: "1",
    CandleInterval.M3: "3",
    CandleInterval.M5: "5",
    CandleInterval.M15: "15",
    CandleInterval.M30: "30",
    CandleInterval.H1: "60",
    CandleInterval.H2: "120",
    CandleInterval.H4: "240",
    CandleInterval.H6: "360",
    CandleInterval.H12: "720",
    CandleInterval.D1: "D",
    CandleInterval.W1: "W",
    CandleInterval.MO1: "M",
}
"""Domain interval -> Bybit ``interval`` parameter (https://bybit-exchange.github.io/docs/v5/enum)."""

KlineRow = tuple[str, str, str, str, str, str, str]


class KlineResultDTO(BaseModel):
    """``result`` of ``GET /v5/market/kline``: newest first, each row
    ``[startTime, open, high, low, close, volume, turnover]`` as strings."""

    model_config = ConfigDict(extra="ignore")

    symbol: str = ""
    rows: list[KlineRow] = Field(default_factory=list, alias="list")


def candles_from_result(
    result: dict[str, Any], *, symbol: str, category: Category, interval: CandleInterval
) -> Candles:
    try:
        parsed = KlineResultDTO.model_validate(result)
    except ValidationError as exc:
        raise unavailable("unexpected kline format") from exc
    if parsed.symbol != symbol:
        raise unavailable("candles for a different symbol")
    try:
        candles = tuple(_candle(row) for row in reversed(parsed.rows))  # Bybit: newest first
        return Candles(symbol=symbol, category=category, interval=interval, candles=candles)
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("inconsistent candles") from exc


def _candle(row: KlineRow) -> Candle:
    start_ms, open_, high, low, close, volume, turnover = row
    start_at = _ts(_int(start_ms))
    if start_at is None:
        raise ValueError("candle without a start time")
    return Candle(
        start_at=start_at,
        open=_required_dec(open_),
        high=_required_dec(high),
        low=_required_dec(low),
        close=_required_dec(close),
        volume_base=_required_dec(volume),
        turnover_quote=_required_dec(turnover),
    )


# --- instrument rules ------------------------------------------------------------------------

_COIN_RE: Final = re.compile(r"^[A-Z0-9]{1,20}$")
_LINEAR_PERPETUAL: Final = "LinearPerpetual"
_STATUSES: Final[dict[str, InstrumentStatus]] = {
    "PreLaunch": InstrumentStatus.PRE_LAUNCH,
    "PendingOpen": InstrumentStatus.PENDING_OPEN,
    "Trading": InstrumentStatus.TRADING,
    "Delivering": InstrumentStatus.SETTLING,
    "Closed": InstrumentStatus.CLOSED,
}
"""Bybit instrument ``status`` enum (https://bybit-exchange.github.io/docs/v5/enum); anything else
maps to ``UNKNOWN`` and therefore to "not trading"."""


class _PriceFilterDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tick_size: str = Field(alias="tickSize")


class _LotSizeFilterDTO(BaseModel):
    """Linear: ``qtyStep``, ``minOrderQty``, ``minNotionalValue``. Spot: ``basePrecision`` is the
    quantity step, ``minOrderAmt`` the minimum order value; spot ``minOrderQty`` is deprecated."""

    model_config = ConfigDict(extra="ignore")

    qty_step: str = Field(default="", alias="qtyStep")
    min_order_qty: str = Field(default="", alias="minOrderQty")
    min_notional_value: str = Field(default="", alias="minNotionalValue")
    base_precision: str = Field(default="", alias="basePrecision")
    min_order_amt: str = Field(default="", alias="minOrderAmt")


class _LeverageFilterDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_leverage: str = Field(alias="maxLeverage")


class InstrumentInfoDTO(BaseModel):
    """One item of ``GET /v5/market/instruments-info``; spot has no ``leverageFilter``/``contractType``."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    status: str
    base_coin: str = Field(alias="baseCoin")
    quote_coin: str = Field(alias="quoteCoin")
    contract_type: str = Field(default="", alias="contractType")
    price_filter: _PriceFilterDTO = Field(alias="priceFilter")
    lot_size_filter: _LotSizeFilterDTO = Field(alias="lotSizeFilter")
    leverage_filter: _LeverageFilterDTO | None = Field(default=None, alias="leverageFilter")


class InstrumentsResultDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[InstrumentInfoDTO] = Field(default_factory=list, alias="list")


def rules_from_result(result: dict[str, Any], *, symbol: str, category: Category) -> InstrumentRules:
    try:
        parsed = InstrumentsResultDTO.model_validate(result)
    except ValidationError as exc:
        raise unavailable("unexpected instruments-info format") from exc
    item = next((i for i in parsed.items if i.symbol == symbol), None)
    if item is None:
        raise NotFound(
            f"no {category.value} instrument {symbol}",
            next_step="Check the symbol and category; spot and linear list different instruments.",
            details={"symbol": symbol, "category": category.value},
        )
    if category is Category.LINEAR and item.contract_type != _LINEAR_PERPETUAL:
        # Domain `linear` means a perpetual: dated futures expire, which v1 does not model.
        raise InvalidParams(
            f"{symbol} is not a perpetual contract; only perpetuals are supported in linear",
            next_step="Use a USDT perpetual symbol, e.g. BTCUSDT.",
            details={"field": "symbol", "reason": "not_perpetual"},
        )
    if not (_COIN_RE.fullmatch(item.base_coin) and _COIN_RE.fullmatch(item.quote_coin)):
        # Coin codes end up in our own messages (e.g. "quoted in BTC"): only plain codes pass.
        raise unavailable("unexpected coin code in instruments-info")
    try:
        return _rules(item, category=category)
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("unparsable instrument rules") from exc


def _rules(item: InstrumentInfoDTO, *, category: Category) -> InstrumentRules:
    lot = item.lot_size_filter
    max_leverage: Decimal | None
    if category is Category.LINEAR:
        if item.leverage_filter is None:
            raise ValueError("linear instrument without leverageFilter")
        qty_step = _required_dec(lot.qty_step)
        min_qty = _required_dec(lot.min_order_qty)
        min_notional = _dec(lot.min_notional_value)
        max_leverage = _required_dec(item.leverage_filter.max_leverage)
    else:
        qty_step = _required_dec(lot.base_precision)
        min_qty = _dec(lot.min_order_qty) or qty_step  # deprecated on spot: one step is the floor
        min_notional = _dec(lot.min_order_amt)
        max_leverage = None  # v1 spot is cash only
    return InstrumentRules(
        instrument=Instrument(
            symbol=item.symbol, category=category, base=item.base_coin, quote=item.quote_coin
        ),
        status=_STATUSES.get(item.status, InstrumentStatus.UNKNOWN),
        tick_size=_required_dec(item.price_filter.tick_size),
        qty_step=qty_step,
        min_qty=min_qty,
        min_notional=min_notional,
        max_leverage=max_leverage,
    )
