"""Cancel, amend and cancel-all on the single trading path, on fakes (ticket 013; SPEC FR-5, FR-16,
FR-21, FR-25 - FR-27, FR-36, FR-43; PLAN D-10, D-20, D-21)."""

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

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
from guarded_exchange.domain.account import ConditionalKind, OrderOrigin, OrderSide, OrderStatus
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.audit import AuditRecord, ClientDescription
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT
from guarded_exchange.domain.errors import (
    ConfirmationChannelUnavailable,
    ExchangeRejected,
    IdempotencyConflict,
    InvalidParams,
    KillSwitchActive,
    LimitExceeded,
    NotFound,
    OutcomeUnknown,
    RateLimited,
)
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.kill_switch import KillSwitch
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from guarded_exchange.domain.users import Environment, User
from tests.fakes import (
    FakeCipher,
    FakeClock,
    FakeExchange,
    FakeKeyReader,
    FakeTrading,
    InMemoryDatabase,
    SequentialIds,
    make_rules,
    make_ticker,
)
from tests.fakes_trading import FakeOrderChanges, plain_order
from tests.unit.test_submit_action import KEYS, TRADING_KEY

pytestmark = pytest.mark.req("FR-5")

BTC_PERP = InstrumentRef(Category.LINEAR, "BTCUSDT")
CLIENT = ClientDescription.sanitized("claude-code", "2.1.312")
EVENTS = ("order_refused", "order_attempt", "order_outcome", "order_repeat")
UNKNOWN = OutcomeUnknown("no reliable answer from the exchange: ReadTimeout", details={"reason": "no_answer"})


@dataclass
class World:
    db: InMemoryDatabase
    clock: FakeClock
    trading: FakeTrading
    changes: FakeOrderChanges
    user: User
    cancel_uc: SubmitCancelOrder
    amend_uc: SubmitAmendOrder
    cancel_all_uc: SubmitCancelAllOrders

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

    async def cancel(self, order_id: str = "o-1", **kwargs: Any) -> Any:
        args = {"symbol": "BTCUSDT", "category": "linear", "order_id": order_id, **kwargs}
        return await self.cancel_uc(self.user.id, CancelOrderInput(**args), CLIENT)

    async def amend(self, order_id: str = "o-1", **kwargs: Any) -> Any:
        args = {"symbol": "BTCUSDT", "category": "linear", "order_id": order_id, **kwargs}
        return await self.amend_uc(self.user.id, AmendOrderInput(**args), CLIENT)

    async def cancel_all(self, **kwargs: Any) -> Any:
        args = {"symbol": "BTCUSDT", "category": "linear", **kwargs}
        return await self.cancel_all_uc(self.user.id, CancelAllOrdersInput(**args), CLIENT)

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
    trading, changes = FakeTrading(db=db), FakeOrderChanges(db=db)
    scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
    credentials = RequireTradingCredential(
        db,
        cipher,
        RecheckCredential(db, cipher, FakeKeyReader(permissions=TRADING_KEY), clock),
        clock,
        **scope,
    )
    common: dict[str, Any] = {
        "changes": changes,
        "clock": clock,
        "ids": SequentialIds("act"),
        "reconnaissance_delay_s": 0,
        **scope,
    }
    return World(
        db,
        clock,
        trading,
        changes,
        user,
        SubmitCancelOrder(db, credentials, **common),
        SubmitAmendOrder(db, credentials, InstrumentRulesCache(exchange, clock), trading=trading, **common),
        SubmitCancelAllOrders(db, credentials, **common),
    )


def payload(record: AuditRecord) -> dict[str, Any]:
    return dict(record.payload)


def kinds(records: list[AuditRecord]) -> list[str]:
    return [r.event_type for r in records]


# --- cancel_order -----------------------------------------------------------------------------------


@pytest.mark.req("FR-21", "FR-36")
async def test_an_external_order_is_cancelled_by_its_id_and_marked_external() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1", origin=OrderOrigin.EXTERNAL))

    result = await world.cancel()

    assert (result.state, result.source) == (ChangeState.CANCELLED, ResultSource.PLACED)
    assert result.plan.order.external
    assert world.changes.cancels == ["o-1"]
    # ADR-008 / D-10: attempt committed before the request, no exchange call inside a transaction
    [at_send] = world.changes.audit_at_send
    assert at_send[-1] == "order_attempt"
    assert "order_outcome" not in at_send
    assert world.changes.calls_in_transaction == 0
    attempt, outcome = world.journal()
    assert payload(attempt)["tool"] == "cancel_order"
    assert payload(attempt)["order"]["origin"] == "external"
    assert payload(attempt)["risk"] == "reduces"
    assert "client_order_id" not in payload(attempt)  # a cancel creates no order of its own
    assert (payload(outcome)["status"], payload(outcome)["state"]) == ("accepted", "cancelled")


