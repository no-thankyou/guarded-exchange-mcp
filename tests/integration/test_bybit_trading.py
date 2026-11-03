"""Placing orders through the Bybit adapter on ``httpx2.MockTransport`` and, end to end, with real
SQLite and AES-GCM (ticket 011: FR-4, FR-17, FR-27, FR-36, ADR-008, ADR-010, D-10, D-15, D-19).

Answers are spike 004 demo recordings; where a recording carries the spike's own ``orderLinkId``,
the stub echoes the id of the request (Bybit returns the id it was sent).
"""

import copy
import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from guarded_exchange.application.credentials import RecheckCredential, RequireTradingCredential
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.submit_action import PlaceOrderInput, SubmitPlaceOrder
from guarded_exchange.domain.account import OrderSide
from guarded_exchange.domain.audit import ClientDescription
from guarded_exchange.domain.credentials import (
    EXCHANGE_BYBIT,
    ApiKeyPair,
    Credential,
    CredentialStatus,
    KeyPermissions,
)
from guarded_exchange.domain.errors import (
    ExchangeRejected,
    ExchangeUnavailable,
    InvalidParams,
    LimitExceeded,
    NotFound,
    OutcomeUnknown,
    RateLimited,
    UnsupportedPositionMode,
)
from guarded_exchange.domain.instruments import Category, InstrumentStatus
from guarded_exchange.domain.orders import FillStatus, OrderRequest, OrderTimeInForce, PriceBand
from guarded_exchange.domain.policy import (
    NO_OVERRIDES,
    AdminCeiling,
    InstrumentRef,
    PolicyRecord,
    Profile,
    default_ceiling,
)
from guarded_exchange.domain.users import Environment, new_user
from guarded_exchange.infrastructure.bybit.client import BybitClient, RetryPolicy
from guarded_exchange.infrastructure.bybit.http import make_http_client
from guarded_exchange.infrastructure.clock import SystemClock
from guarded_exchange.infrastructure.crypto import AesGcmCredentialCipher, Keyring
from guarded_exchange.infrastructure.db.uow import SqlUnitOfWorkFactory
from tests.conftest import load_fixture
from tests.fakes import FakeKeyReader, SequentialIds

pytestmark = pytest.mark.req("FR-4")

KEYS = ApiKeyPair(api_key="TRADEKEY01234567", api_secret="trade-secret-abcdefghijklmnopqrstuv")  # noqa: S106
NOW_MS = 1793168786000
CLIENT_ID = "gx-0123456789abcdef0123456789abcdef"
EMPTY_ORDERS = {
    "retCode": 0,
    "retMsg": "OK",
    "result": {"list": [], "nextPageCursor": "", "category": "linear"},
}
FIXTURES = {
    "/v5/market/price-limit": "bybit/spike004/demo_price_limit_linear_btcusdt.json",
    "/v5/position/list": "bybit/spike004/demo_position_list_before.json",
    "/v5/market/tickers": "bybit/tickers_linear_btcusdt.json",
    "/v5/market/instruments-info": "bybit/instruments_linear_btcusdt.json",
    "/v5/order/create": "bybit/spike004/linear_create_limit_ioc_crossing.json",
}


@dataclass
class Stub:
    """Bybit on fixtures. ``override[path]`` replaces an answer (a body, an exception or a callable)."""

    requests: list[httpx2.Request] = field(default_factory=list)
    override: dict[str, Any] = field(default_factory=dict)
    on_create: Callable[[httpx2.Request], None] | None = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v5/order/create" and self.on_create is not None:
            self.on_create(request)
        answer = self.override.get(path)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx2.Response):
            return answer
        body = copy.deepcopy(answer if answer is not None else load_fixture(FIXTURES[path]))
        link_id = _link_id(request)
        if link_id and path == "/v5/order/create" and "orderLinkId" in body["result"]:
            body["result"]["orderLinkId"] = link_id
        if link_id and path in {"/v5/order/realtime", "/v5/order/history"}:
            for item in body["result"]["list"]:
                item["orderLinkId"] = link_id
        return httpx2.Response(200, json=body)

    def posts(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.method == "POST"]


def _link_id(request: httpx2.Request) -> str:
    if request.method == "POST":
        return str(json.loads(request.content).get("orderLinkId", ""))
    return str(request.url.params.get("orderLinkId", ""))


