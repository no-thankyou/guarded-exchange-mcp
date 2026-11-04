"""``place_order`` through an in-process MCP ``Client`` on fakes (ticket 011: FR-4, FR-25, FR-36,
D-4, D-15, ADR-013)."""

import json
import os
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx2
import jsonschema
import pytest
from mcp import Client
from mcp.types import CallToolResult, Tool

from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.bootstrap import app_server
from guarded_exchange.config import Settings
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.credentials import ApiKeyPair, KeyPermissions
from guarded_exchange.domain.idempotency import IDEMPOTENCY_KEY_PATTERN
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.money import AMOUNT_PATTERN
from guarded_exchange.domain.orders import FillStatus, OrderFill
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from guarded_exchange.infrastructure.crypto import AesGcmCredentialCipher, Keyring
from guarded_exchange.interface.mcp.access import fixed_principal
from tests.conftest import load_fixture
from tests.e2e.support import server_for
from tests.fakes import (
    FakeCipher,
    FakeClock,
    FakeExchange,
    FakeTrading,
    InMemoryDatabase,
    make_rules,
    make_ticker,
)

pytestmark = pytest.mark.req("FR-4")

KEYS = ApiKeyPair(api_key="E2ETRADEKEY01234", api_secret="e2e-trade-secret-abcdefghijklmnopqr")  # noqa: S106
TRADING_KEY = KeyPermissions(
    read_only=False,
    permissions=frozenset({"ContractTrade.Order", "ContractTrade.Position", "Spot.SpotTrade"}),
    expires_at=None,
    ip_restricted=True,
)
MARKET_BUY = {
    "symbol": "BTCUSDT",
    "category": "linear",
    "side": "buy",
    "order_type": "market",
    "qty": "0.001",
}


class Setup:
    def __init__(self, *, confirm: frozenset[ActionKind] = frozenset()) -> None:
        self.db, self.clock, cipher = InMemoryDatabase(), FakeClock(), FakeCipher()
        self.user = self.db.add_user()
        self.db.add_credential(self.user, cipher, KEYS, permissions=TRADING_KEY)
        self.trading = FakeTrading(db=self.db)
        exchange = FakeExchange(
            tickers={("BTCUSDT", Category.LINEAR): make_ticker()},
            rules={("BTCUSDT", Category.LINEAR): make_rules()},
        )
        self.server = server_for(
            self.db, self.user.id, exchange=exchange, cipher=cipher, trading=self.trading, clock=self.clock
        )
        self.confirm = confirm

    async def grant(self) -> None:
        patch = PolicyPatch(
            profile=Profile.TRADE,
            allowlist=frozenset({InstrumentRef(Category.LINEAR, "BTCUSDT")}),
            confirm_actions=self.confirm,
        )
        await SetCeiling(self.db, self.clock)(self.user.name, patch)

    async def call(self, args: dict[str, Any]) -> tuple[Tool, CallToolResult]:
        async with Client(self.server) as client:
            tools = (await client.list_tools()).tools
            result = await client.call_tool("place_order", args)
        return next(t for t in tools if t.name == "place_order"), result


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None, result.content
    out: dict[str, Any] = result.structured_content
    return out


@pytest.mark.req("FR-10", "FR-11")
async def test_input_schema_declares_qty_and_price_as_strings_with_a_pattern() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "place_order")

    properties = tool.input_schema["properties"]
    assert properties["qty"]["type"] == "string"
    assert properties["qty"]["pattern"] == AMOUNT_PATTERN == r"^[0-9]{1,20}(\.[0-9]{1,18})?$"
    price_types = {branch.get("type") for branch in properties["price"]["anyOf"]}
    assert price_types == {"string", "null"}
    assert all("pattern" in b for b in properties["price"]["anyOf"] if b.get("type") == "string")
    assert "number" not in json.dumps({"qty": properties["qty"], "price": properties["price"]})
    assert set(tool.input_schema["required"]) == {"symbol", "category", "side", "order_type", "qty"}
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.idempotent_hint is False


