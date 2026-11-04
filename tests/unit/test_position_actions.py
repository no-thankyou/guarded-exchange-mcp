"""set_trading_stop, close_position and set_leverage on the single trading path, on fakes (ticket 014;
SPEC FR-6 - FR-8, FR-14, FR-16, FR-26, FR-27, FR-43; PLAN D-21)."""

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import pytest

from guarded_exchange.application.credentials import RecheckCredential, RequireTradingCredential
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.order_results import OrderResult, ResultSource
from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.application.position_actions import (
    ClosePositionInput,
    SetLeverageInput,
    SetTradingStopInput,
    SubmitClosePosition,
    SubmitSetLeverage,
    SubmitSetTradingStop,
)
from guarded_exchange.application.position_results import (
    LeverageResult,
    PositionChangeState,
    StopResult,
)
from guarded_exchange.domain.account import PositionSide, SymbolPosition
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.audit import AuditRecord, ClientDescription
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT
from guarded_exchange.domain.errors import (
    ConfirmationChannelUnavailable,
    ExchangeRejected,
    InstrumentRuleViolation,
    InvalidParams,
    KillSwitchActive,
    LimitExceeded,
    OutcomeUnknown,
    PositionNotFound,
)
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.kill_switch import KillSwitch
from guarded_exchange.domain.orders import OrderTimeInForce
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from guarded_exchange.domain.risk import RiskDirection
from guarded_exchange.domain.users import Environment, User
from tests.fakes import (
    FakeCipher,
    FakeClock,
    FakeExchange,
    FakeKeyReader,
    FakeTrading,
    InMemoryDatabase,
    SequentialIds,
    make_position,
    make_rules,
    make_ticker,
)
from tests.fakes_trading import FakePositionActions
from tests.unit.test_submit_action import KEYS, TRADING_KEY

pytestmark = pytest.mark.req("FR-43")

BTC_PERP = InstrumentRef(Category.LINEAR, "BTCUSDT")
CLIENT = ClientDescription.sanitized("claude-code", "2.1.312")
EVENTS = ("order_refused", "order_attempt", "order_outcome", "order_repeat")
UNKNOWN = OutcomeUnknown("no reliable answer from the exchange: ReadTimeout", details={"reason": "no_answer"})


@dataclass
class World:
    db: InMemoryDatabase
    clock: FakeClock
    trading: FakeTrading
    actions: FakePositionActions
    user: User
    stop_uc: SubmitSetTradingStop
    close_uc: SubmitClosePosition
    leverage_uc: SubmitSetLeverage

    async def grant(self, **changes: Any) -> None:
        patch = {
            "profile": Profile.TRADE,
            "allowlist": frozenset({BTC_PERP}),
            "confirm_actions": frozenset(),
            **changes,
        }
        await SetCeiling(self.db, self.clock)(self.user.name, PolicyPatch(**patch))

    def switch_on(self, state: KillSwitch = KillSwitch.USER) -> None:
        self.db.state.users[self.user.id] = replace(self.db.state.users[self.user.id], kill_switch=state)

    def hold(self, side: PositionSide = PositionSide.LONG, leverage: str = "10") -> None:
        position = replace(make_position(side=side), leverage=Decimal(leverage))
        self.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", position, Decimal(leverage))

    async def stop(self, **kwargs: Any) -> StopResult:
        args = {"symbol": "BTCUSDT", "category": "linear", **kwargs}
        return await self.stop_uc(self.user.id, SetTradingStopInput(**args), CLIENT)

    async def close(self, **kwargs: Any) -> OrderResult:
        args = {"symbol": "BTCUSDT", "category": "linear", **kwargs}
        return await self.close_uc(self.user.id, ClosePositionInput(**args), CLIENT)

    async def leverage(self, leverage: str, **kwargs: Any) -> LeverageResult:
        args = {"symbol": "BTCUSDT", "category": "linear", "leverage": leverage, **kwargs}
        return await self.leverage_uc(self.user.id, SetLeverageInput(**args), CLIENT)

    def journal(self, *types: str) -> list[AuditRecord]:
        records = self.db.state.audit.get(self.user.id, [])
        return [r for r in records if r.event_type in (types or EVENTS)]