def client(stub: Stub) -> BybitClient:
    http = make_http_client(
        "https://api-demo.bybit.com",
        connect_timeout_s=1,
        read_timeout_s=1,
        transport=httpx2.MockTransport(stub),
    )
    return BybitClient(http, retry=RetryPolicy(max_attempts=2, base_delay_s=0), now_ms=lambda: NOW_MS)


def ioc_buy(**changes: Any) -> OrderRequest:
    values: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "category": Category.LINEAR,
        "side": OrderSide.BUY,
        "qty": Decimal("0.001"),
        "price": Decimal("81376.90"),
        "time_in_force": OrderTimeInForce.IOC,
        "reduce_only": False,
        "client_order_id": CLIENT_ID,
        **changes,
    }
    return OrderRequest(**values)


def _ret(code: int, message: str) -> dict[str, Any]:
    return {"retCode": code, "retMsg": message, "result": {}, "time": NOW_MS}


# --- the adapter ---------------------------------------------------------------------------------


@pytest.mark.req("FR-48", "NFR-2")
async def test_place_order_signs_and_sends_exactly_one_limit_ioc_body() -> None:
    stub = Stub()

    placed = await client(stub).place_order(KEYS, ioc_buy())

    [request] = stub.requests
    assert (request.method, request.url.path) == ("POST", "/v5/order/create")
    body = request.content.decode()
    assert json.loads(body) == {
        "category": "linear",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Limit",
        "qty": "0.001",
        "price": "81376.9",
        "timeInForce": "IOC",
        "orderLinkId": CLIENT_ID,
        "reduceOnly": False,
        "positionIdx": 0,
    }
    expected = hmac.new(
        KEYS.api_secret.encode(), f"{NOW_MS}{KEYS.api_key}5000{body}".encode(), hashlib.sha256
    ).hexdigest()
    assert request.headers["X-BAPI-SIGN"] == expected  # signed over the exact bytes sent
    assert request.headers["Content-Type"] == "application/json"
    assert placed.order_id == "fa7b555a-dad9-444c-8f67-e4fc7572ac65"


async def test_a_spot_order_carries_no_perpetual_fields() -> None:
    stub = Stub()
    await client(stub).place_order(KEYS, ioc_buy(category=Category.SPOT, time_in_force=OrderTimeInForce.GTC))

    body = json.loads(stub.requests[0].content)
    assert "reduceOnly" not in body
    assert "positionIdx" not in body
    assert body["timeInForce"] == "GTC"


def test_client_order_id_is_the_prefix_plus_the_action_id_within_36_characters() -> None:
    bybit = client(Stub())
    action_id = "0123456789abcdef0123456789abcdef"  # uuid4().hex, as UuidGenerator makes it
    assert bybit.client_order_id(action_id) == "gx-" + action_id
    assert len(bybit.client_order_id(action_id)) == 35
    with pytest.raises(ValueError, match="orderLinkId"):
        bybit.client_order_id("x" * 34)


@pytest.mark.req("FR-27")
@pytest.mark.parametrize(
    ("answer", "error", "reason"),
    [
        (httpx2.ReadTimeout("slow"), OutcomeUnknown, "no_answer"),
        (httpx2.RemoteProtocolError("cut"), OutcomeUnknown, "no_answer"),
        (httpx2.Response(502), OutcomeUnknown, "no_answer"),
        (httpx2.Response(200, text="<html>"), OutcomeUnknown, "no_answer"),
        (_ret(10000, "Server Timeout"), OutcomeUnknown, "exchange_internal_error"),
        (_ret(10016, "server error"), OutcomeUnknown, "exchange_internal_error"),
        (_ret(3400214, "Server error, please try again later"), OutcomeUnknown, "exchange_internal_error"),
        (_ret(170001, "Internal error."), OutcomeUnknown, "exchange_internal_error"),
        (_ret(170007, "Timeout waiting for response"), OutcomeUnknown, "exchange_internal_error"),
        (_ret(170032, "Network error"), OutcomeUnknown, "exchange_internal_error"),
        (_ret(170146, "Order creation timeout"), OutcomeUnknown, "exchange_internal_error"),
        (_ret(170234, "System Error"), OutcomeUnknown, "exchange_internal_error"),
        (_ret(110072, "OrderLinkedID is duplicate"), OutcomeUnknown, "duplicate_client_order_id"),
        (httpx2.ConnectError("refused"), ExchangeUnavailable, None),
    ],
)
async def test_a_possibly_sent_order_is_unknown_and_never_retried(
    answer: object, error: type[Exception], reason: str | None
) -> None:
    stub = Stub(override={"/v5/order/create": answer})

    with pytest.raises(error) as raised:
        await client(stub).place_order(KEYS, ioc_buy())

    assert len(stub.requests) == 1  # a POST is never retried (ADR-002)
    if reason is not None:
        assert raised.value.details["reason"] == reason  # type: ignore[attr-defined]


