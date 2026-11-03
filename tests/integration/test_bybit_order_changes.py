"""Cancel, amend and cancel-all through the Bybit adapter on ``httpx2.MockTransport`` (ticket 013:
FR-5, FR-27, FR-28, FR-43; PLAN ADR-001, ADR-002, D-21, spike 004), and end to end with the use cases."""

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from guarded_exchange.application.credentials import RecheckCredential, RequireTradingCredential
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.order_change_results import ChangeState, ItemState
from guarded_exchange.application.order_changes import (
    AmendOrderInput,
    CancelAllOrdersInput,
    CancelOrderInput,
    SubmitAmendOrder,
    SubmitCancelAllOrders,
    SubmitCancelOrder,
)
from guarded_exchange.application.order_results import ResultSource
from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.domain.account import OrderOrigin, OrderStatus
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT
from guarded_exchange.domain.errors import (
    ExchangeRejected,
    InvalidParams,
    NotFound,
    OutcomeUnknown,
    RateLimited,
    UnsupportedPositionMode,
)
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.order_changes import OrderAmendment
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from guarded_exchange.domain.users import Environment
from guarded_exchange.infrastructure.bybit.client import BybitClient, RetryPolicy
from guarded_exchange.infrastructure.bybit.http import make_http_client
from tests.conftest import load_fixture
from tests.fakes import FakeCipher, FakeClock, FakeKeyReader, InMemoryDatabase, SequentialIds
from tests.integration.test_bybit_trading import EMPTY_ORDERS, KEYS, NOW_MS, TRADING_KEY

pytestmark = pytest.mark.req("FR-5")

WITH_SL = "bybit/order_changes/order_realtime_linear_btcusdt_with_position_sl.json"
OURS = "5c1e0b9a-7d2f-4e3a-9b8c-1d2e3f4a5b6c"
EXTERNAL = "7d6c5b4a-3f2e-4d1c-9b0a-8f7e6d5c4b3a"
POSITION_SL = "0e1d2c3b-4a59-4687-b6a5-c4d3e2f1a0b9"
AMENDED = "3c2d0aac-d095-46e7-8bb4-b599dd055245"  # the order of spike004/linear_amend_ok.json


def _ret(code: int, message: str) -> dict[str, Any]:
    return {"retCode": code, "retMsg": message, "result": {}, "time": NOW_MS}


def _ok(order_id: str) -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK", "result": {"orderId": order_id, "orderLinkId": ""}, "time": NOW_MS}


def _orders(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"list": list(rows), "nextPageCursor": "", "category": "linear"},
    }


def row(order_id: str, **changes: str) -> dict[str, Any]:
    """One row of the fixture, as the exchange would list it (``changes`` on top)."""
    found = next(r for r in load_fixture(WITH_SL)["result"]["list"] if r["orderId"] == order_id)
    return {**copy.deepcopy(found), **changes}


Answer = dict[str, Any] | Exception | Callable[[httpx2.Request], dict[str, Any]]


@dataclass
class Stub:
    """Bybit by path; an answer may be a body, an exception or a function of the request."""

    answers: dict[str, Answer] = field(default_factory=dict)
    requests: list[httpx2.Request] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.answers.get(request.url.path)
        if answer is None:
            raise AssertionError(f"unexpected request {request.method} {request.url.path}")
        if isinstance(answer, Exception):
            raise answer
        body = answer(request) if callable(answer) else answer
        return httpx2.Response(200, json=copy.deepcopy(body))

    def posts(self) -> list[tuple[str, dict[str, Any]]]:
        return [(r.url.path, json.loads(r.content)) for r in self.requests if r.method == "POST"]


def client(stub: Stub) -> BybitClient:
    http = make_http_client(
        "https://api-demo.bybit.com",
        connect_timeout_s=1,
        read_timeout_s=1,
        transport=httpx2.MockTransport(stub),
    )
    return BybitClient(http, retry=RetryPolicy(max_attempts=2, base_delay_s=0), now_ms=lambda: NOW_MS)


# --- the adapter ----------------------------------------------------------------------------------------