def make_world() -> World:
    db, clock, cipher = InMemoryDatabase(), FakeClock(), FakeCipher()
    user = db.add_user(environment=Environment.TESTNET)
    db.add_credential(user, cipher, KEYS, permissions=TRADING_KEY)
    exchange = FakeExchange(
        tickers={("BTCUSDT", Category.LINEAR): make_ticker()},
        rules={("BTCUSDT", Category.LINEAR): make_rules()},
    )
    trading = FakeTrading(db=db)
    actions = FakePositionActions(trading)
    scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
    credentials = RequireTradingCredential(
        db,
        cipher,
        RecheckCredential(db, cipher, FakeKeyReader(permissions=TRADING_KEY), clock),
        clock,
        **scope,
    )
    rules = InstrumentRulesCache(exchange, clock)
    common: dict[str, Any] = {
        "trading": trading,
        "clock": clock,
        "ids": SequentialIds("act"),
        "reconnaissance_delay_s": 0,
        **scope,
    }
    world = World(
        db,
        clock,
        trading,
        actions,
        user,
        SubmitSetTradingStop(db, credentials, rules, actions=actions, **common),
        SubmitClosePosition(db, credentials, rules, market=exchange, **common),
        SubmitSetLeverage(db, credentials, actions=actions, **common),
    )
    world.hold()
    return world


def payload(record: AuditRecord) -> dict[str, Any]:
    return dict(record.payload)


# --- set_trading_stop --------------------------------------------------------------------------------


@pytest.mark.req("FR-6", "FR-13")
async def test_a_stop_on_a_position_above_the_order_limit_is_allowed_and_read_back() -> None:
    """Edge case (SPEC 6): the position's value (about 810 USDT) is above the 100 USDT order limit."""
    world = make_world()
    await world.grant(max_order_notional_usdt=Decimal(100))

    result = await world.stop(stop_loss="79000.5", take_profit="85000")

    assert (result.state, result.source) == (PositionChangeState.APPLIED, ResultSource.PLACED)
    assert (result.stop_loss, result.take_profit) == (Decimal("79000.5"), Decimal(85000))
    assert result.plan.risk is RiskDirection.REDUCES
    assert world.actions.stops == [("BTCUSDT", Decimal(85000), Decimal("79000.5"))]
    # ADR-008 / D-10: the attempt is committed before the request; no exchange call inside a transaction
    assert world.actions.trading.audit_at_send[-1][-1] == "order_attempt"
    assert world.trading.calls_in_transaction == 0
    attempt, outcome = world.journal()
    assert payload(attempt)["tool"] == "set_trading_stop"
    assert payload(attempt)["position"]["stop_loss"] == "78510"
    assert (payload(outcome)["status"], payload(outcome)["state"]) == ("accepted", "applied")


@pytest.mark.req("FR-6")
async def test_a_stop_without_a_position_or_off_the_tick_is_an_input_error_and_sends_nothing() -> None:
    world = make_world()
    await world.grant()

    with pytest.raises(InstrumentRuleViolation) as off_tick:
        await world.stop(stop_loss="79000.55")
    assert off_tick.value.details["rule"] == "tick_size"

    world.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", None, Decimal(10))
    with pytest.raises(PositionNotFound):
        await world.stop(stop_loss="79000.5")

    with pytest.raises(InvalidParams):
        await world.stop()  # nothing to set
    assert world.actions.stops == []
    assert world.journal() == []


@pytest.mark.req("FR-6", "FR-7", "FR-8")
@pytest.mark.parametrize("tool", ["stop", "close", "leverage"])
async def test_spot_is_refused_with_a_plain_message_and_nothing_is_sent(tool: str) -> None:
    world = make_world()
    await world.grant()
    args: dict[str, Any] = {"category": "spot", **({"stop_loss": "1"} if tool == "stop" else {})}

    with pytest.raises(InvalidParams) as error:
        await (world.leverage("3", category="spot") if tool == "leverage" else getattr(world, tool)(**args))

    assert error.value.details["reason"] == "linear_only"
    assert "spot has no positions" in error.value.message
    assert (world.actions.stops, world.actions.leverages, world.trading.sent) == ([], [], [])


