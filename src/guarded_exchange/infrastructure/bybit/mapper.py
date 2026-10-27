"""Bybit V5 response DTOs and their mapping to domain objects.

DTOs mirror the wire format (camelCase strings) and never leave this package: the rest of
the system only sees ``guarded_exchange.domain`` types (ENGINEERING.md, PLAN ADR-002).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from guarded_exchange.domain.errors import ExchangeUnavailable, NotFound
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.market import Ticker
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


def _int(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw else None


def _ts(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return _EPOCH + timedelta(milliseconds=ms)