@pytest.mark.req("FR-28")
@pytest.mark.parametrize(
    "fixture", ["spot_create_over_buy_limit_default", "linear_create_over_buy_limit_reject_mode"]
)
async def test_a_price_outside_the_band_is_a_definite_rejection(fixture: str) -> None:
    stub = Stub(override={"/v5/order/create": load_fixture(f"bybit/spike004/{fixture}.json")})

    with pytest.raises(ExchangeRejected) as error:
        await client(stub).place_order(KEYS, ioc_buy())

    assert error.value.details["reason"] == "price_outside_band"
    assert error.value.untrusted_text  # the exchange's text only as data (FR-41)


async def test_rate_limit_and_parameter_errors_are_definite() -> None:
    limited = Stub(override={"/v5/order/create": _ret(10006, "Too many visits")})
    with pytest.raises(RateLimited):
        await client(limited).place_order(KEYS, ioc_buy())
    bad = Stub(override={"/v5/order/create": _ret(10001, "Qty invalid")})
    with pytest.raises(InvalidParams) as error:
        await client(bad).place_order(KEYS, ioc_buy())
    assert error.value.untrusted_text == "Qty invalid"


@pytest.mark.req("FR-48")
@pytest.mark.parametrize(
    ("fixture", "status", "filled", "avg"),
    [
        ("linear_order_ioc_first_read", FillStatus.FILLED, "0.001", "80938.4"),
        ("linear_order_ioc_not_crossing_final", FillStatus.CANCELLED, "0", None),
        ("spot_order_ioc_not_crossing_final", FillStatus.CANCELLED, "0", None),
    ],
)
async def test_the_order_is_read_back_by_client_id(
    fixture: str, status: FillStatus, filled: str, avg: str | None
) -> None:
    body = load_fixture(f"bybit/spike004/{fixture}.json")
    category = Category.SPOT if fixture.startswith("spot") else Category.LINEAR
    stub = Stub(override={"/v5/order/realtime": body})

    fill = await client(stub).get_order(KEYS, category, "BTCUSDT", CLIENT_ID)

    assert fill is not None
    assert (fill.status, fill.filled_qty) == (status, Decimal(filled))
    assert fill.avg_fill_price == (None if avg is None else Decimal(avg))
    assert fill.price == Decimal(body["result"]["list"][0]["price"])  # the order's own price
    assert stub.requests[0].url.params["orderLinkId"] == CLIENT_ID


@pytest.mark.req("FR-48")
async def test_an_order_not_listed_yet_reads_as_none() -> None:
    stub = Stub(override={"/v5/order/realtime": EMPTY_ORDERS})
    assert await client(stub).get_order(KEYS, Category.LINEAR, "BTCUSDT", CLIENT_ID) is None


@pytest.mark.req("FR-48")
@pytest.mark.parametrize(
    ("status", "filled"), [("New", None), ("SomethingNew", None), ("Filled", None), ("Rejected", "0")]
)
async def test_an_empty_executed_quantity_is_unknown_not_zero(status: str, filled: str | None) -> None:
    """D-15: an empty ``cumExecQty`` says nothing about the execution, except for a rejected order."""
    body = load_fixture("bybit/spike004/linear_order_ioc_first_read.json")
    body["result"]["list"][0].update(orderStatus=status, cumExecQty="")
    stub = Stub(override={"/v5/order/realtime": body})

    fill = await client(stub).get_order(KEYS, Category.LINEAR, "BTCUSDT", CLIENT_ID)

    assert fill is not None
    assert fill.filled_qty == (None if filled is None else Decimal(filled))


@pytest.mark.req("FR-43")
async def test_a_reduce_only_order_carries_the_flag_on_the_wire() -> None:
    stub = Stub(override={"/v5/order/create": load_fixture("bybit/spike004/linear_close_ioc_reduce.json")})

    await client(stub).place_order(KEYS, ioc_buy(side=OrderSide.SELL, reduce_only=True))

    body = json.loads(stub.requests[0].content)
    assert body["reduceOnly"] is True
    assert body["side"] == "Sell"


