"""Async Bybit V5 REST client implementing the ``ExchangeClient`` port (PLAN ADR-002).

- Timeouts are explicit and split (connect / read) and are configured on the ``AsyncClient``
  that the composition root passes in.
- Only GET requests are retried, with exponential backoff and jitter. Mutating requests
  (later tickets) are never retried automatically: a timeout there means "outcome unknown"
  (FR-27), not "failed".
- Every failure leaves this module as a domain exception.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx2
import structlog

from guarded_exchange.domain.account import AccountBalances, Order, Page, Position
from guarded_exchange.domain.credentials import ApiKeyPair, KeyPermissions
from guarded_exchange.domain.errors import DomainError
from guarded_exchange.domain.instruments import Category, InstrumentRules
from guarded_exchange.domain.market import CandleInterval, Candles, Orderbook, Ticker
from guarded_exchange.infrastructure.bybit.errors import (
    RET_OK,
    RETRYABLE_RET_CODES,
    error_for_ret_code,
    unavailable,
)
from guarded_exchange.infrastructure.bybit.mapper import (
    KLINE_INTERVALS,
    OPEN_ORDERS_PAGE_MAX,
    UNIFIED_ACCOUNT,
    USDT_SETTLE,
    Envelope,
    balances_from_result,
    candles_from_result,
    key_permissions_from_result,
    open_orders_from_result,
    orderbook_from_result,
    parse_envelope,
    positions_from_result,
    rules_from_result,
    ticker_from_result,
)
from guarded_exchange.infrastructure.bybit.signing import signed_headers

DEMO_BASE_URL: Final = "https://api-demo.bybit.com"

_HTTP_FORBIDDEN: Final = 403
_HTTP_SERVER_ERROR: Final = 500
_LIMIT_RESET_HEADER: Final = "X-Bapi-Limit-Reset-Timestamp"
_MIN_RETRY_AFTER_S: Final = 1.0
_MAX_RETRY_AFTER_S: Final = 600.0

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
    """Public market data and signed private reads (``query-api``, account balances, positions,
    open orders); trading arrives in later tickets.

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

    async def get_instrument_rules(self, symbol: str, category: Category) -> InstrumentRules:
        envelope = await self._get(
            "/v5/market/instruments-info", {"category": category.value, "symbol": symbol}
        )
        return rules_from_result(envelope.result, symbol=symbol, category=category)

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
                query = urlencode(sorted(params.items()))
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


def _json_or_none(response: httpx2.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