@pytest.mark.req("FR-43")
async def test_the_cancel_all_snapshot_asks_for_plain_orders_with_an_explicit_order_filter() -> None:
    """ADR-001 / spike 004: on linear, Bybit lists (and cancel-all cancels) every kind of order, a
    position's TP/SL included, unless ``orderFilter`` says otherwise."""
    stub = Stub({"/v5/order/realtime": load_fixture(WITH_SL)})

    page = await client(stub).list_cancellable_orders(KEYS, Category.LINEAR, "BTCUSDT", 20)

    params = stub.requests[0].url.params
    assert (params["orderFilter"], params["symbol"], params["category"]) == ("Order", "BTCUSDT", "linear")
    assert [o.origin for o in page.items[:2]] == [OrderOrigin.SYSTEM, OrderOrigin.EXTERNAL]


@pytest.mark.req("FR-27", "NFR-2")
@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (Category.LINEAR, {"category": "linear", "symbol": "BTCUSDT", "orderId": OURS}),
        (Category.SPOT, {"category": "spot", "symbol": "BTCUSDT", "orderId": OURS, "orderFilter": "Order"}),
    ],
)
async def test_a_cancel_is_one_post_by_order_id(category: Category, expected: dict[str, str]) -> None:
    stub = Stub({"/v5/order/cancel": _ok(OURS)})

    await client(stub).cancel_order(KEYS, category, "BTCUSDT", OURS)

    assert stub.posts() == [("/v5/order/cancel", expected)]
    assert "X-BAPI-SIGN" in stub.requests[0].headers


async def test_an_amend_sends_only_what_changes_and_reads_the_spike_answer() -> None:
    stub = Stub({"/v5/order/amend": load_fixture("bybit/spike004/linear_amend_ok.json")})
    amendment = OrderAmendment("BTCUSDT", Category.LINEAR, AMENDED, qty=None, price=Decimal("68796.60"))

    await client(stub).amend_order(KEYS, amendment)

    assert stub.posts() == [
        (
            "/v5/order/amend",
            {"category": "linear", "symbol": "BTCUSDT", "orderId": AMENDED, "price": "68796.6"},
        )
    ]


async def test_an_answer_about_another_order_is_an_unknown_outcome() -> None:
    stub = Stub({"/v5/order/cancel": _ok("someone-else")})
    with pytest.raises(OutcomeUnknown) as error:
        await client(stub).cancel_order(KEYS, Category.LINEAR, "BTCUSDT", OURS)
    assert error.value.details["reason"] == "unexpected_change_response"


_UNKNOWN_CODES = [
    (170147, "Order cancellation timeout"),
    (170150, "Cancel order failed"),
    (170191, "Can not cancel order, please try again later"),
    (170212, "Cancel order request processing"),
    (170310, "Order modification timeout"),
    (170311, "Order modification failed"),
    (110079, "The order is processing and can not be operated, please try again later"),
    (10016, "Server error"),
    (4242424, "something new"),
]


@pytest.mark.req("FR-27")
@pytest.mark.parametrize(("code", "message"), _UNKNOWN_CODES)
@pytest.mark.parametrize("path", ["/v5/order/cancel", "/v5/order/amend"])
async def test_codes_after_which_the_change_may_have_happened_are_unknown_and_never_retried(
    code: int, message: str, path: str
) -> None:
    stub = Stub({path: _ret(code, message)})
    bybit = client(stub)

    async def send() -> None:
        if path.endswith("cancel"):
            await bybit.cancel_order(KEYS, Category.SPOT, "BTCUSDT", OURS)
        else:
            await bybit.amend_order(KEYS, OrderAmendment("BTCUSDT", Category.SPOT, OURS, None, Decimal(1)))

    with pytest.raises(OutcomeUnknown) as error:
        await send()

    assert len(stub.requests) == 1
    assert error.value.details["exchange_code"] == str(code)
    assert error.value.untrusted_text == message  # the exchange's words only as data (FR-41)