async def test_price_band_and_symbol_position_map_from_demo_records() -> None:
    stub = Stub()
    bybit = client(stub)

    band = await bybit.get_price_band("BTCUSDT", Category.LINEAR)
    position = await bybit.get_symbol_position(KEYS, "BTCUSDT")

    assert band == PriceBand(buy_max=Decimal("81747.70"), sell_min=Decimal("80128.90"))
    assert (position.position, position.leverage) == (None, Decimal(10))  # flat, leverage still known
    assert stub.requests[1].url.params["symbol"] == "BTCUSDT"
    hedge = Stub(override={"/v5/position/list": load_fixture("bybit/account/position_list_hedge_mode.json")})
    with pytest.raises(UnsupportedPositionMode):
        await client(hedge).get_symbol_position(KEYS, "BTCUSDT")


async def test_an_open_position_is_mapped_with_its_leverage() -> None:
    body = load_fixture("bybit/account/position_list_linear_open.json")
    body["result"]["list"] = [row for row in body["result"]["list"] if row["symbol"] == "BTCUSDT"][:1]
    stub = Stub(override={"/v5/position/list": body})

    state = await client(stub).get_symbol_position(KEYS, "BTCUSDT")

    assert state.position is not None
    assert state.position.size > 0
    assert state.leverage is not None


@pytest.mark.req("FR-43", "FR-15")
async def test_rules_of_a_delisting_perpetual_are_found_with_an_explicit_status() -> None:
    delivering = load_fixture("bybit/instruments_linear_btcusdt.json")
    delivering["result"]["list"][0]["status"] = "Delivering"

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.params.get("status") == "Delivering":
            return httpx2.Response(200, json=delivering)
        return httpx2.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": []}})

    http = make_http_client(
        "https://api-demo.bybit.com",
        connect_timeout_s=1,
        read_timeout_s=1,
        transport=httpx2.MockTransport(answer),
    )
    bybit = BybitClient(http, retry=RetryPolicy(max_attempts=1), now_ms=lambda: NOW_MS)

    with pytest.raises(NotFound):
        await bybit.get_instrument_rules("BTCUSDT", Category.LINEAR)
    rules = await bybit.get_instrument_rules("BTCUSDT", Category.LINEAR, any_status=True)
    assert rules.status is InstrumentStatus.SETTLING


# --- end to end: use case + adapter + real SQLite ------------------------------------------------

TRADING_KEY = KeyPermissions(
    read_only=False,
    permissions=frozenset({"ContractTrade.Order", "ContractTrade.Position", "Spot.SpotTrade"}),
    expires_at=None,
    ip_restricted=True,
)


@dataclass
class Live:
    place: SubmitPlaceOrder
    stub: Stub
    user_id: str
    db_path: Path

    def journal(self) -> list[tuple[str, dict[str, Any]]]:
        """Committed order records, read through a separate connection (what another reader sees)."""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT event_type, payload FROM audit_log WHERE chain_id = ? AND event_type LIKE 'order_%' "
                "ORDER BY seq",
                (self.user_id,),
            ).fetchall()
        finally:
            conn.close()
        return [(event, json.loads(raw)) for event, raw in rows]


async def live(uow: SqlUnitOfWorkFactory, db_path: Path, ceiling: AdminCeiling) -> Live:
    clock, cipher = SystemClock(), AesGcmCredentialCipher(Keyring.single(os.urandom(32)))
    user = new_user("trader-1", "local", clock.now())
    credential = Credential(
        id="cred-1",
        user_id=user.id,
        exchange=EXCHANGE_BYBIT,
        environment=Environment.TESTNET,
        status=CredentialStatus.ACTIVE,
        permissions=TRADING_KEY,
        permissions_checked_at=clock.now(),
        admin_attested=False,
        created_at=clock.now(),
    )
    async with uow(write=True) as tx:
        await tx.users.add(user)
        await tx.policies.save(
            user.id, PolicyRecord(ceiling=ceiling, overrides=NO_OVERRIDES, updated_at=clock.now())
        )
        await tx.credentials.add(credential, cipher.seal(KEYS, credential.binding))
    stub = Stub(override={"/v5/order/realtime": EMPTY_ORDERS, "/v5/order/history": EMPTY_ORDERS})
    bybit = client(stub)
    scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
    place = SubmitPlaceOrder(
        uow,
        RequireTradingCredential(
            uow, cipher, RecheckCredential(uow, cipher, FakeKeyReader(), clock), clock, **scope
        ),
        InstrumentRulesCache(bybit, clock),
        market=bybit,
        trading=bybit,
        clock=clock,
        ids=SequentialIds("0123456789abcdef0123456789ab"),
        reconnaissance_delay_s=0,
        **scope,
    )
    return Live(place, stub, user.id, db_path)