@pytest.mark.req("FR-27")
async def test_an_accepted_stop_the_position_does_not_show_is_requested_never_failed() -> None:
    world = make_world()
    await world.grant()
    world.actions.apply = False

    result = await world.stop(stop_loss="79000.5")

    assert result.state is PositionChangeState.REQUESTED
    assert result.stop_loss == Decimal("78510.0")  # what the position shows: the old level
    assert payload(world.journal("order_outcome")[0])["state"] == "requested"


@pytest.mark.req("FR-27")
async def test_an_unknown_stop_is_settled_by_reading_the_position_without_resending() -> None:
    world = make_world()
    await world.grant()
    world.actions.error = UNKNOWN
    world.actions.apply_after_error = True  # it did happen

    result = await world.stop(stop_loss="79000.5")

    assert (result.state, result.source) == (PositionChangeState.APPLIED, ResultSource.RECONNAISSANCE)
    assert len(world.actions.stops) == 1
    outcome = payload(world.journal("order_outcome")[0])
    assert (outcome["reconnaissance"], outcome["send_error"]["code"]) == ("found", "outcome_unknown")


@pytest.mark.req("FR-27")
async def test_an_unknown_stop_the_position_does_not_show_stays_unknown() -> None:
    """Read-back mismatch after an unknown outcome: never reported as applied, never resent."""
    world = make_world()
    await world.grant()
    world.actions.error = UNKNOWN
    world.actions.apply_after_error = False

    with pytest.raises(OutcomeUnknown) as error:
        await world.stop(stop_loss="79000.5")

    assert error.value.details["reconnaissance"] == "found"
    assert "safe" in (error.value.next_step or "")
    assert len(world.actions.stops) == 1
    assert payload(world.journal("order_outcome")[0])["status"] == "unknown"


@pytest.mark.req("FR-27")
async def test_a_position_that_cannot_be_read_after_an_unknown_outcome_stays_unknown() -> None:
    world = make_world()
    await world.grant()
    world.actions.error = UNKNOWN
    world.actions.on_send = lambda: setattr(world.trading, "position_error", RuntimeError("boom"))

    with pytest.raises(OutcomeUnknown) as error:
        await world.stop(stop_loss="79000.5")

    assert error.value.details["reconnaissance"] == "failed"


@pytest.mark.req("FR-28")
async def test_a_refusal_of_the_exchange_is_journaled_as_rejected() -> None:
    world = make_world()
    await world.grant()
    world.actions.error = ExchangeRejected("no", details={"exchange_code": "110017"})

    with pytest.raises(ExchangeRejected):
        await world.stop(stop_loss="79000.5")

    assert payload(world.journal("order_outcome")[0])["status"] == "rejected"


@pytest.mark.req("FR-25")
async def test_a_stop_the_policy_wants_confirmed_is_refused_without_a_channel() -> None:
    world = make_world()
    await world.grant(confirm_actions=frozenset({ActionKind.SET_TRADING_STOP}))

    with pytest.raises(ConfirmationChannelUnavailable):
        await world.stop(stop_loss="79000.5")

    assert world.actions.stops == []
    assert payload(world.journal("order_refused")[0])["code"] == "confirmation_channel_unavailable"


@pytest.mark.req("FR-26")
async def test_a_keyed_repeat_returns_the_first_answer_and_sends_nothing() -> None:
    world = make_world()
    await world.grant()

    first = await world.stop(stop_loss="79000.5", idempotency_key="k-1")
    again = await world.stop(stop_loss="79000.5", idempotency_key="k-1")

    assert again.source is ResultSource.REPLAY
    assert (again.state, again.plan.action) == (first.state, first.plan.action)
    assert len(world.actions.stops) == 1
    assert payload(world.journal("order_repeat")[0])["status"] == "replay"


