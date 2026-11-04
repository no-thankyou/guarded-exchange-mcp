"""Leverage and stop-loss / take-profit through the Bybit adapter on ``httpx2.MockTransport`` (ticket 014:
FR-6, FR-8, FR-27, FR-28; PLAN ADR-002, D-21, D-26), and end to end with the use cases."""

import copy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from guarded_exchange.application.credentials import RecheckCredential, RequireTradingCredential
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.order_results import ResultSource
from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.application.position_actions import (
    SetLeverageInput,
    SetTradingStopInput,
    SubmitSetLeverage,
    SubmitSetTradingStop,
)
from guarded_exchange.application.position_results import PositionChangeState
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT
from guarded_exchange.domain.errors import ExchangeRejected, InvalidParams, OutcomeUnknown, RateLimited
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from guarded_exchange.domain.users import Environment
from tests.conftest import load_fixture
from tests.fakes import FakeCipher, FakeClock, FakeKeyReader, InMemoryDatabase, SequentialIds
from tests.integration.test_bybit_order_changes import Stub, client
from tests.integration.test_bybit_trading import KEYS, NOW_MS, TRADING_KEY

pytestmark = pytest.mark.req("FR-8")

FLAT = "bybit/spike004/demo_position_list_before.json"
OPEN = "bybit/account/position_list_linear_open.json"


def _ret(code: int, message: str) -> dict[str, Any]:
    return {"retCode": code, "retMsg": message, "result": {}, "time": NOW_MS}


OK: dict[str, Any] = {"retCode": 0, "retMsg": "OK", "result": {}, "retExtInfo": {}, "time": NOW_MS}


def positions(**changes: str) -> dict[str, Any]:
    """The flat BTCUSDT row of the spike (leverage 10) with ``changes`` on top."""
    body = copy.deepcopy(load_fixture(FLAT))
    body["result"]["list"][0].update(changes)
    return body


def open_position(**changes: str) -> dict[str, Any]:
    body = copy.deepcopy(load_fixture(OPEN))
    body["result"]["list"] = [{**body["result"]["list"][0], **changes}]
    return body


# --- the adapter ----------------------------------------------------------------------------------------


async def test_set_leverage_is_one_post_with_the_same_buy_and_sell_leverage() -> None:
    stub = Stub({"/v5/position/set-leverage": OK})

    await client(stub).set_leverage(KEYS, "BTCUSDT", Decimal("5.50"))

    assert stub.posts() == [
        (
            "/v5/position/set-leverage",
            {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "5.5", "sellLeverage": "5.5"},
        )
    ]
    assert "X-BAPI-SIGN" in stub.requests[0].headers


@pytest.mark.req("FR-8")
async def test_leverage_already_at_that_value_110043_is_success() -> None:
    stub = Stub({"/v5/position/set-leverage": _ret(110043, "Set leverage has not been modified.")})

    await client(stub).set_leverage(KEYS, "BTCUSDT", Decimal(10))  # does not raise

    assert len(stub.requests) == 1


async def test_a_trading_stop_sends_only_the_levels_being_set_for_the_whole_position() -> None:
    stub = Stub({"/v5/position/trading-stop": OK})

    await client(stub).set_trading_stop(KEYS, "BTCUSDT", None, Decimal("79000.50"))

    assert stub.posts() == [
        (
            "/v5/position/trading-stop",
            {
                "category": "linear",
                "symbol": "BTCUSDT",
                "tpslMode": "Full",
                "positionIdx": 0,
                "stopLoss": "79000.5",
            },
        )
    ]


async def test_a_trading_stop_already_set_34040_is_success() -> None:
    stub = Stub({"/v5/position/trading-stop": _ret(34040, "Not modified")})

    await client(stub).set_trading_stop(KEYS, "BTCUSDT", Decimal(85000), None)


