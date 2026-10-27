"""In-process MCP ``Client`` against the fully wired server (real Bybit adapter on MockTransport)."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import jsonschema
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import Tool

from guarded_exchange.application.read_market import GetTicker
from guarded_exchange.bootstrap import app_server
from guarded_exchange.config import Settings
from guarded_exchange.domain.errors import ExchangeUnavailable
from guarded_exchange.interface.mcp.server import UseCases, create_server
from tests.conftest import load_fixture
from tests.fakes import FakeExchange

pytestmark = pytest.mark.req("FR-1", "FR-3")

_FIXTURES = {
    ("linear", "BTCUSDT"): "bybit/tickers_linear_btcusdt.json",
    ("spot", "ETHUSDT"): "bybit/tickers_spot_ethusdt.json",
}


def _bybit_stub(request: httpx2.Request) -> httpx2.Response:
    key = (request.url.params["category"], request.url.params["symbol"])
    if key in _FIXTURES:
        return httpx2.Response(200, json=load_fixture(_FIXTURES[key]))
    if request.url.params["symbol"] == "SLOWUSDT":
        raise httpx2.ConnectTimeout("connect timed out")
    return httpx2.Response(200, json=load_fixture("bybit/error_10001_not_supported_symbol.json"))


@pytest.fixture
async def server() -> AsyncIterator[MCPServer[Any]]:
    settings = Settings(_env_file=None, bybit_get_max_attempts=1)
    async with app_server(settings, transport=httpx2.MockTransport(_bybit_stub)) as mcp:
        yield mcp


async def _tool(client: Client, name: str) -> Tool:
    tools = await client.list_tools()
    return next(t for t in tools.tools if t.name == name)


async def test_tools_list_exposes_get_ticker_with_schemas_and_read_only_hint(server: MCPServer[Any]) -> None:
    async with Client(server) as client:
        tool = await _tool(client, "get_ticker")

    assert tool.input_schema["required"] == ["symbol", "category"]
    assert tool.input_schema["properties"]["category"]["enum"] == ["spot", "linear"]
    assert tool.output_schema is not None
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True


@pytest.mark.parametrize(
    ("symbol", "category", "last_price"),
    [("BTCUSDT", "linear", "80571.1"), ("ethusdt", "spot", "2343.49")],
)
async def test_get_ticker_structured_content_matches_output_schema(
    server: MCPServer[Any], symbol: str, category: str, last_price: str
) -> None:
    async with Client(server) as client:
        tool = await _tool(client, "get_ticker")
        result = await client.call_tool("get_ticker", {"symbol": symbol, "category": category})

    assert result.is_error is False
    assert result.structured_content is not None
    assert tool.output_schema is not None
    jsonschema.validate(result.structured_content, tool.output_schema)
    assert result.structured_content["last_price"] == last_price
    assert result.structured_content["category"] == category
    # FR-3: compact - fields that do not apply are omitted, not null
    assert None not in result.structured_content.values()
    # text content mirrors the structured content for clients that only show text
    assert json.loads(result.content[0].text) == result.structured_content  # type: ignore[union-attr]


@pytest.mark.req("FR-28")
@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"symbol": "BTC-USDT", "category": "linear"}, "invalid_params"),
        ({"symbol": "BTCUSDT", "category": "inverse"}, "invalid_params"),
        ({"symbol": "NOPEUSDT", "category": "spot"}, "invalid_params"),
        ({"symbol": "SLOWUSDT", "category": "linear"}, "exchange_unavailable"),
    ],
    ids=["bad-symbol", "bad-category", "exchange-10001", "timeout"],
)
async def test_errors_are_is_error_with_code_and_next_step(
    server: MCPServer[Any], args: dict[str, str], code: str
) -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_ticker", args)

    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["code"] == code
    assert result.structured_content["next_step"]


@pytest.mark.req("FR-28", "FR-41")
async def test_exchange_text_is_returned_only_as_untrusted_text(server: MCPServer[Any]) -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_ticker", {"symbol": "NOPEUSDT", "category": "spot"})

    assert result.structured_content is not None
    assert result.structured_content["untrusted_text"] == "Not supported symbols"
    assert "Not supported" not in result.structured_content["message"]


@pytest.mark.req("NFR-5")
async def test_server_works_on_fake_exchange_without_any_bybit_code() -> None:
    exchange = FakeExchange(fail_with=ExchangeUnavailable("the exchange is unavailable: maintenance"))
    mcp = create_server(UseCases(get_ticker=GetTicker(exchange)))

    async with Client(mcp) as client:
        result = await client.call_tool("get_ticker", {"symbol": "BTCUSDT", "category": "linear"})

    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["code"] == "exchange_unavailable"