def trading_ceiling(**changes: Any) -> AdminCeiling:
    base = default_ceiling(Environment.TESTNET)
    values: dict[str, Any] = {
        "profile": Profile.TRADE,
        "allowlist": frozenset({InstrumentRef(Category.LINEAR, "BTCUSDT")}),
        "confirm_actions": frozenset(),
        "slippage_pct": Decimal(1),
        **changes,
    }
    return replace(base, **values)


MARKET_BUY = PlaceOrderInput(
    symbol="BTCUSDT", category="linear", side="buy", order_type="market", qty="0.001"
)


@pytest.mark.req("FR-36", "FR-48", "FR-27")
async def test_market_order_goes_out_as_limit_ioc_at_the_bound_and_filled_is_unknown(
    uow: SqlUnitOfWorkFactory, db_path: Path
) -> None:
    world = await live(uow, db_path, trading_ceiling())
    seen_at_send: list[list[str]] = []
    world.stub.on_create = lambda _: seen_at_send.append([event for event, _ in world.journal()])

    result = await world.place(world.user_id, MARKET_BUY, ClientDescription.sanitized("pytest", "1"))

    [create] = world.stub.posts()
    body = json.loads(create.content)
    # ask 80571.2 x (1 + 1 %) = 81376.912 -> 81376.9 on the tick: the worst price is the IOC bound
    assert (body["orderType"], body["timeInForce"], body["price"]) == ("Limit", "IOC", "81376.9")
    assert body["orderLinkId"] == result.client_order_id == "gx-0123456789abcdef0123456789ab-0001"
    assert result.fill is None  # realtime does not list it yet (D-15)
    # ADR-008: the attempt was committed (visible to another connection) before the POST; outcome after
    assert seen_at_send == [["order_attempt"]]
    events = world.journal()
    assert [event for event, _ in events] == ["order_attempt", "order_outcome"]
    attempt, outcome = events[0][1], events[1][1]
    assert (attempt["order"]["price"], attempt["order"]["worst_price"]) == ("81376.9", "81376.9")
    assert attempt["client"] == {"name": "pytest", "version": "1"}
    assert outcome == {
        "action_id": "0123456789abcdef0123456789ab-0001",
        "client_order_id": "gx-0123456789abcdef0123456789ab-0001",
        "status": "accepted",
        "order_id": "fa7b555a-dad9-444c-8f67-e4fc7572ac65",
        "fill": "unknown",
    }


@pytest.mark.req("FR-17", "FR-13")
async def test_a_refusal_is_committed_in_the_deciding_transaction_and_nothing_is_posted(
    uow: SqlUnitOfWorkFactory, db_path: Path
) -> None:
    world = await live(uow, db_path, trading_ceiling(max_order_notional_usdt=Decimal(10)))

    with pytest.raises(LimitExceeded):
        await world.place(world.user_id, MARKET_BUY)

    assert world.stub.posts() == []
    [(event, payload)] = world.journal()
    assert event == "order_refused"
    assert payload["code"] == "limit_exceeded"
    assert payload["details"]["limit"] == "max_order_notional_usdt"
    assert payload["details"]["allowed"] == "10"


@pytest.mark.req("FR-27")
async def test_a_timeout_after_sending_is_recorded_as_unknown(
    uow: SqlUnitOfWorkFactory, db_path: Path
) -> None:
    world = await live(uow, db_path, trading_ceiling())
    world.stub.override["/v5/order/create"] = httpx2.ReadTimeout("slow")

    with pytest.raises(OutcomeUnknown) as error:
        await world.place(world.user_id, MARKET_BUY)

    assert error.value.details["client_order_id"].startswith("gx-")
    assert [event for event, _ in world.journal()] == ["order_attempt", "order_outcome"]
    assert world.journal()[1][1]["status"] == "unknown"
    assert len(world.stub.posts()) == 1