# --- close_position -----------------------------------------------------------------------------------


@pytest.mark.req("FR-7", "FR-13")
async def test_closing_a_position_above_the_order_limit_is_a_reduce_only_market_order() -> None:
    """Edge case (SPEC 6): the position's value is above the order limit; closing is never limited by it."""
    world = make_world()
    await world.grant(max_order_notional_usdt=Decimal(100))

    result = await world.close()

    [order] = world.trading.sent
    assert (order.side.value, order.qty, order.reduce_only) == ("sell", Decimal("0.010"), True)
    assert order.time_in_force is OrderTimeInForce.IOC
    assert order.client_order_id == f"gx-{result.action_id}"
    assert result.plan.risk is RiskDirection.REDUCES
    attempt, _ = world.journal()
    assert payload(attempt)["tool"] == "close_position"
    assert payload(attempt)["client_order_id"] == order.client_order_id


@pytest.mark.req("FR-7", "FR-43")
async def test_a_partial_close_is_cut_to_the_position_and_a_short_is_closed_with_a_buy() -> None:
    world = make_world()
    await world.grant()
    world.hold(PositionSide.SHORT)

    result = await world.close(qty="5")  # far more than the 0.010 held

    [order] = world.trading.sent
    assert (order.side.value, order.qty) == ("buy", Decimal("0.010"))
    assert result.plan.qty_adjustment is not None
    assert result.plan.qty_adjustment.value == "clamped_to_position"
    world.trading.sent.clear()

    await world.close(qty="0.004")
    assert world.trading.sent[0].qty == Decimal("0.004")


@pytest.mark.req("FR-7")
async def test_closing_without_a_position_sends_nothing() -> None:
    world = make_world()
    await world.grant()
    world.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", None, Decimal(10))

    with pytest.raises(PositionNotFound):
        await world.close()

    assert world.trading.sent == []


@pytest.mark.req("FR-27")
async def test_an_unknown_close_is_found_by_its_client_order_id_without_a_second_order() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = UNKNOWN

    with pytest.raises(OutcomeUnknown) as error:
        await world.close()

    assert len(world.trading.sent) == 1  # never resent
    assert error.value.details["reconnaissance"] == "not_found"
    assert payload(world.journal("order_outcome")[0])["status"] == "unknown"


@pytest.mark.req("FR-16")
async def test_a_kill_switch_does_not_stop_closing_or_protecting_a_position() -> None:
    world = make_world()
    await world.grant()
    world.switch_on(KillSwitch.ADMIN)

    assert (await world.stop(stop_loss="79000.5")).state is PositionChangeState.APPLIED
    assert (await world.leverage("5")).state is PositionChangeState.APPLIED  # lowering reduces risk
    await world.close()

    assert len(world.trading.sent) == 1


@pytest.mark.req("FR-25")
async def test_a_close_the_policy_wants_confirmed_is_refused_without_a_channel() -> None:
    world = make_world()
    await world.grant(confirm_actions=frozenset({ActionKind.CLOSE_POSITION}))

    with pytest.raises(ConfirmationChannelUnavailable):
        await world.close()

    assert world.trading.sent == []


# --- set_leverage -------------------------------------------------------------------------------------


@pytest.mark.req("FR-8", "FR-14")
async def test_a_leverage_above_the_policy_is_limit_exceeded_and_journaled() -> None:
    world = make_world()
    await world.grant(max_leverage=Decimal(5))
    world.hold(leverage="3")

    with pytest.raises(LimitExceeded) as error:
        await world.leverage("8")

    assert error.value.details["limit"] == "max_leverage"
    assert error.value.details["allowed"] == "5"
    assert world.actions.leverages == []
    refused = payload(world.journal("order_refused")[0])
    assert (refused["tool"], refused["code"]) == ("set_leverage", "limit_exceeded")


