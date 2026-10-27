"""Bybit adapter on ``httpx2.MockTransport`` with responses recorded from api-demo.bybit.com."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from guarded_exchange.domain.errors import ExchangeRejected, ExchangeUnavailable, InvalidParams, NotFound
from guarded_exchange.domain.instruments import Category
from guarded_exchange.infrastructure.bybit.client import BybitClient, RetryPolicy
from guarded_exchange.infrastructure.bybit.http import make_http_client
from tests.conftest import load_fixture

pytestmark = pytest.mark.req("FR-1")

_LINEAR = load_fixture("bybit/tickers_linear_btcusdt.json")

Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    """MockTransport handler that replays a scripted sequence and records requests."""

    def __init__(self, *steps: httpx2.Response | Exception) -> None:
        self._steps = list(steps)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        if isinstance(step, Exception):
            raise step
        return step


class SleepSpy:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _client(handler: Handler, *, attempts: int = 3) -> tuple[BybitClient, SleepSpy]:
    http = make_http_client(
        "https://api-demo.bybit.com",
        connect_timeout_s=1,
        read_timeout_s=1,
        transport=httpx2.MockTransport(handler),
    )
    sleep = SleepSpy()
    return BybitClient(http, retry=RetryPolicy(max_attempts=attempts, base_delay_s=0.1), sleep=sleep), sleep


def _json(payload: dict[str, Any], status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json=payload)


async def test_linear_ticker_fields_are_mapped_to_decimals() -> None:
    recorder = Recorder(_json(load_fixture("bybit/tickers_linear_btcusdt.json")))
    client, _ = _client(recorder)

    ticker = await client.get_ticker("BTCUSDT", Category.LINEAR)

    assert ticker.symbol == "BTCUSDT"
    assert ticker.category is Category.LINEAR
    assert ticker.last_price == Decimal("80571.10")
    assert isinstance(ticker.last_price, Decimal)
    assert ticker.bid_price == Decimal("80571.10")
    assert ticker.ask_price == Decimal("80571.20")
    assert ticker.high_24h == Decimal("80800.00")
    assert ticker.low_24h == Decimal("79458.90")
    assert ticker.change_24h_pct == Decimal("0.6848")  # 0.006848 * 100, exact
    assert ticker.volume_24h_base == Decimal("43556.0370")
    assert ticker.turnover_24h_quote == Decimal("3486773303.0305")
    assert ticker.mark_price == Decimal("80571.10")
    assert ticker.index_price == Decimal("80603.97")
    assert ticker.funding_rate_pct == Decimal("0.0100")
    assert ticker.next_funding_at == datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
    assert ticker.observed_at == datetime(2026, 10, 27, 18, 53, 43, 449000, tzinfo=UTC)

    (request,) = recorder.requests
    assert request.method == "GET"
    assert request.url.path == "/v5/market/tickers"
    assert dict(request.url.params) == {"category": "linear", "symbol": "BTCUSDT"}


async def test_spot_ticker_has_no_derivative_fields() -> None:
    client, _ = _client(Recorder(_json(load_fixture("bybit/tickers_spot_ethusdt.json"))))

    ticker = await client.get_ticker("ETHUSDT", Category.SPOT)

    assert ticker.last_price == Decimal("2343.49")
    assert ticker.change_24h_pct == Decimal("1.24")
    assert ticker.mark_price is None
    assert ticker.funding_rate_pct is None
    assert ticker.next_funding_at is None


async def test_empty_strings_become_none_not_zero() -> None:
    payload = load_fixture("bybit/tickers_spot_ethusdt.json")
    payload["result"]["list"][0]["bid1Price"] = ""
    client, _ = _client(Recorder(_json(payload)))

    ticker = await client.get_ticker("ETHUSDT", Category.SPOT)

    assert ticker.bid_price is None


@pytest.mark.req("FR-28")
async def test_timeout_is_retried_then_becomes_exchange_unavailable() -> None:
    recorder = Recorder(httpx2.ReadTimeout("read timed out"))
    client, sleep = _client(recorder, attempts=3)

    with pytest.raises(ExchangeUnavailable) as err:
        await client.get_ticker("BTCUSDT", Category.LINEAR)

    assert err.value.code == "exchange_unavailable"
    assert "timeout" in err.value.message
    assert len(recorder.requests) == 3
    assert len(sleep.delays) == 2
    assert sleep.delays[0] < sleep.delays[1]  # exponential backoff


@pytest.mark.req("FR-28")
async def test_connect_error_is_exchange_unavailable() -> None:
    client, _ = _client(Recorder(httpx2.ConnectError("connection refused")), attempts=1)

    with pytest.raises(ExchangeUnavailable):
        await client.get_ticker("BTCUSDT", Category.LINEAR)


async def test_transient_failure_then_success_is_retried_transparently() -> None:
    recorder = Recorder(
        httpx2.Response(502, text="bad gateway"),
        _json({"retCode": 10016, "retMsg": "Server error", "result": {}, "time": 1}),
        _json(load_fixture("bybit/tickers_linear_btcusdt.json")),
    )
    client, sleep = _client(recorder, attempts=3)

    ticker = await client.get_ticker("BTCUSDT", Category.LINEAR)

    assert ticker.last_price == Decimal("80571.10")
    assert len(recorder.requests) == 3
    assert len(sleep.delays) == 2


@pytest.mark.req("FR-28")
async def test_param_error_ret_code_is_invalid_params_with_untrusted_exchange_text() -> None:
    recorder = Recorder(_json(load_fixture("bybit/error_10001_not_supported_symbol.json")))
    client, sleep = _client(recorder)

    with pytest.raises(InvalidParams) as err:
        await client.get_ticker("NOPEUSDT", Category.SPOT)

    assert err.value.details == {"exchange_code": "10001"}
    assert err.value.untrusted_text == "Not supported symbols"
    assert "Not supported" not in err.value.message  # exchange text never becomes our message
    assert len(recorder.requests) == 1  # not retried
    assert sleep.delays == []


@pytest.mark.req("FR-28")
async def test_unknown_ret_code_is_exchange_rejected() -> None:
    client, _ = _client(Recorder(_json({"retCode": 33004, "retMsg": "api key expired", "result": {}})))

    with pytest.raises(ExchangeRejected) as err:
        await client.get_ticker("BTCUSDT", Category.LINEAR)

    assert err.value.details == {"exchange_code": "33004"}


@pytest.mark.req("FR-28")
async def test_rate_limit_ret_code_is_not_retried() -> None:
    recorder = Recorder(_json({"retCode": 10006, "retMsg": "Too many visits!", "result": {}, "time": 1}))
    client, _ = _client(recorder)

    with pytest.raises(ExchangeUnavailable):
        await client.get_ticker("BTCUSDT", Category.LINEAR)

    assert len(recorder.requests) == 1


@pytest.mark.req("FR-28")
@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(403, text="access too frequent"),
        httpx2.Response(404, text="not found"),
        httpx2.Response(200, text="<html>maintenance</html>"),
        httpx2.Response(200, json={"unexpected": True}),
        httpx2.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": "nope"}, "time": 1}),
        httpx2.Response(200, json={k: v for k, v in _LINEAR.items() if k != "time"}),
    ],
    ids=["http-403", "http-404", "not-json", "no-envelope", "bad-result", "no-timestamp"],
)
async def test_malformed_or_refused_responses_are_exchange_unavailable(response: httpx2.Response) -> None:
    recorder = Recorder(response)
    client, _ = _client(recorder)

    with pytest.raises(ExchangeUnavailable):
        await client.get_ticker("BTCUSDT", Category.LINEAR)

    assert len(recorder.requests) == 1


@pytest.mark.req("FR-28")
async def test_empty_list_is_not_found() -> None:
    client, _ = _client(Recorder(_json({"retCode": 0, "retMsg": "OK", "result": {"list": []}, "time": 1})))

    with pytest.raises(NotFound):
        await client.get_ticker("BTCUSDT", Category.LINEAR)


async def test_unparsable_number_is_exchange_unavailable() -> None:
    payload = load_fixture("bybit/tickers_linear_btcusdt.json")
    payload["result"]["list"][0]["lastPrice"] = "NaN"
    client, _ = _client(Recorder(_json(payload)))

    with pytest.raises(ExchangeUnavailable):
        await client.get_ticker("BTCUSDT", Category.LINEAR)


def test_retry_delay_grows_and_is_capped() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_s=0.5, max_delay_s=1.0)

    delays = [policy.delay(a) for a in range(4)]

    assert 0.5 <= delays[0] <= 0.55
    assert 1.0 <= delays[1] <= 1.1
    assert all(d <= 1.1 for d in delays[1:])


async def test_missing_last_price_is_exchange_unavailable() -> None:
    payload = load_fixture("bybit/tickers_linear_btcusdt.json")
    payload["result"]["list"][0]["lastPrice"] = ""
    client, _ = _client(Recorder(_json(payload)))

    with pytest.raises(ExchangeUnavailable):
        await client.get_ticker("BTCUSDT", Category.LINEAR)


def test_http_client_has_explicit_split_timeouts() -> None:
    http = make_http_client("https://api-demo.bybit.com", connect_timeout_s=2.5, read_timeout_s=7.0)

    assert http.timeout.connect == 2.5
    assert http.timeout.read == 7.0
    assert str(http.base_url) == "https://api-demo.bybit.com"
