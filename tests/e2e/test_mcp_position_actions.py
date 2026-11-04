"""``set_trading_stop``, ``close_position`` and ``set_leverage`` through an in-process MCP ``Client`` on
fakes (ticket 014: FR-6 - FR-8, FR-11, FR-28)."""

from decimal import Decimal
from typing import Any

import jsonschema
import pytest
from mcp import Client
from mcp.types import CallToolResult, Tool

from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.domain.account import SymbolPosition
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from tests.e2e.support import server_for
from tests.e2e.test_mcp_place_order import KEYS, TRADING_KEY
from tests.fakes import (
    FakeCipher,
    FakeClock,
    FakeExchange,
    FakeTrading,
    InMemoryDatabase,
    make_position,
    make_rules,
    make_ticker,
)
from tests.fakes_trading import FakePositionActions

pytestmark = pytest.mark.req("FR-7")

TOOLS = ("set_trading_stop", "close_position", "set_leverage")
ARGS: dict[str, dict[str, Any]] = {
    "set_trading_stop": {"stop_loss": "79000.5"},
    "close_position": {},
    "set_leverage": {"leverage": "5"},
}


class Setup:
    def __init__(self) -> None:
        self.db, self.clock, cipher = InMemoryDatabase(), FakeClock(), FakeCipher()
        self.user = self.db.add_user()
        self.db.add_credential(self.user, cipher, KEYS, permissions=TRADING_KEY)
        self.trading = FakeTrading()
        self.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", make_position(), Decimal(10))
        self.actions = FakePositionActions(self.trading)
        exchange = FakeExchange(
            tickers={("BTCUSDT", Category.LINEAR): make_ticker()},
            rules={("BTCUSDT", Category.LINEAR): make_rules()},
        )
        self.server = server_for(
            self.db,
            self.user.id,
            exchange=exchange,
            cipher=cipher,
            trading=self.trading,
            clock=self.clock,
            position_actions=self.actions,
        )

    async def grant(self, profile: Profile = Profile.TRADE) -> None:
        patch = PolicyPatch(
            profile=profile,
            allowlist=frozenset({InstrumentRef(Category.LINEAR, "BTCUSDT")}),
            confirm_actions=frozenset(),
        )
        await SetCeiling(self.db, self.clock)(self.user.name, patch)

    async def call(self, name: str, args: dict[str, Any]) -> tuple[list[Tool], CallToolResult]:
        async with Client(self.server) as client:
            tools = (await client.list_tools()).tools
            result = await client.call_tool(name, {"symbol": "BTCUSDT", "category": "linear", **args})
        return tools, result


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None
    return dict(result.structured_content)


@pytest.mark.parametrize("name", TOOLS)
async def test_each_tool_answers_with_its_output_schema(name: str) -> None:
    setup = Setup()
    await setup.grant()

    tools, result = await setup.call(name, ARGS[name])

    assert not result.is_error, result
    tool = next(t for t in tools if t.name == name)
    assert tool.output_schema is not None
    jsonschema.validate(_structured(result), tool.output_schema)


async def test_stop_and_leverage_report_what_the_position_shows() -> None:
    setup = Setup()
    await setup.grant()

    _, stop = await setup.call("set_trading_stop", ARGS["set_trading_stop"])
    _, leverage = await setup.call("set_leverage", ARGS["set_leverage"])

    assert (_structured(stop)["status"], _structured(stop)["stop_loss"]) == ("applied", "79000.5")
    out = _structured(leverage)
    assert (out["status"], out["risk"], out["previous_leverage"]) == ("applied", "reduces", "10")


@pytest.mark.req("FR-6", "FR-7", "FR-8")
@pytest.mark.parametrize("name", TOOLS)
async def test_spot_gets_a_plain_refusal(name: str) -> None:
    setup = Setup()
    await setup.grant()

    _, result = await setup.call(name, {**ARGS[name], "category": "spot"})

    assert result.is_error
    assert _structured(result)["code"] == "invalid_params"
    assert (setup.actions.stops, setup.actions.leverages, setup.trading.sent) == ([], [], [])


@pytest.mark.req("FR-11")
async def test_a_read_only_user_neither_sees_nor_calls_the_position_actions() -> None:
    setup = Setup()
    await setup.grant(Profile.READ_ONLY)

    tools, result = await setup.call("set_leverage", ARGS["set_leverage"])

    assert not {t.name for t in tools} & set(TOOLS)
    assert result.is_error
    assert _structured(result)["code"] == "permission_denied"
    assert setup.actions.leverages == []


@pytest.mark.req("FR-28")
async def test_a_float_price_is_invalid_params() -> None:
    setup = Setup()
    await setup.grant()

    _, result = await setup.call("set_trading_stop", {"stop_loss": 79000.5})

    assert _structured(result)["code"] == "invalid_params"
    assert setup.actions.stops == []