@pytest.mark.req("FR-27")
async def test_an_accepted_cancel_not_visible_yet_is_requested_never_failed() -> None:
    """D-21: Bybit applies a cancel asynchronously; "accepted" is not "applied"."""
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.changes.apply = False  # still listed as open when read back

    result = await world.cancel()

    assert result.state is ChangeState.CANCEL_REQUESTED
    assert result.after is not None
    assert result.after.status is OrderStatus.NEW


@pytest.mark.req("FR-27")
@pytest.mark.parametrize(
    ("applied", "filled", "state"),
    [(True, False, ChangeState.CANCELLED), (False, True, ChangeState.FILLED)],
)
async def test_an_unknown_cancel_is_settled_by_the_orders_status_without_resending(
    applied: bool, filled: bool, state: ChangeState
) -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.changes.errors["o-1"] = UNKNOWN
    world.changes.apply_after_error = applied
    if filled:
        world.changes.on_send = lambda: world.changes.add(plain_order("o-1", status=OrderStatus.FILLED))

    result = await world.cancel()

    assert (result.state, result.source) == (state, ResultSource.RECONNAISSANCE)
    assert world.changes.cancels == ["o-1"]  # one request, never resent
    outcome = payload(world.journal("order_outcome")[0])
    assert (outcome["reconnaissance"], outcome["send_error"]["code"]) == ("found", "outcome_unknown")


@pytest.mark.req("FR-27")
@pytest.mark.parametrize(("hidden", "looked"), [(False, "found"), (True, "not_found")])
async def test_an_unknown_cancel_the_order_does_not_show_stays_unknown(hidden: bool, looked: str) -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.changes.errors["o-1"] = UNKNOWN
    world.changes.hide_after_send = hidden

    with pytest.raises(OutcomeUnknown) as error:
        await world.cancel()

    assert error.value.details["reconnaissance"] == looked
    assert error.value.details["order_id"] == "o-1"
    assert "safe" in (error.value.next_step or "")  # repeating a cancel cannot add risk
    assert world.changes.cancels == ["o-1"]
    assert payload(world.journal("order_outcome")[0])["status"] == "unknown"


@pytest.mark.req("FR-27")
async def test_a_failing_reconnaissance_is_unknown_not_an_internal_error() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.changes.errors["o-1"] = UNKNOWN
    world.changes.read_error = RuntimeError("boom")

    with pytest.raises(OutcomeUnknown) as error:
        await world.cancel()

    assert error.value.details["reconnaissance"] == "failed"


@pytest.mark.req("FR-36")
async def test_a_closed_order_is_not_found_and_nothing_is_sent() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1", status=OrderStatus.FILLED))

    with pytest.raises(NotFound) as error:
        await world.cancel()

    assert error.value.details["reason"] == "order_not_open"
    assert world.changes.cancels == []
    [refused] = world.journal()
    assert (payload(refused)["stage"], payload(refused)["code"]) == ("pre_check", "not_found")


async def test_a_conditional_order_or_a_position_tpsl_is_not_cancelled_here() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(
        plain_order("o-1", conditional=ConditionalKind.STOP_LOSS, status=OrderStatus.UNTRIGGERED)
    )

    with pytest.raises(InvalidParams) as error:
        await world.cancel()

    assert error.value.details["reason"] == "conditional_order"
    assert world.changes.cancels == []


@pytest.mark.req("FR-27")
async def test_the_exchange_saying_the_order_is_gone_is_a_definite_answer() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.changes.errors["o-1"] = NotFound("not open", details={"reason": "order_not_open"})
    world.changes.apply = False

    with pytest.raises(NotFound):
        await world.cancel()

    assert payload(world.journal("order_outcome")[0])["status"] == "rejected"


@pytest.mark.req("FR-25")
async def test_a_cancel_the_policy_wants_confirmed_is_refused_without_a_channel() -> None:
    world = make_world()
    await world.grant(confirm_actions=frozenset({ActionKind.CANCEL_ORDER}))
    world.changes.add(plain_order("o-1"))

    with pytest.raises(ConfirmationChannelUnavailable):
        await world.cancel()

    assert world.changes.cancels == []
    assert payload(world.journal("order_refused")[0])["code"] == "confirmation_channel_unavailable"