@pytest.mark.req("FR-28", "FR-10")
@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({**MARKET_BUY, "qty": 0.001}, "qty"),
        ({**MARKET_BUY, "order_type": "limit", "price": 80000.5}, "price"),
        ({**MARKET_BUY, "qty": "1e-3"}, "qty"),
    ],
)
async def test_a_json_number_instead_of_a_string_is_invalid_params(args: dict[str, Any], field: str) -> None:
    setup = Setup()
    await setup.grant()

    _, result = await setup.call(args)

    assert result.is_error is True
    error = _structured(result)
    assert error["code"] == "invalid_params"
    assert error["details"]["field"] == field
    assert setup.trading.calls == []  # nothing reached the exchange


@pytest.mark.req("FR-36", "FR-48")
async def test_a_market_order_end_to_end_reports_what_was_sent_and_filled_unknown() -> None:
    setup = Setup()
    await setup.grant()

    tool, result = await setup.call(MARKET_BUY)

    assert result.is_error is False, result.structured_content
    out = _structured(result)
    assert tool.output_schema is not None
    jsonschema.validate(out, tool.output_schema)
    assert json.loads(result.content[0].text) == out  # type: ignore[union-attr]
    assert out["status"] == "accepted"
    assert (out["time_in_force"], out["price"], out["price_source"]) == ("ioc", "81747.7", "request")
    assert (out["best_price"], out["worst_price"]) == ("80571.2", "82182.6")
    assert (out["filled_qty"], out["order_status"]) == ("unknown", "unknown")
    assert "do not place it again" in out["next_step"]
    assert out["client_order_id"].startswith("gx-")
    attempt = next(r for r in setup.db.all_audit() if r.event_type == "order_attempt")
    assert attempt.payload["client"] is not None  # the client's self-description, unverified (FR-36)


async def test_a_visible_fill_is_reported_with_the_exchange_price() -> None:
    setup = Setup()
    await setup.grant()
    setup.trading.fills["gx-act-0001"] = OrderFill(
        order_id="order-1",
        status=FillStatus.FILLED,
        qty=Decimal("0.001"),
        price=Decimal("81700.0"),  # not the price sent (81747.7)
        filled_qty=Decimal("0.001"),
        avg_fill_price=Decimal("80571.2"),
    )

    _, result = await setup.call(MARKET_BUY)

    out = _structured(result)
    assert (out["order_status"], out["filled_qty"], out["avg_fill_price"]) == ("filled", "0.001", "80571.2")
    assert (out["price"], out["price_source"]) == ("81700", "exchange")
    assert (out["notional_usdt"], out["notional_price"]) == ("81.7477", "81747.7")
    assert "next_step" not in out
    journal = {r.event_type: r.payload for r in setup.db.all_audit()}
    assert journal["order_attempt"]["order"]["price"] == "81747.7"  # what was sent
    assert journal["order_outcome"]["fill"]["price"] == "81700"  # what the exchange shows


@pytest.mark.req("FR-25", "FR-19")
async def test_a_policy_requiring_confirmation_gets_confirmation_channel_unavailable() -> None:
    setup = Setup(confirm=frozenset({ActionKind.PLACE_ORDER}))
    await setup.grant()

    _, result = await setup.call(MARKET_BUY)

    assert result.is_error is True
    error = _structured(result)
    assert error["code"] == "confirmation_channel_unavailable"
    assert "Do not retry" in error["next_step"]
    assert [c for c in setup.trading.calls if c[0] == "place_order"] == []


@pytest.mark.req("FR-17", "FR-28")
async def test_a_limit_violation_names_the_limit_and_the_allowed_value() -> None:
    setup = Setup()
    await setup.grant()

    _, result = await setup.call({**MARKET_BUY, "qty": "0.5"})  # ~40 875 USDT

    error = _structured(result)
    assert error["code"] == "limit_exceeded"
    assert error["details"]["limit"] == "max_order_notional_usdt"
    assert error["details"]["allowed"] == "1000"


# --- the composition root: real adapter and cipher, fake storage --------------------------------

_WIRED_ROUTES = {
    "/v5/market/instruments-info": "bybit/instruments_linear_btcusdt.json",
    "/v5/market/price-limit": "bybit/spike004/demo_price_limit_linear_btcusdt.json",
    "/v5/position/list": "bybit/spike004/demo_position_list_before.json",
}


