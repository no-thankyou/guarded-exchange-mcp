"""``cancel_order``, ``amend_order`` and ``cancel_all_orders`` through an in-process MCP ``Client`` on
fakes (ticket 013: FR-5, FR-11, FR-21, FR-28, FR-41, ADR-013)."""

from typing import Any

import jsonschema
import pytest
from mcp import Client
from mcp.types import CallToolResult, Tool

from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.domain.account import OrderOrigin, OrderSide
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from tests.e2e.support import server_for
from tests.e2e.test_mcp_place_order import KEYS, TRADING_KEY
from tests.fakes import FakeCipher, FakeClock, FakeExchange, FakeTrading, InMemoryDatabase, make_rules
from tests.fakes_trading import FakeOrderChanges, plain_order

pytestmark = pytest.mark.req("FR-5")

TOOLS = ("cancel_order", "amend_order", "cancel_all_orders")


class Setup:
    def __init__(self) -> None:
        self.db, self.clock, cipher = InMemoryDatabase(), FakeClock(), FakeCipher()
        self.user = self.db.add_user()
        self.db.add_credential(self.user, cipher, KEYS, permissions=TRADING_KEY)
        self.changes = FakeOrderChanges()
        exchange = FakeExchange(rules={("BTCUSDT", Category.LINEAR): make_rules()})
        self.server = server_for(
            self.db,
            self.user.id,
            exchange=exchange,
            cipher=cipher,
            trading=FakeTrading(),
            clock=self.clock,
            changes=self.changes,
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
            result = await client.call_tool(name, args)
        return tools, result


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None
    return dict(result.structured_content)


@pytest.mark.req("FR-21", "FR-41")
async def test_cancel_all_through_mcp_names_each_order_and_counts_external_ones() -> None:
    setup = Setup()
    await setup.grant()
    setup.changes.add(
        plain_order("ours"),
        plain_order("manual", origin=OrderOrigin.EXTERNAL, side=OrderSide.SELL, price="81000"),
    )

    tools, result = await setup.call("cancel_all_orders", {"symbol": "BTCUSDT", "category": "linear"})

    assert not result.is_error
    out = _structured(result)
    tool = next(t for t in tools if t.name == "cancel_all_orders")
    assert tool.output_schema is not None
    jsonschema.validate(out, tool.output_schema)
    assert out["external_count"] == 1
    assert [(o["order"]["order_id"], o["order"]["origin"], o["status"]) for o in out["orders"]] == [
        ("ours", "system", "closed"),
        ("manual", "external", "closed"),
    ]


async def test_cancel_and_amend_answer_with_their_schemas() -> None:
    setup = Setup()
    await setup.grant()
    setup.changes.add(plain_order("o-1"), plain_order("o-2"))

    tools, cancelled = await setup.call(
        "cancel_order", {"symbol": "BTCUSDT", "category": "linear", "order_id": "o-1"}
    )
    _, amended = await setup.call(
        "amend_order", {"symbol": "BTCUSDT", "category": "linear", "order_id": "o-2", "price": "79990.5"}
    )

    by_name = {t.name: t for t in tools}
    for name, result in (("cancel_order", cancelled), ("amend_order", amended)):
        assert not result.is_error, result
        schema = by_name[name].output_schema
        assert schema is not None
        jsonschema.validate(_structured(result), schema)
    assert _structured(cancelled)["status"] == "cancelled"
    assert (_structured(amended)["status"], _structured(amended)["risk"]) == ("amended", "reduces")


@pytest.mark.req("FR-11")
async def test_a_read_only_user_neither_sees_nor_calls_the_order_changes() -> None:
    setup = Setup()
    await setup.grant(Profile.READ_ONLY)
    setup.changes.add(plain_order("o-1"))

    tools, result = await setup.call(
        "cancel_order", {"symbol": "BTCUSDT", "category": "linear", "order_id": "o-1"}
    )

    assert not {t.name for t in tools} & set(TOOLS)
    assert result.is_error
    assert _structured(result)["code"] == "permission_denied"
    assert setup.changes.cancels == []


@pytest.mark.req("FR-28")
async def test_a_float_qty_or_a_bad_order_id_is_invalid_params() -> None:
    setup = Setup()
    await setup.grant()

    _, as_float = await setup.call(
        "amend_order", {"symbol": "BTCUSDT", "category": "linear", "order_id": "o-1", "qty": 0.002}
    )
    _, bad_id = await setup.call(
        "cancel_order", {"symbol": "BTCUSDT", "category": "linear", "order_id": "a b"}
    )

    assert _structured(as_float)["code"] == "invalid_params"
    assert _structured(bad_id)["code"] == "invalid_params"
    assert setup.changes.amends == []
