"""Signed ``GET /v5/user/query-api`` on ``httpx2.MockTransport`` (FR-49, ADR-002).

The signature is compared with reference vectors computed independently (OpenSSL); the response
is the one recorded on Bybit Demo Trading in spike 004 (``tests/fixtures/bybit/spike004``).
"""

import copy
import hashlib
import hmac
import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
import structlog

from guarded_exchange.domain.credentials import EXCHANGE_BYBIT, ApiKeyPair, assess_key
from guarded_exchange.domain.errors import ExchangeCredentialsInvalid, ExchangeUnavailable
from guarded_exchange.infrastructure.bybit.client import BybitClient, RetryPolicy
from guarded_exchange.infrastructure.bybit.http import make_http_client
from guarded_exchange.infrastructure.bybit.mapper import UNRECOGNISED_PERMISSION
from guarded_exchange.infrastructure.bybit.signing import sign
from guarded_exchange.infrastructure.logging import configure_logging
from tests.conftest import load_fixture

VECTORS = load_fixture("bybit/signature_vectors.json")
VECTOR_KEYS = ApiKeyPair(api_key=VECTORS["api_key"], api_secret=VECTORS["api_secret"])
QUERY_API = load_fixture("bybit/spike004/demo_query_api.json")
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
# Values a real response would carry in apiKey/secret: they must never reach logs or results.
LEAKY_API_KEY = "LEAKYAPIKEY0123456"
LEAKY_SECRET = "LEAKY-SECRET-abcdefghijklmnopqrstuv"  # noqa: S105 - test value


def _query_api(**result_changes: Any) -> dict[str, Any]:
    body = copy.deepcopy(QUERY_API)
    body["result"].update(apiKey=LEAKY_API_KEY, secret=LEAKY_SECRET, **result_changes)
    return body


class Recorder:
    def __init__(self, *responses: httpx2.Response) -> None:
        self._responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


def _client(recorder: Recorder, *, now_ms: int = int(VECTORS["timestamp"]), attempts: int = 1) -> BybitClient:
    http = make_http_client(
        "https://api-demo.bybit.com",
        connect_timeout_s=1,
        read_timeout_s=1,
        transport=httpx2.MockTransport(recorder),
    )

    async def no_sleep(_: float) -> None:
        return None

    return BybitClient(
        http, retry=RetryPolicy(max_attempts=attempts, base_delay_s=0), sleep=no_sleep, now_ms=lambda: now_ms
    )


@pytest.mark.req("FR-49")
@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["name"])
def test_sign_matches_the_reference_vectors(vector: dict[str, str]) -> None:
    assert (
        sign(
            VECTORS["api_secret"],
            VECTORS["timestamp"],
            VECTORS["api_key"],
            VECTORS["recv_window"],
            vector["payload"],
        )
        == vector["sign"]
    )


@pytest.mark.req("FR-49")
async def test_query_api_request_is_signed_like_the_reference() -> None:
    recorder = Recorder(httpx2.Response(200, json=_query_api()))

    await _client(recorder).get_key_permissions(VECTOR_KEYS)

    [request] = recorder.requests
    assert request.method == "GET"
    assert request.url.host == "api-demo.bybit.com"
    assert request.url.path == "/v5/user/query-api"
    assert request.url.query == b""
    assert request.headers["X-BAPI-API-KEY"] == VECTORS["api_key"]
    assert request.headers["X-BAPI-TIMESTAMP"] == VECTORS["timestamp"]
    assert request.headers["X-BAPI-RECV-WINDOW"] == VECTORS["recv_window"]
    assert request.headers["X-BAPI-SIGN"] == VECTORS["vectors"][0]["sign"]
    # and the signature covers what was actually sent (an independent recomputation)
    sent = f"{VECTORS['timestamp']}{VECTORS['api_key']}{VECTORS['recv_window']}{request.url.query.decode()}"
    expected = hmac.new(VECTORS["api_secret"].encode(), sent.encode(), hashlib.sha256).hexdigest()
    assert request.headers["X-BAPI-SIGN"] == expected


@pytest.mark.req("FR-49")
async def test_a_retry_is_signed_again_with_a_fresh_timestamp() -> None:
    recorder = Recorder(httpx2.Response(502), httpx2.Response(200, json=_query_api()))
    ticks = iter([1793168780000, 1793168781000])
    http = make_http_client(
        "https://api-demo.bybit.com",
        connect_timeout_s=1,
        read_timeout_s=1,
        transport=httpx2.MockTransport(recorder),
    )

    async def no_sleep(_: float) -> None:
        return None

    client = BybitClient(http, retry=RetryPolicy(max_attempts=2), sleep=no_sleep, now_ms=lambda: next(ticks))
    await client.get_key_permissions(VECTOR_KEYS)

    stamps = [r.headers["X-BAPI-TIMESTAMP"] for r in recorder.requests]
    assert stamps == ["1793168780000", "1793168781000"]
    assert len({r.headers["X-BAPI-SIGN"] for r in recorder.requests}) == 2


