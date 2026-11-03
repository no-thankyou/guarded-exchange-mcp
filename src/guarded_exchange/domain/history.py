"""Order and trade history as the agent reads it (SPEC FR-2, FR-3, FR-41).

A history read covers one period of at most ``MAX_HISTORY_WINDOW`` no older than
``MAX_HISTORY_DEPTH`` (the exchange's own limits, checked before anything is sent) and is paged
with a cursor. The cursor the agent gets is ours: it carries the whole query (what, category,
symbol, period) plus the exchange's opaque page cursor, so a next page is always read with the
parameters that cursor was issued for and the agent cannot mix them up.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from guarded_exchange.domain.account import OrderOrigin, OrderSide, UntrustedText
from guarded_exchange.domain.errors import InvalidParams
from guarded_exchange.domain.instruments import Category

MAX_HISTORY_WINDOW: Final = timedelta(days=7)
"""Bybit: ``endTime - startTime <= 7 days`` for order history and executions (checked 2026-11-03)."""

MAX_HISTORY_DEPTH: Final = timedelta(days=730)
"""Bybit keeps 730 days (2 years) of order history and executions for a unified account."""

MAX_CURSOR_LENGTH: Final = 1024
_MAX_EXCHANGE_CURSOR: Final = 512
_CURSOR_VERSION: Final = 1
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class HistoryKind(StrEnum):
    """What a history cursor pages through; a cursor of one tool is refused by the other."""

    ORDERS = "orders"
    TRADES = "trades"


@dataclass(frozen=True, slots=True)
class HistoryPeriod:
    """``[start, end]`` in UTC, whole milliseconds (the exchange's precision), at most 7 days."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("history period bounds are timezone-aware")
        if self.start >= self.end:
            raise ValueError("history period starts before it ends")
        if self.end - self.start > MAX_HISTORY_WINDOW:
            raise ValueError("history period is at most 7 days")


def history_period(start: datetime | None, end: datetime | None, now: datetime) -> HistoryPeriod:
    """The period to read, by the exchange's rules, or ``InvalidParams`` with a hint.

    Neither bound: the last 7 days. Only ``start``: 7 days from it. Only ``end``: 7 days before it.
    A time without an offset is UTC. An end in the future is cut to ``now`` (nothing is there yet),
    so the answer states the period actually read.
    """
    start, end, now = _utc_or_none(start), _utc_or_none(end), _whole_ms(now)
    if start is None:
        end = now if end is None else end
        start = end - MAX_HISTORY_WINDOW
    elif end is None:
        end = start + MAX_HISTORY_WINDOW
    if start >= end:
        raise _period_error(
            "start_time must be before end_time",
            "Pass start_time earlier than end_time, at most 7 days apart.",
            "end_time",
        )
    if end - start > MAX_HISTORY_WINDOW:
        raise _period_error(
            "the period is longer than 7 days, the most the exchange returns per request",
            "Split the period into windows of at most 7 days and call once per window, e.g. "
            f"start_time={_iso(start)} end_time={_iso(start + MAX_HISTORY_WINDOW)}.",
            "end_time",
        )
    if start >= now:
        raise _period_error(
            "the period is in the future", "Ask for a period that starts in the past.", "start_time"
        )
    period = HistoryPeriod(start=start, end=min(end, now))
    ensure_within_depth(period, now)
    return period


def ensure_within_depth(period: HistoryPeriod, now: datetime) -> None:
    """``InvalidParams`` if the period starts before the exchange's 2-year history horizon."""
    oldest = now - MAX_HISTORY_DEPTH
    if period.start < oldest:
        raise _period_error(
            "the exchange keeps only the last 730 days (2 years) of history",
            f"Use a start_time after {_iso(oldest)}; older history cannot be read.",
            "start_time",
        )


def _utc_or_none(value: datetime | None) -> datetime | None:
    """UTC, whole milliseconds (what the exchange takes); no offset means UTC."""
    if value is None:
        return None
    return _whole_ms(value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC))


def _whole_ms(value: datetime) -> datetime:
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _period_error(message: str, next_step: str, field: str) -> InvalidParams:
    return InvalidParams(message, next_step=next_step, details={"field": field, "reason": "period"})


