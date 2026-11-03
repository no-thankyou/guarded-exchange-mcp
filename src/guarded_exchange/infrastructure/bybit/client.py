"""Async Bybit V5 REST client implementing the ``ExchangeClient`` port (PLAN ADR-002).

- Timeouts are explicit and split (connect / read) and are configured on the ``AsyncClient``
  that the composition root passes in.
- Only GET requests are retried, with exponential backoff and jitter. Mutating requests
  (later tickets) are never retried automatically: a timeout there means "outcome unknown"
  (FR-27), not "failed".
- Every failure leaves this module as a domain exception.
"""

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlencode

import httpx2
import structlog

from guarded_exchange.domain.account import AccountBalances, Order, Page, Position, SymbolPosition
from guarded_exchange.domain.credentials import ApiKeyPair, KeyPermissions
from guarded_exchange.domain.errors import DomainError, ExchangeUnavailable, NotFound
from guarded_exchange.domain.history import HistoryPage, HistoryPeriod, Trade
from guarded_exchange.domain.instruments import Category, InstrumentRules
from guarded_exchange.domain.market import CandleInterval, Candles, Orderbook, Ticker
from guarded_exchange.domain.order_changes import OrderAmendment
from guarded_exchange.domain.orders import OrderFill, OrderRequest, PlacedOrder, PriceBand
from guarded_exchange.infrastructure.bybit.errors import (
    RET_OK,
    RETRYABLE_RET_CODES,
    error_for_create,
    error_for_order_change,
    error_for_ret_code,
    outcome_unknown,
    unavailable,
)
from guarded_exchange.infrastructure.bybit.mapper import (
    KLINE_INTERVALS,
    OPEN_ORDERS_PAGE_MAX,
    ORDER_HISTORY_PAGE_MAX,
    PLAIN_ORDERS_FILTER,
    UNIFIED_ACCOUNT,
    USDT_SETTLE,
    Envelope,
    balances_from_result,
    candles_from_result,
    client_order_id_for,
    key_permissions_from_result,
    open_orders_from_result,
    order_amend_body,
    order_by_id_from_result,
    order_cancel_body,
    order_create_body,
    order_fill_from_result,
    order_history_from_result,
    orderbook_from_result,
    parse_envelope,
    placed_order_from_result,
    positions_from_result,
    price_band_from_result,
    require_changed_order,
    rules_from_result,
    symbol_position_from_result,
    ticker_from_result,
    trades_from_result,
    wire_exchange_cursor,
)
from guarded_exchange.infrastructure.bybit.signing import signed_headers

DEMO_BASE_URL: Final = "https://api-demo.bybit.com"

_HTTP_FORBIDDEN: Final = 403
_HTTP_SERVER_ERROR: Final = 500
_LIMIT_RESET_HEADER: Final = "X-Bapi-Limit-Reset-Timestamp"
_MIN_RETRY_AFTER_S: Final = 1.0
_MAX_RETRY_AFTER_S: Final = 600.0
_DELIVERING: Final = "Delivering"
_NOT_SENT: Final = (
    httpx2.ConnectError,
    httpx2.ConnectTimeout,
    httpx2.PoolTimeout,
    httpx2.ProxyError,
    httpx2.UnsupportedProtocol,
    httpx2.InvalidURL,
)
"""Failures before the request can have reached the exchange: definitely not sent."""
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

log = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]
NowMs = Callable[[], int]