@pytest.mark.req("FR-8", "FR-14", "FR-43")
async def test_raising_the_leverage_within_the_policy_increases_risk_and_is_read_back() -> None:
    world = make_world()
    await world.grant(max_leverage=Decimal(5))
    world.hold(leverage="3")

    result = await world.leverage("5")

    assert (result.state, result.plan.risk) == (PositionChangeState.APPLIED, RiskDirection.INCREASES)
    assert (result.leverage, result.plan.current) == (Decimal(5), Decimal(3))
    attempt = payload(world.journal("order_attempt")[0])
    assert (attempt["risk"], attempt["leverage"]) == ("increases", {"from": "3", "to": "5"})


@pytest.mark.req("FR-8", "FR-43")
async def test_lowering_the_leverage_reduces_risk_and_needs_no_limit_even_above_the_policy() -> None:
    world = make_world()
    await world.grant(max_leverage=Decimal(2))  # the account is at x10, above the policy
    world.hold(leverage="10")

    result = await world.leverage("5")

    assert (result.state, result.plan.risk) == (PositionChangeState.APPLIED, RiskDirection.REDUCES)


@pytest.mark.req("FR-16", "FR-43")
@pytest.mark.parametrize("state", [KillSwitch.USER, KillSwitch.ADMIN])
async def test_the_kill_switch_blocks_raising_the_leverage_before_anything_is_sent(state: KillSwitch) -> None:
    world = make_world()
    await world.grant()
    world.hold(leverage="3")
    world.switch_on(state)

    with pytest.raises(KillSwitchActive):
        await world.leverage("5")

    assert world.actions.leverages == []
    assert payload(world.journal("order_refused")[0])["code"] == "kill_switch_active"


@pytest.mark.req("FR-16")
async def test_a_kill_switch_turned_on_after_the_early_check_still_blocks_a_raise() -> None:
    """D-8: the switch is read again under the lock."""
    world = make_world()
    await world.grant()
    world.hold(leverage="3")
    world.trading.before_band = None
    original = world.trading.get_symbol_position

    async def flip(keys: Any, symbol: str) -> SymbolPosition:
        found = await original(keys, symbol)
        world.switch_on()  # right after the call read the exchange, before its decision
        return found

    world.trading.get_symbol_position = flip  # type: ignore[method-assign]

    with pytest.raises(KillSwitchActive):
        await world.leverage("5")

    assert world.actions.leverages == []


@pytest.mark.req("FR-27")
async def test_an_unknown_leverage_change_is_settled_by_the_positions_leverage_without_resending() -> None:
    world = make_world()
    await world.grant()
    world.actions.error = UNKNOWN
    world.actions.apply_after_error = True

    result = await world.leverage("5")

    assert (result.state, result.source) == (PositionChangeState.APPLIED, ResultSource.RECONNAISSANCE)
    assert world.actions.leverages == [("BTCUSDT", Decimal(5))]


@pytest.mark.req("FR-27")
async def test_an_unknown_leverage_change_the_exchange_does_not_show_stays_unknown() -> None:
    world = make_world()
    await world.grant()
    world.actions.error = UNKNOWN

    with pytest.raises(OutcomeUnknown) as error:
        await world.leverage("5")

    assert len(world.actions.leverages) == 1
    assert error.value.details["reconnaissance"] == "found"
    assert payload(world.journal("order_outcome")[0])["status"] == "unknown"


@pytest.mark.req("FR-27")
async def test_an_accepted_leverage_not_visible_yet_is_requested() -> None:
    world = make_world()
    await world.grant()
    world.actions.apply = False

    result = await world.leverage("5")

    assert (result.state, result.leverage) == (PositionChangeState.REQUESTED, Decimal(10))


@pytest.mark.req("FR-26")
async def test_a_keyed_leverage_repeat_is_answered_from_storage() -> None:
    world = make_world()
    await world.grant()

    first = await world.leverage("5", idempotency_key="k-1")
    again = await world.leverage("5", idempotency_key="k-1")

    assert (first.source, again.source) == (ResultSource.PLACED, ResultSource.REPLAY)
    assert len(world.actions.leverages) == 1