@pytest.mark.req("FR-28")
@pytest.mark.parametrize(
    ("code", "error"),
    [
        (110001, NotFound),
        (110008, NotFound),
        (110010, NotFound),
        (170142, NotFound),
        (170213, NotFound),
        (10001, InvalidParams),
        (110022, ExchangeRejected),
        (10006, RateLimited),
    ],
)
async def test_definite_answers_to_a_change(code: int, error: type[Exception]) -> None:
    stub = Stub({"/v5/order/cancel": _ret(code, "x")})
    with pytest.raises(error):
        await client(stub).cancel_order(KEYS, Category.LINEAR, "BTCUSDT", OURS)
    assert len(stub.requests) == 1


async def test_an_order_is_read_by_id_from_realtime_then_history() -> None:
    history = _orders(row(OURS, orderStatus="Cancelled"))
    stub = Stub({"/v5/order/realtime": EMPTY_ORDERS, "/v5/order/history": history})

    order = await client(stub).get_order_by_id(KEYS, Category.LINEAR, "BTCUSDT", OURS)

    assert order is not None
    assert order.status is OrderStatus.CANCELLED
    assert [r.url.path for r in stub.requests] == ["/v5/order/realtime", "/v5/order/history"]
    assert all(r.url.params["orderId"] == OURS for r in stub.requests)
    hedge = Stub({"/v5/order/realtime": _orders(row(OURS, positionIdx=1))})  # type: ignore[arg-type]
    with pytest.raises(UnsupportedPositionMode):
        await client(hedge).get_order_by_id(KEYS, Category.LINEAR, "BTCUSDT", OURS)


# --- end to end: the use cases on the adapter -----------------------------------------------------------


@dataclass
class Live:
    db: InMemoryDatabase
    user_id: str
    stub: Stub
    cancel: SubmitCancelOrder
    amend: SubmitAmendOrder
    cancel_all: SubmitCancelAllOrders


async def live(stub: Stub) -> Live:
    db, clock, cipher = InMemoryDatabase(), FakeClock(), FakeCipher()
    user = db.add_user(environment=Environment.TESTNET)
    db.add_credential(user, cipher, KEYS, permissions=TRADING_KEY)
    patch = PolicyPatch(
        profile=Profile.TRADE,
        allowlist=frozenset({InstrumentRef(Category.LINEAR, "BTCUSDT")}),
        confirm_actions=frozenset(),
    )
    await SetCeiling(db, clock)(user.name, patch)
    bybit = client(stub)
    scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
    credentials = RequireTradingCredential(
        db,
        cipher,
        RecheckCredential(db, cipher, FakeKeyReader(permissions=TRADING_KEY), clock),
        clock,
        **scope,
    )
    common: dict[str, Any] = {
        "changes": bybit,
        "clock": clock,
        "ids": SequentialIds("act"),
        "reconnaissance_delay_s": 0,
        **scope,
    }
    return Live(
        db,
        user.id,
        stub,
        SubmitCancelOrder(db, credentials, **common),
        SubmitAmendOrder(db, credentials, InstrumentRulesCache(bybit, clock), trading=bybit, **common),
        SubmitCancelAllOrders(db, credentials, **common),
    )


def _by_order_id(rows: dict[str, dict[str, Any]]) -> Callable[[httpx2.Request], dict[str, Any]]:
    """``order/realtime``: by ``orderId`` one row, else (the snapshot) every row still open."""

    def answer(request: httpx2.Request) -> dict[str, Any]:
        wanted = request.url.params.get("orderId")
        if wanted is not None:
            return _orders(*([rows[wanted]] if wanted in rows else []))
        return _orders(*(r for r in rows.values() if r["orderStatus"] in {"New", "PartiallyFilled"}))

    return answer


