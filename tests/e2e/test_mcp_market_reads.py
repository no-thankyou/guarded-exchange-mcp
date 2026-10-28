"""In-process MCP ``Client`` against the fully wired server: order book, candles, instrument rules,
and argument validation at the MCP boundary (``GuardedMCPServer``, ADR-013, spike 002).

The real Bybit adapter runs on ``httpx2.MockTransport`` with public responses recorded from
api-demo.bybit.com; nothing leaves the process.
"""

import json
import typing
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import jsonschema
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, Tool

from guarded_exchange.bootstrap import app_server
from guarded_exchange.config import Settings
from guarded_exchange.domain.market import CandleInterval
from guarded_exchange.interface.mcp.guarded_server import GuardedMCPServer
from guarded_exchange.interface.mcp.server import create_server
from guarded_exchange.interface.mcp.tools.market import IntervalLiteral
from tests.conftest import load_fixture
from tests.fakes import FakeExchange, use_cases_on

pytestmark = pytest.mark.req("FR-1", "FR-3")

_ROUTES = {
    ("/v5/market/orderbook", "linear", "BTCUSDT"): "bybit/orderbook_linear_btcusdt_5.json",
    ("/v5/market/orderbook", "spot", "ETHUSDT"): "bybit/orderbook_spot_ethusdt_5.json",
    ("/v5/market/kline", "linear", "BTCUSDT"): "bybit/kline_linear_btcusdt_1h_3.json",
    ("/v5/market/kline", "spot", "ETHUSDT"): "bybit/kline_spot_ethusdt_1d_3.json",
    ("/v5/market/instruments-info", "linear", "BTCUSDT"): "bybit/instruments_linear_btcusdt.json",
    ("/v5/market/instruments-info", "spot", "ETHUSDT"): "bybit/instruments_spot_ethusdt.json",
    ("/v5/market/instruments-info", "spot", "ETHBTC"): "bybit/instruments_spot_ethbtc.json",
    ("/v5/market/tickers", "linear", "BTCUSDT"): "bybit/tickers_linear_btcusdt.json",
}


class BybitStub:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        key = (request.url.path, request.url.params["category"], request.url.params["symbol"])
        if key in _ROUTES:
            return httpx2.Response(200, json=load_fixture(_ROUTES[key]))
        return httpx2.Response(200, json=load_fixture("bybit/error_10001_not_supported_symbol.json"))


@pytest.fixture
def stub() -> BybitStub:
    return BybitStub()


@pytest.fixture
async def server(stub: BybitStub) -> AsyncIterator[MCPServer[Any]]:
    settings = Settings(_env_file=None, bybit_get_max_attempts=1)
    async with app_server(settings, transport=httpx2.MockTransport(stub)) as mcp:
        yield mcp


async def _call(server: MCPServer[Any], name: str, args: dict[str, Any]) -> tuple[Tool, CallToolResult]:
    async with Client(server) as client:
        tools = await client.list_tools()
        result = await client.call_tool(name, args)
    return next(t for t in tools.tools if t.name == name), result