@pytest.mark.req("FR-16")
@pytest.mark.parametrize("state", [KillSwitch.USER, KillSwitch.ADMIN])
async def test_cancels_still_work_under_the_kill_switch(state: KillSwitch) -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"), plain_order("o-2"), plain_order("o-3"))
    world.switch_on(state)

    assert (await world.cancel()).state is ChangeState.CANCELLED
    assert (await world.amend("o-2", qty="0.001")).state is ChangeState.AMENDED  # a smaller order reduces
    assert [i.state for i in (await world.cancel_all()).items] == [ItemState.CLOSED, ItemState.CLOSED]


@pytest.mark.req("FR-26")
async def test_a_keyed_repeat_returns_the_first_answer_and_sends_nothing() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))

    first = await world.cancel(idempotency_key="k-1")
    again = await world.cancel(idempotency_key="k-1")

    assert again.source is ResultSource.REPLAY
    assert (again.state, again.plan.order) == (first.state, first.plan.order)
    assert world.changes.cancels == ["o-1"]
    assert payload(world.journal("order_repeat")[0])["status"] == "replay"


@pytest.mark.req("FR-26")
async def test_a_keyless_repeated_cancel_is_not_deduplicated() -> None:
    """D-20 (3): no fingerprint window for risk-reducing calls; the repeat finds the order closed."""
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    await world.cancel()

    with pytest.raises(NotFound):
        await world.cancel()

    assert world.journal("order_repeat") == []


# --- amend_order ------------------------------------------------------------------------------------


@pytest.mark.req("FR-36")
async def test_a_cheaper_buy_is_amended_and_only_the_price_is_sent() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))

    result = await world.amend(price="79990.5")

    assert (result.state, result.plan.risk.value) == (ChangeState.AMENDED, "reduces")
    [sent] = world.changes.amends
    assert (sent.qty, sent.price) == (None, Decimal("79990.5"))
    attempt = payload(world.journal("order_attempt")[0])
    assert attempt["change"]["price"] == "79990.5"
    assert world.changes.calls_in_transaction == 0
    assert world.trading.calls_in_transaction == 0


@pytest.mark.req("FR-13", "FR-43", "FR-17")
async def test_an_increasing_amend_faces_the_order_value_limit() -> None:
    world = make_world()
    await world.grant(max_order_notional_usdt=Decimal(200))
    world.changes.add(plain_order("o-1"))  # 0.002 x 80000 = 160

    with pytest.raises(LimitExceeded) as error:
        await world.amend(qty="0.003")  # 240 > 200

    assert error.value.details["limit"] == "max_order_notional_usdt"
    assert world.changes.amends == []
    assert payload(world.journal("order_refused")[0])["code"] == "limit_exceeded"
    await world.grant(max_order_notional_usdt=Decimal(240))
    allowed = await world.amend(qty="0.003")  # 240: within the limit
    assert allowed.plan.risk.value == "increases"


@pytest.mark.req("FR-16", "FR-43")
@pytest.mark.parametrize(
    ("args", "risk"), [({"qty": "0.003"}, "increases"), ({"price": "80100"}, "increases")]
)
async def test_an_increasing_amend_is_refused_under_the_kill_switch(args: dict[str, str], risk: str) -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.switch_on()

    with pytest.raises(KillSwitchActive) as error:
        await world.amend(**args)

    assert error.value.details == {"kill_switch": "user", "action": "amend_order", "risk": risk}
    assert world.changes.amends == []
    [refused] = world.journal()
    assert payload(refused)["stage"] == "pre_check"


@pytest.mark.req("FR-16")
async def test_the_kill_switch_is_checked_again_in_the_decision() -> None:
    """D-8: turned on after the early check (here: while the price band is read) still stops it."""
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.trading.before_band = world.switch_on

    with pytest.raises(KillSwitchActive):
        await world.amend(qty="0.003", price="80000.1")

    assert world.changes.amends == []
    [refused] = world.journal()
    assert "stage" not in payload(refused)  # refused in the decision, not before it


@pytest.mark.req("FR-27")
@pytest.mark.parametrize("applied", [True, False])
async def test_an_unknown_amend_is_settled_by_reading_the_order(applied: bool) -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.changes.errors["o-1"] = UNKNOWN
    world.changes.apply_after_error = applied

    if applied:
        result = await world.amend(price="79990.5")
        assert (result.state, result.source) == (ChangeState.AMENDED, ResultSource.RECONNAISSANCE)
    else:
        with pytest.raises(OutcomeUnknown) as error:
            await world.amend(price="79990.5")
        assert error.value.details["reconnaissance"] == "found"
        assert "price=80000" not in error.value.message
    assert len(world.changes.amends) == 1


@pytest.mark.req("FR-27")
async def test_an_accepted_amend_not_visible_yet_is_requested() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.changes.apply = False

    result = await world.amend(price="79990.5")

    assert result.state is ChangeState.AMEND_REQUESTED