@pytest.mark.req("FR-43", "FR-21")
async def test_cancel_all_on_linear_cancels_the_listed_ids_and_never_the_position_stop_loss() -> None:
    """The fixture lists the position's stop-loss as if the filter were ignored: still only the plain
    orders are cancelled, one by one, and ``cancel-all`` (which would sweep TP/SL) is never called."""
    rows = {r["orderId"]: r for r in load_fixture(WITH_SL)["result"]["list"]}
    stub = Stub()

    def cancel(request: httpx2.Request) -> dict[str, Any]:
        order_id = json.loads(request.content)["orderId"]
        rows[order_id] = {**rows[order_id], "orderStatus": "Cancelled"}
        return _ok(order_id)

    world = await live(stub)
    world.stub.answers["/v5/order/cancel"] = cancel
    snapshot_then_open = iter([load_fixture(WITH_SL)])
    world.stub.answers["/v5/order/realtime"] = lambda request: next(
        snapshot_then_open, _orders(*(r for r in rows.values() if r["orderStatus"] == "New"))
    )

    result = await world.cancel_all(world.user_id, CancelAllOrdersInput("BTCUSDT", "linear"))

    assert world.stub.posts() == [
        ("/v5/order/cancel", {"category": "linear", "symbol": "BTCUSDT", "orderId": OURS}),
        ("/v5/order/cancel", {"category": "linear", "symbol": "BTCUSDT", "orderId": EXTERNAL}),
    ]
    assert all(r.url.path != "/v5/order/cancel-all" for r in world.stub.requests)
    assert [(i.order.order_id, i.order.external, i.state) for i in result.items] == [
        (OURS, False, ItemState.CLOSED),
        (EXTERNAL, True, ItemState.CLOSED),
    ]
    assert world.stub.requests[0].url.params["orderFilter"] == "Order"


@pytest.mark.req("FR-27")
async def test_a_cancel_that_timed_out_is_found_cancelled_by_its_order_id() -> None:
    rows = {OURS: row(OURS)}
    stub = Stub({"/v5/order/realtime": _by_order_id(rows)})

    def timeout(request: httpx2.Request) -> dict[str, Any]:
        rows[OURS] = row(OURS, orderStatus="Cancelled")  # the exchange did it; the answer got lost
        raise httpx2.ReadTimeout("slow")

    stub.answers["/v5/order/cancel"] = timeout
    world = await live(stub)

    result = await world.cancel(world.user_id, CancelOrderInput("BTCUSDT", "linear", OURS))

    assert (result.state, result.source) == (ChangeState.CANCELLED, ResultSource.RECONNAISSANCE)
    assert len(world.stub.posts()) == 1  # never resent
    reads = [r.url.params.get("orderId") for r in world.stub.requests if r.method == "GET"]
    assert reads == [OURS, OURS]  # before the decision, then the reconnaissance


@pytest.mark.req("FR-27")
@pytest.mark.parametrize(("applied", "state"), [(True, ChangeState.AMENDED), (False, None)])
async def test_an_amend_the_exchange_did_not_confirm_is_settled_by_the_orders_status(
    applied: bool, state: ChangeState | None
) -> None:
    rows = {OURS: row(OURS)}  # buy 0.001 at 68797.60
    stub = Stub(
        {
            "/v5/order/realtime": _by_order_id(rows),
            "/v5/market/instruments-info": load_fixture("bybit/instruments_linear_btcusdt.json"),
            "/v5/market/price-limit": load_fixture("bybit/spike004/demo_price_limit_linear_btcusdt.json"),
            "/v5/position/list": load_fixture("bybit/spike004/demo_position_list_before.json"),
        }
    )

    def processing(request: httpx2.Request) -> dict[str, Any]:
        if applied:
            rows[OURS] = row(OURS, price="80200.00")
        return _ret(170310, "Order modification timeout")

    stub.answers["/v5/order/amend"] = processing
    world = await live(stub)
    request = AmendOrderInput("BTCUSDT", "linear", OURS, price="80200")

    if state is None:
        with pytest.raises(OutcomeUnknown) as error:
            await world.amend(world.user_id, request)
        assert error.value.details["reconnaissance"] == "found"
    else:
        result = await world.amend(world.user_id, request)
        assert (result.state, result.source) == (state, ResultSource.RECONNAISSANCE)
    assert [path for path, _ in world.stub.posts()] == ["/v5/order/amend"]