def _ok(tool: Tool, result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is False, result.structured_content
    assert result.structured_content is not None
    assert tool.output_schema is not None
    jsonschema.validate(result.structured_content, tool.output_schema)
    assert None not in result.structured_content.values()  # FR-3: absent fields are omitted
    assert json.loads(result.content[0].text) == result.structured_content  # type: ignore[union-attr]
    out: dict[str, Any] = result.structured_content
    return out


def _error(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is True
    assert result.structured_content is not None, result.content
    assert result.structured_content["next_step"]
    error: dict[str, Any] = result.structured_content
    return error


# --- the three tools through the in-process client -------------------------------------------


def test_server_is_the_guarded_subclass() -> None:
    assert isinstance(create_server(use_cases_on(FakeExchange())), GuardedMCPServer)


async def test_market_tools_are_listed_read_only_with_schemas(server: MCPServer[Any]) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    for name in ("get_ticker", "get_orderbook", "get_candles", "get_instrument_rules"):
        tool = tools[name]
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    orderbook = tools["get_orderbook"].input_schema
    assert orderbook["required"] == ["symbol", "category"]
    assert (orderbook["properties"]["depth"]["minimum"], orderbook["properties"]["depth"]["maximum"]) == (
        1,
        50,
    )
    candles = tools["get_candles"].input_schema
    assert candles["required"] == ["symbol", "category", "interval"]
    assert candles["properties"]["interval"]["enum"] == [i.value for i in CandleInterval]
    assert candles["properties"]["limit"]["maximum"] == 200


@pytest.mark.parametrize(
    ("args", "best_bid_ask"),
    [
        ({"symbol": "BTCUSDT", "category": "linear"}, (["80865.3", "2.806"], ["80865.4", "3.743"])),
        ({"symbol": "ethusdt", "category": "spot"}, (["2352.49", "3.60849"], ["2352.5", "3.26662"])),
    ],
)
async def test_get_orderbook(
    server: MCPServer[Any], stub: BybitStub, args: dict[str, str], best_bid_ask: tuple[list[str], list[str]]
) -> None:
    out = _ok(*await _call(server, "get_orderbook", {**args, "depth": 3}))

    assert out["symbol"] == args["symbol"].upper()
    assert (out["bids"][0], out["asks"][0]) == best_bid_ask
    assert len(out["bids"]) == len(out["asks"]) == 3  # the recorded book has 5 levels: cut to depth
    assert stub.requests[0].url.params["limit"] == "3"


async def test_get_orderbook_default_depth(server: MCPServer[Any], stub: BybitStub) -> None:
    _ok(*await _call(server, "get_orderbook", {"symbol": "BTCUSDT", "category": "linear"}))

    assert stub.requests[0].url.params["limit"] == "10"


@pytest.mark.parametrize(
    ("args", "bybit_interval", "last_close"),
    [
        ({"symbol": "BTCUSDT", "category": "linear", "interval": "1h"}, "60", "80865.4"),
        ({"symbol": "ETHUSDT", "category": "spot", "interval": "1d"}, "D", "2352.5"),
    ],
)
async def test_get_candles(
    server: MCPServer[Any], stub: BybitStub, args: dict[str, str], bybit_interval: str, last_close: str
) -> None:
    out = _ok(*await _call(server, "get_candles", {**args, "limit": 3}))

    assert out["interval"] == args["interval"]
    assert out["columns"] == ["start_at", "open", "high", "low", "close", "volume_base"]
    assert len(out["candles"]) == 3
    starts = [row[0] for row in out["candles"]]
    assert starts == sorted(starts)  # oldest first
    assert out["candles"][-1][4] == last_close
    assert stub.requests[0].url.params["interval"] == bybit_interval


@pytest.mark.parametrize(
    ("symbol", "category", "expected"),
    [
        (
            "BTCUSDT",
            "linear",
            {
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "status": "trading",
                "tick_size": "0.1",
                "qty_step": "0.001",
                "min_qty": "0.001",
                "min_notional": "5",
                "max_leverage": "150",
            },
        ),
        (
            "ETHUSDT",
            "spot",
            {
                "base_asset": "ETH",
                "quote_asset": "USDT",
                "status": "trading",
                "tick_size": "0.01",
                "qty_step": "0.00001",
                "min_qty": "0.00001",
                "min_notional": "5",
            },
        ),
    ],
)
async def test_get_instrument_rules(
    server: MCPServer[Any], symbol: str, category: str, expected: dict[str, str]
) -> None:
    out = _ok(*await _call(server, "get_instrument_rules", {"symbol": symbol, "category": category}))

    assert out == {"symbol": symbol, "category": category, **expected}


async def test_instrument_rules_are_cached_across_calls(server: MCPServer[Any], stub: BybitStub) -> None:
    async with Client(server) as client:
        for _ in range(3):
            result = await client.call_tool(
                "get_instrument_rules", {"symbol": "BTCUSDT", "category": "linear"}
            )
            assert result.is_error is False

    assert len(stub.requests) == 1


@pytest.mark.req("FR-4", "FR-28")
async def test_instrument_not_quoted_in_usdt_is_invalid_params(server: MCPServer[Any]) -> None:
    _, result = await _call(server, "get_instrument_rules", {"symbol": "ETHBTC", "category": "spot"})

    error = _error(result)
    assert error["code"] == "invalid_params"
    assert error["details"] == {"field": "symbol", "reason": "quote_not_supported", "quote": "BTC"}


@pytest.mark.req("FR-28", "FR-41")
async def test_exchange_rejection_keeps_exchange_text_in_untrusted_text(server: MCPServer[Any]) -> None:
    _, result = await _call(
        server, "get_candles", {"symbol": "NOPEUSDT", "category": "spot", "interval": "1h"}
    )

    error = _error(result)
    assert error["code"] == "invalid_params"
    assert error["untrusted_text"] == "Not supported symbols"


# --- argument validation at the MCP boundary (GuardedMCPServer) ------------------------------


async def test_get_ticker_input_schema_still_has_enum_and_pattern(server: MCPServer[Any]) -> None:
    async with Client(server) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "get_ticker")

    props = tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["symbol", "category"]
    assert props["category"]["enum"] == ["spot", "linear"]
    assert props["symbol"]["pattern"] == "^[A-Za-z0-9]{2,30}$"
    assert props["symbol"]["description"]
    assert props["category"]["description"]


@pytest.mark.req("FR-28")
@pytest.mark.parametrize(
    ("args", "field", "reason"),
    [
        ({"symbol": "BTCUSDT"}, "category", "missing"),
        ({"category": "linear"}, "symbol", "missing"),
        ({"symbol": 42, "category": "linear"}, "symbol", "string_type"),
        ({"symbol": "BTCUSDT", "category": ["linear"]}, "category", "literal_error"),
        ({"symbol": "BTCUSDT", "category": "inverse"}, "category", "literal_error"),
        ({"symbol": "BTC-USDT", "category": "linear"}, "symbol", "string_pattern_mismatch"),
    ],
    ids=[
        "missing-category",
        "missing-symbol",
        "symbol-not-string",
        "category-wrong-type",
        "category-not-in-enum",
        "symbol-pattern",
    ],
)
async def test_get_ticker_bad_arguments_are_invalid_params(
    server: MCPServer[Any], stub: BybitStub, args: dict[str, Any], field: str, reason: str
) -> None:
    _, result = await _call(server, "get_ticker", args)

    error = _error(result)
    assert error["code"] == "invalid_params"
    assert error["details"] == {"field": field, "reason": reason}
    assert error["message"].startswith(f"invalid arguments: {field}: ")
    assert json.loads(result.content[0].text) == error  # type: ignore[union-attr]
    assert stub.requests == []  # rejected before any use case or exchange call


@pytest.mark.req("FR-28")
@pytest.mark.parametrize(
    ("tool", "args", "field", "reason"),
    [
        (
            "get_orderbook",
            {"symbol": "BTCUSDT", "category": "linear", "depth": 51},
            "depth",
            "less_than_equal",
        ),
        (
            "get_orderbook",
            {"symbol": "BTCUSDT", "category": "linear", "depth": "lots"},
            "depth",
            "int_parsing",
        ),
        (
            "get_candles",
            {"symbol": "BTCUSDT", "category": "linear", "interval": "7m"},
            "interval",
            "literal_error",
        ),
        (
            "get_candles",
            {"symbol": "BTCUSDT", "category": "linear", "interval": "1h", "limit": 0},
            "limit",
            "greater_than_equal",
        ),
        ("get_instrument_rules", {"symbol": "BTCUSDT"}, "category", "missing"),
    ],
)
async def test_new_tools_bad_arguments_are_invalid_params(
    server: MCPServer[Any], tool: str, args: dict[str, Any], field: str, reason: str
) -> None:
    _, result = await _call(server, tool, args)

    error = _error(result)
    assert error["code"] == "invalid_params"
    assert error["details"] == {"field": field, "reason": reason}


@pytest.mark.req("FR-28")
async def test_several_problems_are_listed_and_values_are_not_echoed(server: MCPServer[Any]) -> None:
    pasted = "sk-live-SECRET-123"
    _, result = await _call(server, "get_ticker", {"symbol": pasted, "category": pasted})

    error = _error(result)
    assert error["details"] == {"field": "symbol", "reason": "string_pattern_mismatch"}
    assert "symbol: " in error["message"]
    assert "category: " in error["message"]
    assert "SECRET" not in result.model_dump_json()


async def test_extra_arguments_are_ignored(server: MCPServer[Any]) -> None:
    _, result = await _call(
        server, "get_ticker", {"symbol": "BTCUSDT", "category": "linear", "confirmed": True}
    )

    assert result.is_error is False


async def test_unknown_tool_keeps_the_sdk_handling(server: MCPServer[Any]) -> None:
    async with Client(server) as client:
        result = await client.call_tool("withdraw", {"amount": "1"})

    assert result.is_error is True
    assert result.structured_content is None


def test_interval_literal_matches_the_domain_enum() -> None:
    assert list(typing.get_args(IntervalLiteral)) == [i.value for i in CandleInterval]