@pytest.mark.req("FR-36", "NFR-2")
async def test_the_wired_server_places_a_limit_order_through_the_bybit_adapter() -> None:
    """``app_server`` wiring: AES-GCM key, ``RequireTradingCredential``, ``BybitClient`` on fixtures."""
    db, clock = InMemoryDatabase(), FakeClock()
    user = db.add_user()
    cipher = AesGcmCredentialCipher(Keyring.single(os.urandom(32)))
    db.add_credential(user, cipher, KEYS, permissions=TRADING_KEY)
    db.state.credentials = {  # checked just now (system clock): no re-check against the exchange
        cid: (replace(cred, permissions_checked_at=datetime.now(UTC)), sealed)
        for cid, (cred, sealed) in db.state.credentials.items()
    }
    grant = PolicyPatch(
        profile=Profile.TRADE,
        allowlist=frozenset({InstrumentRef(Category.LINEAR, "BTCUSDT")}),
        confirm_actions=frozenset(),
    )
    await SetCeiling(db, clock)(user.name, grant)
    sent: list[dict[str, Any]] = []

    def bybit(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/v5/order/create":
            sent.append(json.loads(request.content))
            body = load_fixture("bybit/spike004/linear_create_limit_gtc_ok.json")
            body["result"]["orderLinkId"] = sent[-1]["orderLinkId"]
            return httpx2.Response(200, json=body)
        if path == "/v5/order/realtime":
            body = load_fixture("bybit/account/order_realtime_linear_open.json")
            body["result"]["list"] = body["result"]["list"][:1]
            body["result"]["list"][0].update(orderLinkId=sent[-1]["orderLinkId"], price="68797.6")
            return httpx2.Response(200, json=body)
        return httpx2.Response(200, json=load_fixture(_WIRED_ROUTES[path]))

    settings = Settings(_env_file=None, bybit_get_max_attempts=1)
    transport = httpx2.MockTransport(bybit)
    limit_buy = {**MARKET_BUY, "order_type": "limit", "price": "68797.6"}
    async with (
        app_server(
            settings, uow=db, cipher=cipher, principal=fixed_principal(user.id), transport=transport
        ) as server,
        Client(server) as client,
    ):
        result = await client.call_tool("place_order", limit_buy)

    out = _structured(result)
    assert result.is_error is False, out
    assert (out["time_in_force"], out["price"], out["price_source"]) == ("gtc", "68797.6", "exchange")
    assert out["order_status"] == "open"
    [body] = sent
    assert (body["orderType"], body["timeInForce"], body["price"]) == ("Limit", "GTC", "68797.6")
    assert re.fullmatch(r"gx-[0-9a-f]{32}", body["orderLinkId"])  # D-19: uuid4 hex, 35 characters


# --- idempotency through MCP (ticket 012: FR-26, FR-28) -------------------------------------------


@pytest.mark.req("FR-26")
async def test_the_tool_offers_an_idempotency_key_and_says_a_new_attempt_needs_a_new_key() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "place_order")

    key = tool.input_schema["properties"]["idempotency_key"]
    branch = next(b for b in key["anyOf"] if b.get("type") == "string")
    assert branch["pattern"] == IDEMPOTENCY_KEY_PATTERN
    assert "idempotency_key" not in tool.input_schema["required"]
    assert "needs a new key" in (tool.description or "")
    assert tool.output_schema is not None
    assert "result_source" in tool.output_schema["$defs"]["PlaceOrderOut"]["properties"]
    assert tool.output_schema["type"] == "object"  # FR-47 (b): or a pending confirmation


@pytest.mark.req("FR-26", "FR-28")
async def test_a_repeated_call_is_answered_as_a_replay_and_other_arguments_conflict() -> None:
    setup = Setup()
    await setup.grant()
    keyed = {**MARKET_BUY, "idempotency_key": "e2e-key-1"}

    async with Client(setup.server) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "place_order")
        first = _structured(await client.call_tool("place_order", keyed))
        again = _structured(await client.call_tool("place_order", keyed))
        conflict = await client.call_tool("place_order", {**keyed, "qty": "0.002"})

    assert tool.output_schema is not None
    jsonschema.validate(again, tool.output_schema)
    assert (first["result_source"], again["result_source"]) == ("placed", "replay")
    assert again["order_id"] == first["order_id"]
    assert "new idempotency_key" in again["next_step"]
    assert len(setup.trading.sent) == 1
    assert conflict.is_error is True
    assert _structured(conflict)["code"] == "idempotency_conflict"