def _system_now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Backoff for idempotent (GET) requests: ``base * 2**attempt`` plus up to 10 % jitter."""

    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0

    def delay(self, attempt: int) -> float:
        raw = min(self.base_delay_s * 2.0**attempt, self.max_delay_s)
        return raw + random.uniform(0, raw / 10)  # noqa: S311 - jitter, not cryptography


class _Retryable(Exception):
    """Internal marker: this GET attempt may be repeated."""

    def __init__(self, error: DomainError) -> None:
        super().__init__(error.message)
        self.error = error


class BybitClient:
    """Public market data, signed private reads (``query-api``, account balances, positions, open
    orders) and placing orders (``TradingExchange``); amend, cancel and position actions arrive later.

    Signed requests: the query string is built here once and both signed and sent verbatim, so
    the signature always covers exactly what goes on the wire. Signed headers and response bodies
    of private endpoints are never logged.
    """

    def __init__(
        self,
        http: httpx2.AsyncClient,
        *,
        retry: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        now_ms: NowMs = _system_now_ms,
    ) -> None:
        self._http = http
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._now_ms = now_ms

    async def get_key_permissions(self, keys: ApiKeyPair) -> KeyPermissions:
        """``GET /v5/user/query-api``: what the key may do (FR-49). ``KeyPermissionsReader`` port."""
        envelope = await self._get("/v5/user/query-api", {}, keys=keys)
        return key_permissions_from_result(envelope.result)

    async def get_balances(self, keys: ApiKeyPair) -> AccountBalances:
        """``GET /v5/account/wallet-balance`` of the unified trading account (``AccountReader``)."""
        envelope = await self._get("/v5/account/wallet-balance", {"accountType": UNIFIED_ACCOUNT}, keys=keys)
        return balances_from_result(envelope.result, time_ms=envelope.time_ms)

    async def get_positions(self, keys: ApiKeyPair, *, symbol: str | None, limit: int) -> Page[Position]:
        """``GET /v5/position/list`` (linear); one extra row tells whether there are more."""
        params = {"category": Category.LINEAR.value, "limit": str(limit + 1)}
        # Linear needs a symbol or a settle coin; with settleCoin only positions with size > 0 return.
        params.update({"symbol": symbol} if symbol else {"settleCoin": USDT_SETTLE})
        envelope = await self._get("/v5/position/list", params, keys=keys)
        return positions_from_result(envelope.result, symbol=symbol, limit=limit)

    async def get_open_orders(
        self, keys: ApiKeyPair, category: Category, *, symbol: str | None, limit: int
    ) -> Page[Order]:
        """``GET /v5/order/realtime`` with ``openOnly=0`` (open orders only), newest first."""
        params = {
            "category": category.value,
            "openOnly": "0",
            "limit": str(min(limit + 1, OPEN_ORDERS_PAGE_MAX)),
        }
        if symbol:
            params["symbol"] = symbol
        elif category is Category.LINEAR:
            params["settleCoin"] = USDT_SETTLE  # linear needs a symbol, base coin or settle coin
        envelope = await self._get("/v5/order/realtime", params, keys=keys)
        return open_orders_from_result(envelope.result, category=category, symbol=symbol, limit=limit)

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
        """``GET /v5/order/history`` (``AccountHistoryReader``): closed orders, newest first."""
        page_limit = min(limit, ORDER_HISTORY_PAGE_MAX)
        params = _history_params(category, symbol, period, exchange_cursor, page_limit)
        envelope = await self._get("/v5/order/history", params, keys=keys)
        return order_history_from_result(envelope.result, category=category, symbol=symbol, limit=page_limit)

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
        """``GET /v5/execution/list`` (``AccountHistoryReader``): executions, newest first."""
        params = _history_params(category, symbol, period, exchange_cursor, limit)
        envelope = await self._get("/v5/execution/list", params, keys=keys)
        return trades_from_result(envelope.result, category=category, symbol=symbol, limit=limit)

    async def get_ticker(self, symbol: str, category: Category) -> Ticker:
        envelope = await self._get("/v5/market/tickers", {"category": category.value, "symbol": symbol})
        return ticker_from_result(envelope.result, symbol=symbol, category=category, time_ms=envelope.time_ms)

    async def get_orderbook(self, symbol: str, category: Category, depth: int) -> Orderbook:
        envelope = await self._get(
            "/v5/market/orderbook", {"category": category.value, "symbol": symbol, "limit": str(depth)}
        )
        return orderbook_from_result(envelope.result, symbol=symbol, category=category)

    async def get_candles(
        self, symbol: str, category: Category, interval: CandleInterval, limit: int
    ) -> Candles:
        params = {
            "category": category.value,
            "symbol": symbol,
            "interval": KLINE_INTERVALS[interval],
            "limit": str(limit),
        }
        envelope = await self._get("/v5/market/kline", params)
        return candles_from_result(envelope.result, symbol=symbol, category=category, interval=interval)

    async def get_instrument_rules(
        self, symbol: str, category: Category, *, any_status: bool = False
    ) -> InstrumentRules:
        """Without ``status`` Bybit lists only ``Trading`` and ``PendingOpen`` instruments (docs, checked
        2026-10-29). ``any_status`` asks once more with ``status=Delivering`` for a linear instrument
        being delisted, so a position on it can still be reduced (ticket 011)."""
        params = {"category": category.value, "symbol": symbol}
        try:
            envelope = await self._get("/v5/market/instruments-info", params)
            return rules_from_result(envelope.result, symbol=symbol, category=category)
        except NotFound:
            if not any_status or category is not Category.LINEAR:
                raise
        envelope = await self._get("/v5/market/instruments-info", {**params, "status": _DELIVERING})
        return rules_from_result(envelope.result, symbol=symbol, category=category)

    # --- trading (ticket 011): ``TradingExchange`` port -------------------------------------

    def client_order_id(self, action_id: str) -> str:
        return client_order_id_for(action_id)

    async def get_price_band(self, symbol: str, category: Category) -> PriceBand:
        """``GET /v5/market/price-limit``: the corridor Bybit moves (perp) or rejects (spot) prices to."""
        envelope = await self._get("/v5/market/price-limit", {"category": category.value, "symbol": symbol})
        return price_band_from_result(envelope.result, symbol=symbol)

    async def get_symbol_position(self, keys: ApiKeyPair, symbol: str) -> SymbolPosition:
        params = {"category": Category.LINEAR.value, "symbol": symbol}
        envelope = await self._get("/v5/position/list", params, keys=keys)
        return symbol_position_from_result(envelope.result, symbol=symbol)

    async def place_order(self, keys: ApiKeyPair, order: OrderRequest) -> PlacedOrder:
        """``POST /v5/order/create``, once: never retried (ADR-002, FR-27)."""
        envelope = await self._post("/v5/order/create", order_create_body(order), keys=keys)
        return placed_order_from_result(envelope.result, client_order_id=order.client_order_id)

    async def get_order(
        self, keys: ApiKeyPair, category: Category, symbol: str, client_order_id: str
    ) -> OrderFill | None:
        params = {"category": category.value, "symbol": symbol, "orderLinkId": client_order_id}
        envelope = await self._get("/v5/order/realtime", params, keys=keys)
        return order_fill_from_result(envelope.result, symbol=symbol, client_order_id=client_order_id)

    async def find_order(
        self, keys: ApiKeyPair, category: Category, symbol: str, client_order_id: str
    ) -> OrderFill | None:
        """Reconnaissance by ``orderLinkId`` (FR-27, ADR-014): ``order/realtime`` (open orders and the
        most recent 500 closed ones; ``openOnly`` is ignored for an ``orderLinkId`` query), then
        ``order/history`` (last 7 days by default; cancelled or rejected orders only for 24 h). Both
        are GETs: safe to repeat. Bybit docs checked 2026-11-03."""
        found = await self.get_order(keys, category, symbol, client_order_id)
        if found is not None:
            return found
        params = {"category": category.value, "symbol": symbol, "orderLinkId": client_order_id}
        envelope = await self._get("/v5/order/history", params, keys=keys)
        return order_fill_from_result(
            envelope.result, symbol=symbol, client_order_id=client_order_id, endpoint="order/history"
        )

    # --- cancel and amend (ticket 013): ``OrderChanges`` port --------------------------------

    async def get_order_by_id(
        self, keys: ApiKeyPair, category: Category, symbol: str, order_id: str
    ) -> Order | None:
        """``order/realtime?orderId=`` (open and recent closed orders: ``openOnly`` is ignored for an id
        query), then ``order/history?orderId=``. Both GETs: safe to repeat (D-21 reconnaissance)."""
        params = {"category": category.value, "symbol": symbol, "orderId": order_id}
        for path, endpoint in (
            ("/v5/order/realtime", "order/realtime"),
            ("/v5/order/history", "order/history"),
        ):
            envelope = await self._get(path, params, keys=keys)
            found = order_by_id_from_result(
                envelope.result, category=category, symbol=symbol, order_id=order_id, endpoint=endpoint
            )
            if found is not None:
                return found
        return None

    async def list_cancellable_orders(
        self, keys: ApiKeyPair, category: Category, symbol: str, limit: int
    ) -> Page[Order]:
        """``order/realtime`` with an explicit ``orderFilter=Order``: plain open orders only, so neither
        conditional orders nor a position's TP/SL can ever be on the list ``cancel_all_orders`` works
        through (Bybit returns all kinds when the filter is omitted; docs checked 2026-11-03)."""
        params = {
            "category": category.value,
            "symbol": symbol,
            "openOnly": "0",
            "orderFilter": PLAIN_ORDERS_FILTER,
            "limit": str(min(limit + 1, OPEN_ORDERS_PAGE_MAX)),
        }
        envelope = await self._get("/v5/order/realtime", params, keys=keys)
        return open_orders_from_result(envelope.result, category=category, symbol=symbol, limit=limit)

    async def cancel_order(self, keys: ApiKeyPair, category: Category, symbol: str, order_id: str) -> None:
        """``POST /v5/order/cancel`` by order id, once: never retried. Asynchronous at Bybit: success means
        "accepted", the use case reads the order back (D-21)."""
        envelope = await self._post(
            "/v5/order/cancel",
            order_cancel_body(category, symbol, order_id),
            keys=keys,
            errors=error_for_order_change,
        )
        require_changed_order(envelope.result, order_id=order_id)

    async def amend_order(self, keys: ApiKeyPair, amendment: OrderAmendment) -> None:
        """``POST /v5/order/amend``, once: never retried; asynchronous like a cancel."""
        envelope = await self._post(
            "/v5/order/amend", order_amend_body(amendment), keys=keys, errors=error_for_order_change
        )
        require_changed_order(envelope.result, order_id=amendment.order_id)

    async def _post(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        keys: ApiKeyPair,
        errors: Callable[..., DomainError] = error_for_create,
    ) -> Envelope:
        """One signed POST. The body is serialised once and both signed and sent verbatim.

        Failures before the request can have left (no connection) are ``ExchangeUnavailable``;
        anything after that (timeout while waiting, broken connection, 5xx, an unreadable answer)
        is ``OutcomeUnknown``: the exchange may have acted on it.
        """
        payload = json.dumps(body, separators=(",", ":"))
        headers = {
            **signed_headers(keys, timestamp_ms=self._now_ms(), payload=payload),
            "Content-Type": "application/json",
        }
        try:
            response = await self._http.post(path, content=payload.encode(), headers=headers)
        except _NOT_SENT as exc:
            raise unavailable(f"could not connect ({type(exc).__name__})") from exc
        except (httpx2.HTTPError, httpx2.StreamError) as exc:
            raise outcome_unknown(type(exc).__name__) from exc

        if response.status_code >= _HTTP_SERVER_ERROR:
            raise outcome_unknown(f"HTTP {response.status_code}")
        if response.status_code == _HTTP_FORBIDDEN:
            raise unavailable("HTTP 403 (IP rate limit or access denied)")  # refused at the gateway
        if not response.is_success:
            raise unavailable(f"HTTP {response.status_code}")
        payload_json = _json_or_none(response)
        if payload_json is None:
            raise outcome_unknown("response is not JSON")
        try:
            envelope = parse_envelope(payload_json)
        except ExchangeUnavailable:
            raise outcome_unknown("unexpected response format") from None
        if envelope.ret_code == RET_OK:
            return envelope
        raise errors(
            envelope.ret_code, envelope.ret_msg, retry_after_s=self._retry_after(response, envelope.time_ms)
        )

    async def _get(self, path: str, params: Mapping[str, str], *, keys: ApiKeyPair | None = None) -> Envelope:
        attempts = max(1, self._retry.max_attempts)
        attempt = 0
        while True:
            try:
                return await self._get_once(path, params, keys)
            except _Retryable as retryable:
                attempt += 1
                if attempt >= attempts:
                    raise retryable.error from retryable.__cause__
                delay = self._retry.delay(attempt - 1)
                log.warning(
                    "exchange_get_retry",
                    path=path,
                    attempt=attempt,
                    delay_s=round(delay, 3),
                    reason=retryable.error.message,
                )
                await self._sleep(delay)

    async def _get_once(self, path: str, params: Mapping[str, str], keys: ApiKeyPair | None) -> Envelope:
        try:
            if keys is None:
                response = await self._http.get(path, params=dict(params))
            else:
                # Signed per attempt: a retry gets a fresh timestamp inside the receive window.
                query = _query(params)
                headers = signed_headers(keys, timestamp_ms=self._now_ms(), payload=query)
                response = await self._http.get(f"{path}?{query}" if query else path, headers=headers)
        except httpx2.TimeoutException as exc:
            raise _Retryable(unavailable(f"timeout ({type(exc).__name__})")) from exc
        except httpx2.TransportError as exc:
            raise _Retryable(unavailable(f"network error ({type(exc).__name__})")) from exc
        except (httpx2.HTTPError, httpx2.InvalidURL, httpx2.StreamError) as exc:
            # Decoding errors, redirects, bad URL/config: repeating the same request will not help.
            raise unavailable(f"HTTP client error ({type(exc).__name__})") from exc

        if response.status_code >= _HTTP_SERVER_ERROR:
            raise _Retryable(unavailable(f"HTTP {response.status_code}"))
        if response.status_code == _HTTP_FORBIDDEN:
            # Bybit answers 403 when the IP rate limit is exceeded; retrying makes it worse.
            raise unavailable("HTTP 403 (IP rate limit or access denied)")
        if not response.is_success:
            raise unavailable(f"HTTP {response.status_code}")

        payload = _json_or_none(response)
        if payload is None:
            # Not chained: a JSON decoding error keeps the whole body, which may hold key material.
            raise unavailable("response is not JSON")
        envelope = parse_envelope(payload)
        if envelope.ret_code == RET_OK:
            return envelope
        error = error_for_ret_code(
            envelope.ret_code, envelope.ret_msg, retry_after_s=self._retry_after(response, envelope.time_ms)
        )
        if envelope.ret_code in RETRYABLE_RET_CODES:
            raise _Retryable(error)
        raise error

    def _retry_after(self, response: httpx2.Response, server_ms: int | None) -> float | None:
        """Seconds until ``X-Bapi-Limit-Reset-Timestamp`` (ms), within
        ``_MIN_RETRY_AFTER_S``..``_MAX_RETRY_AFTER_S``.

        Measured from the exchange's own response time (``time``), both stamps on the same clock,
        so a skewed local clock does not shrink or stretch the wait; the local clock only if the
        response carries no time. Never 0: an immediate retry would hit the same limit.
        """
        raw = response.headers.get(_LIMIT_RESET_HEADER, "").strip()
        if not raw.isdigit():
            return None
        now_ms = server_ms if server_ms else self._now_ms()
        seconds = (int(raw) - now_ms) / 1000
        return round(min(max(seconds, _MIN_RETRY_AFTER_S), _MAX_RETRY_AFTER_S), 3)


def _history_params(
    category: Category, symbol: str | None, period: HistoryPeriod, exchange_cursor: str | None, limit: int
) -> dict[str, str]:
    """Both period bounds are always sent, so the exchange reads exactly the period we checked."""
    params = {
        "category": category.value,
        "startTime": str(_epoch_ms(period.start)),
        "endTime": str(_epoch_ms(period.end)),
        "limit": str(limit),
    }
    if symbol:
        params["symbol"] = symbol
    elif category is Category.LINEAR:
        params["settleCoin"] = USDT_SETTLE  # v1 reads USDT perpetuals only
    if exchange_cursor is not None:
        params["cursor"] = wire_exchange_cursor(exchange_cursor)
    return params


def _epoch_ms(value: datetime) -> int:
    return (value - _EPOCH) // timedelta(milliseconds=1)  # exact: no float on the way


def _query(params: Mapping[str, str]) -> str:
    """The signed query string, sorted. ``%`` stays as is: the only value carrying it is the
    exchange's page cursor, which arrives percent-encoded and must go back verbatim
    (``wire_exchange_cursor`` admits only unreserved characters and ``%XX`` in it); everything else
    is percent-encoded as usual."""
    return urlencode(sorted(params.items()), safe="%")


def _json_or_none(response: httpx2.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