@pytest.mark.req("FR-49")
async def test_the_recorded_demo_key_is_mapped_and_rejected_by_the_allowlist() -> None:
    permissions = await _client(Recorder(httpx2.Response(200, json=_query_api()))).get_key_permissions(
        VECTOR_KEYS
    )

    assert permissions.permissions == {
        "ContractTrade.Order",
        "ContractTrade.Position",
        "Spot.SpotTrade",
        "Wallet.AccountTransfer",
        "Wallet.SubMemberTransferList",
        "Options.OptionsTrade",
        "Derivatives.DerivativesTrade",
        "Exchange.ExchangeHistory",
    }  # empty groups (CopyTrading, BlockTrade, NFT, Affiliate) mean no permission
    assert permissions.read_only is False
    assert permissions.expires_at == datetime(2027, 1, 29, 6, 13, 6, tzinfo=UTC)
    assert permissions.ip_restricted is True  # the recorded "ips" is a redacted non-empty list
    with pytest.raises(ExchangeCredentialsInvalid, match=r"Wallet\.AccountTransfer"):
        assess_key(EXCHANGE_BYBIT, permissions, NOW)


@pytest.mark.req("FR-49")
async def test_a_trading_only_read_only_key_without_ip_and_expiry_is_mapped() -> None:
    body = _query_api(
        readOnly=1,
        permissions={"ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"], "Wallet": [], "NFT": []},
        ips=["*"],
        expiredAt="1970-01-01T00:00:00Z",  # documented value for keys without expiry
    )

    permissions = await _client(Recorder(httpx2.Response(200, json=body))).get_key_permissions(VECTOR_KEYS)

    assert permissions.read_only is True
    assert permissions.permissions == {"ContractTrade.Order", "ContractTrade.Position", "Spot.SpotTrade"}
    assert permissions.expires_at is None
    assert permissions.ip_restricted is False
    assessment = assess_key(EXCHANGE_BYBIT, permissions, NOW)
    assert assessment.profile.value == "read_only"
    assert assessment.warnings == ("no_ip_whitelist",)


@pytest.mark.req("FR-49", "NFR-1")
async def test_a_permission_name_with_odd_characters_counts_as_an_unknown_permission() -> None:
    body = _query_api(permissions={"Spot": ["SpotTrade", "Trade; DROP TABLE"], "ContractTrade": []})

    permissions = await _client(Recorder(httpx2.Response(200, json=body))).get_key_permissions(VECTOR_KEYS)

    assert permissions.permissions == {"Spot.SpotTrade", UNRECOGNISED_PERMISSION}
    with pytest.raises(ExchangeCredentialsInvalid):
        assess_key(EXCHANGE_BYBIT, permissions, NOW)


@pytest.mark.req("FR-49", "FR-28")
@pytest.mark.parametrize("code", [10003, 10004, 10005, 10007, 10010, 33004])
async def test_a_key_the_exchange_does_not_accept_is_invalid_not_unverifiable(code: int) -> None:
    body = {"retCode": code, "retMsg": "API key is invalid.", "result": {}, "time": 1}

    with pytest.raises(ExchangeCredentialsInvalid) as err:
        await _client(Recorder(httpx2.Response(200, json=body))).get_key_permissions(VECTOR_KEYS)

    assert err.value.details == {"exchange_code": str(code)}
    assert err.value.untrusted_text == "API key is invalid."


@pytest.mark.req("FR-49")
@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(404, text="Not Found"),  # endpoint missing in some environment
        httpx2.Response(503),
        httpx2.Response(200, text="<html>"),
        httpx2.Response(200, json={"retCode": 0, "result": {"readOnly": 2, "permissions": {}}}),
        httpx2.Response(200, json={"retCode": 0, "result": {"readOnly": 0}}),
        httpx2.Response(
            200, json={"retCode": 0, "result": {"readOnly": 0, "permissions": {}, "expiredAt": "soon"}}
        ),
    ],
    ids=["404", "503", "not-json", "bad-readOnly", "no-permissions", "bad-expiry"],
)
async def test_permissions_that_cannot_be_read_are_unverifiable(response: httpx2.Response) -> None:
    with pytest.raises(ExchangeUnavailable):
        await _client(Recorder(response)).get_key_permissions(VECTOR_KEYS)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    yield stream
    structlog.reset_defaults()
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.mark.req("FR-31")
async def test_query_api_secrets_reach_neither_logs_nor_errors(log_stream: io.StringIO) -> None:
    ok = Recorder(httpx2.Response(200, json=_query_api()))
    malformed = Recorder(httpx2.Response(200, json=_query_api(readOnly="not-a-number")))
    broken_envelope = Recorder(httpx2.Response(200, json={"result": _query_api()["result"]}))  # no retCode

    permissions = await _client(ok).get_key_permissions(VECTOR_KEYS)
    errors = []
    for recorder in (malformed, broken_envelope):
        with pytest.raises(ExchangeUnavailable) as err:
            await _client(recorder).get_key_permissions(VECTOR_KEYS)
        errors.append(err.value)

    logging.getLogger("test").exception("boom", exc_info=errors[0])  # even a logged traceback is clean
    text = log_stream.getvalue() + repr(permissions) + "".join(f"{e!r}{e}{e.__cause__!r}" for e in errors)
    for secret in (LEAKY_API_KEY, LEAKY_SECRET, VECTORS["api_secret"]):
        assert secret not in text
    assert all(e.__cause__ is None for e in errors)
    assert json.loads(log_stream.getvalue().splitlines()[-1])["event"] == "boom"