# --- cursor ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    """One history read: what, where and when, and the exchange's cursor of the page to read
    (``None``: the first page). The exchange cursor is opaque here; the adapter checks its shape."""

    kind: HistoryKind
    category: Category
    symbol: str | None
    period: HistoryPeriod
    exchange_cursor: str | None = None


def encode_cursor(query: HistoryQuery) -> str:
    """The cursor handed to the agent: URL-safe base64 of the query, opaque to the agent."""
    data = {
        "v": _CURSOR_VERSION,
        "k": query.kind.value,
        "c": query.category.value,
        "s": query.symbol,
        "a": _ms(query.period.start),
        "b": _ms(query.period.end),
        "t": query.exchange_cursor,
    }
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(raw: str, kind: HistoryKind) -> HistoryQuery:
    """The query a cursor of ``kind`` was issued for, or ``InvalidParams``.

    Everything is re-validated: a cursor is input from the model, not proof that we issued it. A
    changed cursor can only ask for what the agent could ask for with plain arguments.
    """
    try:
        if not 0 < len(raw) <= MAX_CURSOR_LENGTH:
            raise ValueError("cursor length")
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode())
        if not isinstance(data, dict) or set(data) != {"v", "k", "c", "s", "a", "b", "t"}:
            raise ValueError("cursor fields")
        if data["v"] != _CURSOR_VERSION or data["k"] != kind.value:
            raise ValueError("cursor of another tool or version")
        symbol, token = data["s"], data["t"]
        if symbol is not None and not isinstance(symbol, str):
            raise ValueError("cursor symbol")
        if not isinstance(token, str) or not 0 < len(token) <= _MAX_EXCHANGE_CURSOR:
            raise ValueError("exchange cursor in cursor")
        return HistoryQuery(
            kind=kind,
            category=Category(data["c"]),
            symbol=symbol,
            period=HistoryPeriod(start=_from_ms(data["a"]), end=_from_ms(data["b"])),
            exchange_cursor=token,
        )
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError, OverflowError):
        raise InvalidParams(
            "cursor is not a cursor this tool returned",
            next_step="Pass next_cursor exactly as the previous call of the same tool returned it, or "
            "call again without cursor to start from the first page.",
            details={"field": "cursor", "reason": "invalid_cursor"},
        ) from None


def _ms(value: datetime) -> int:
    return (value - _EPOCH) // timedelta(milliseconds=1)


def _from_ms(value: object) -> datetime:
    if type(value) is not int:
        raise ValueError("cursor time")
    return _EPOCH + timedelta(milliseconds=value)


@dataclass(frozen=True, slots=True)
class HistoryPage[T]:
    """One page of history, newest first. ``next_exchange_cursor``: the exchange's cursor of the next
    page (``None``: this is the last one); a next page may still turn out empty.

    ``omitted``: rows of this page v1 cannot represent (e.g. a dated future), counted, not shown.
    """

    items: tuple[T, ...]
    next_exchange_cursor: str | None
    omitted: int = 0


# --- trades ------------------------------------------------------------------------------------


class ExecutionKind(StrEnum):
    """Why a fill happened. ``TRADE`` is an ordinary match of an order; the others are the
    exchange acting on the position (liquidation, auto-deleveraging, funding, settlement)."""

    TRADE = "trade"
    LIQUIDATION = "liquidation"
    AUTO_DELEVERAGE = "auto_deleverage"
    FUNDING = "funding"
    SETTLEMENT = "settlement"
    BLOCK_TRADE = "block_trade"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Trade:
    """One execution (fill) of an order. ``qty`` in the base asset, ``price`` and ``value`` in the
    pair's quote currency, ``fee`` in ``fee_currency`` (negative: a rebate paid to the account).

    ``origin`` and ``client_order_id`` describe the order that was filled (see ``Order``).
    """

    trade_id: str
    order_id: str
    symbol: str
    category: Category
    side: OrderSide
    kind: ExecutionKind
    price: Decimal
    qty: Decimal
    value: Decimal | None
    fee: Decimal | None
    fee_currency: str | None
    fee_rate: Decimal | None
    is_maker: bool | None
    closed_size: Decimal | None
    executed_at: datetime
    origin: OrderOrigin
    client_order_id: UntrustedText | None

    def __post_init__(self) -> None:
        if self.qty < 0 or self.price < 0:
            raise ValueError("trade price and quantity are non-negative")