@pytest.mark.req("FR-27", "FR-28")
@pytest.mark.parametrize(
    ("code", "error"),
    [
        (10016, OutcomeUnknown),  # server error: may have been applied
        (4242424, OutcomeUnknown),  # a code nobody listed
        (10001, InvalidParams),
        (110013, ExchangeRejected),  # leverage out of range: a definite refusal
        (10006, RateLimited),
    ],
)
@pytest.mark.parametrize("path", ["/v5/position/set-leverage", "/v5/position/trading-stop"])
async def test_answers_to_a_position_change_are_classified_and_never_retried(
    code: int, error: type[Exception], path: str
) -> None:
    stub = Stub({path: _ret(code, "x")})
    bybit = client(stub)

    async def send() -> None:
        if path.endswith("leverage"):
            await bybit.set_leverage(KEYS, "BTCUSDT", Decimal(5))
        else:
            await bybit.set_trading_stop(KEYS, "BTCUSDT", None, Decimal(1))

    with pytest.raises(error):
        await send()

    assert len(stub.requests) == 1


# --- end to end: the use cases on the adapter -----------------------------------------------------------


@dataclass
class Live:
    user_id: str
    stub: Stub
    leverage: SubmitSetLeverage
    stop: SubmitSetTradingStop


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
        "trading": bybit,
        "actions": bybit,
        "clock": clock,
        "ids": SequentialIds("act"),
        "reconnaissance_delay_s": 0,
        **scope,
    }
    return Live(
        user.id,
        stub,
        SubmitSetLeverage(db, credentials, **common),
        SubmitSetTradingStop(db, credentials, InstrumentRulesCache(bybit, clock), **common),
    )


@pytest.mark.req("FR-8", "FR-27")
async def test_a_leverage_the_account_already_has_is_a_success_read_back_from_the_position() -> None:
    stub = Stub(
        {
            "/v5/position/list": positions(),
            "/v5/position/set-leverage": _ret(110043, "Set leverage has not been modified."),
        }
    )
    world = await live(stub)

    result = await world.leverage(world.user_id, SetLeverageInput("BTCUSDT", "linear", "10"))

    assert (result.state, result.source) == (PositionChangeState.APPLIED, ResultSource.PLACED)
    assert [path for path, _ in world.stub.posts()] == ["/v5/position/set-leverage"]


@pytest.mark.req("FR-27")
@pytest.mark.parametrize(("applied", "shown"), [(True, "5"), (False, "10")])
async def test_a_leverage_change_that_timed_out_is_settled_by_the_positions_leverage(
    applied: bool, shown: str
) -> None:
    rows = {"leverage": "10"}
    stub = Stub({"/v5/position/list": lambda request: positions(leverage=rows["leverage"])})

    def timeout(request: httpx2.Request) -> dict[str, Any]:
        if applied:
            rows["leverage"] = "5"  # the exchange did it; the answer got lost
        raise httpx2.ReadTimeout("slow")

    stub.answers["/v5/position/set-leverage"] = timeout
    world = await live(stub)
    request = SetLeverageInput("BTCUSDT", "linear", "5")

    if applied:
        result = await world.leverage(world.user_id, request)
        assert (result.state, result.source) == (PositionChangeState.APPLIED, ResultSource.RECONNAISSANCE)
        assert result.leverage == Decimal(shown)
    else:
        with pytest.raises(OutcomeUnknown) as error:
            await world.leverage(world.user_id, request)
        assert error.value.details["reconnaissance"] == "found"
    assert len(world.stub.posts()) == 1  # never resent


@pytest.mark.req("FR-6", "FR-27")
async def test_a_stop_loss_is_read_back_from_the_position_list() -> None:
    levels = {"stopLoss": "78510.0"}
    stub = Stub({"/v5/position/list": lambda request: open_position(**levels)})

    def accept(request: httpx2.Request) -> dict[str, Any]:
        levels["stopLoss"] = "79000.5"
        return OK

    stub.answers["/v5/position/trading-stop"] = accept
    stub.answers["/v5/market/instruments-info"] = load_fixture("bybit/instruments_linear_btcusdt.json")
    world = await live(stub)

    result = await world.stop(world.user_id, SetTradingStopInput("BTCUSDT", "linear", stop_loss="79000.5"))

    assert (result.state, result.stop_loss) == (PositionChangeState.APPLIED, Decimal("79000.5"))