async def test_a_definite_refusal_of_an_amend_is_recorded_as_rejected() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.changes.errors["o-1"] = ExchangeRejected("nope", details={"exchange_code": "110022"})

    with pytest.raises(ExchangeRejected):
        await world.amend(qty="0.001")

    assert payload(world.journal("order_outcome")[0])["status"] == "rejected"


# --- cancel_all_orders ------------------------------------------------------------------------------


@pytest.mark.req("FR-21", "FR-36")
async def test_cancel_all_cancels_the_snapshot_by_id_and_leaves_tpsl_alone() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(
        plain_order("ours"),
        plain_order("manual", origin=OrderOrigin.EXTERNAL, side=OrderSide.SELL, price="81000"),
        plain_order("sl", conditional=ConditionalKind.STOP_LOSS, status=OrderStatus.UNTRIGGERED),
    )

    result = await world.cancel_all()

    assert world.changes.cancels == ["ours", "manual"]  # concrete ids; the stop-loss is never sent
    assert [(i.order.order_id, i.state) for i in result.items] == [
        ("ours", ItemState.CLOSED),
        ("manual", ItemState.CLOSED),
    ]
    assert world.changes.orders["sl"].status is OrderStatus.UNTRIGGERED
    attempt = payload(world.journal("order_attempt")[0])
    assert attempt["external_count"] == 1
    assert [o["origin"] for o in attempt["orders"]] == ["system", "external"]
    assert world.changes.audit_at_send[0][-1] == "order_attempt"
    assert world.changes.calls_in_transaction == 0


@pytest.mark.req("FR-27")
async def test_an_unknown_cancel_stops_the_rest_and_is_settled_by_one_read() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("a"), plain_order("b"), plain_order("c"))
    world.changes.errors["b"] = UNKNOWN  # not applied: still listed

    result = await world.cancel_all()

    assert world.changes.cancels == ["a", "b"]  # never resent, the rest not attempted
    assert [i.state for i in result.items] == [ItemState.CLOSED, ItemState.UNKNOWN, ItemState.NOT_SENT]
    assert result.stopped_by == "outcome_unknown"
    assert payload(world.journal("order_outcome")[0])["status"] == "unknown"


async def test_a_rate_limit_before_anything_was_cancelled_is_the_answer() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("a"), plain_order("b"))
    world.changes.errors["a"] = RateLimited("slow down", retry_after_s=2)
    world.changes.apply = False

    with pytest.raises(RateLimited):
        await world.cancel_all(idempotency_key="k-1")

    assert world.changes.cancels == ["a"]
    world.changes.errors.clear()
    world.changes.apply = True
    assert len((await world.cancel_all(idempotency_key="k-1")).items) == 2  # the key was released


async def test_nothing_to_cancel_sends_nothing() -> None:
    world = make_world()
    await world.grant()

    result = await world.cancel_all()

    assert result.items == ()
    assert world.changes.cancels == []
    assert kinds(world.journal()) == ["order_attempt", "order_outcome"]


async def test_an_order_gone_meanwhile_is_reported_and_the_rest_continue() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("a"), plain_order("b"))
    world.changes.errors["a"] = NotFound("gone", details={"reason": "order_not_open"})

    result = await world.cancel_all()

    assert [i.state for i in result.items] == [ItemState.NOT_OPEN, ItemState.CLOSED]


@pytest.mark.req("FR-26")
async def test_amend_and_cancel_all_answers_are_replayed_exactly() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"), plain_order("o-2", origin=OrderOrigin.EXTERNAL))

    amended = await world.amend(qty="0.003", idempotency_key="a-1")
    swept = await world.cancel_all(idempotency_key="c-1")
    amended_again = await world.amend(qty="0.003", idempotency_key="a-1")
    swept_again = await world.cancel_all(idempotency_key="c-1")

    assert replace(amended_again, source=ResultSource.PLACED) == amended
    assert replace(swept_again, source=ResultSource.PLACED) == swept
    assert len(world.changes.amends) == 1
    assert world.changes.cancels == ["o-1", "o-2"]


@pytest.mark.req("FR-26")
async def test_one_key_for_two_different_tools_is_a_conflict() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    await world.cancel(idempotency_key="k-1")

    with pytest.raises(IdempotencyConflict):
        await world.cancel_all(idempotency_key="k-1")

    assert world.changes.cancels == ["o-1"]


@pytest.mark.req("FR-27")
async def test_a_failed_read_back_after_an_accepted_cancel_is_requested_not_an_error() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"))
    world.changes.read_error = RuntimeError("read broke")

    result = await world.cancel()

    assert (result.state, result.after) == (ChangeState.CANCEL_REQUESTED, None)
    assert payload(world.journal("order_outcome")[0])["status"] == "accepted"
