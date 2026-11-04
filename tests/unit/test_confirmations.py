"""Pending confirmations, the CLI decision and the ConfirmedActionExecutor, on fakes (tickets 016, 017;
SPEC FR-19 - FR-25, FR-29, FR-42, FR-47, FR-48; PLAN ADR-005, ADR-006, D-3, D-8, D-9, D-17, D-20).

One world with every action path (place/cancel/amend/cancel-all and the position actions of 014), the
channel enabled and an executor that is driven by hand (``run_once``)."""

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import anyio
import pytest

from guarded_exchange.application.confirmation import ConfirmationPending, ConfirmationSupport
from guarded_exchange.application.credentials import (
    DeleteCredential,
    OpenCredentialKeys,
    RecheckCredential,
    RequireTradingCredential,
)
from guarded_exchange.application.execute_confirmed import ConfirmablePath, ConfirmedActionExecutor
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.order_changes import (
    AmendOrderInput,
    CancelAllOrdersInput,
    CancelOrderInput,
    SubmitAmendOrder,
    SubmitCancelAllOrders,
    SubmitCancelOrder,
)
from guarded_exchange.application.policy import ResetOverrides, SetCeiling, TightenPolicy
from guarded_exchange.application.position_actions import (
    ClosePositionInput,
    SetLeverageInput,
    SetTradingStopInput,
    SubmitClosePosition,
    SubmitSetLeverage,
    SubmitSetTradingStop,
)
from guarded_exchange.application.resolve_confirmation import (
    CancelConfirmation,
    GetConfirmationStatus,
    ResolveConfirmation,
)
from guarded_exchange.application.submit_action import PlaceOrderInput, SubmitPlaceOrder
from guarded_exchange.domain.account import OrderOrigin, PositionSide, SymbolPosition
from guarded_exchange.domain.actions import ALL_ACTION_KINDS, ActionKind
from guarded_exchange.domain.audit import AuditRecord, ClientDescription
from guarded_exchange.domain.confirmation import ConfirmationStatus, DecisionChannel, PendingConfirmation
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT
from guarded_exchange.domain.errors import (
    ConfirmationChannelUnavailable,
    ConfirmationExpired,
    ConfirmationNotPending,
    NotFound,
    PendingLimitReached,
)
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.kill_switch import KillSwitch
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from guarded_exchange.domain.users import Environment, User, UserStatus
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
from tests.fakes_trading import FakeOrderChanges, FakePositionActions, plain_order
from tests.unit.test_submit_action import KEYS, TRADING_KEY

pytestmark = pytest.mark.req("FR-19", "FR-47")

BTC_PERP = InstrumentRef(Category.LINEAR, "BTCUSDT")
CLIENT = ClientDescription.sanitized("claude-code", "2.1.312")
S = ConfirmationStatus


@dataclass
class World:
    db: InMemoryDatabase
    clock: FakeClock
    exchange: FakeExchange
    trading: FakeTrading
    changes: FakeOrderChanges
    actions: FakePositionActions
    user: User
    place: SubmitPlaceOrder
    cancel: SubmitCancelOrder
    amend: SubmitAmendOrder
    cancel_all: SubmitCancelAllOrders
    stop: SubmitSetTradingStop
    close: SubmitClosePosition
    leverage: SubmitSetLeverage
    executor: ConfirmedActionExecutor
    resolve: ResolveConfirmation
    cancel_confirmation: CancelConfirmation
    status: GetConfirmationStatus

    async def grant(self, **changes: Any) -> None:
        patch = {
            "profile": Profile.TRADE,
            "allowlist": frozenset({BTC_PERP}),
            "confirm_actions": ALL_ACTION_KINDS,
            **changes,
        }
        await SetCeiling(self.db, self.clock)(self.user.name, PolicyPatch(**patch))

    async def buy(self, **kwargs: Any) -> Any:
        args = {
            "symbol": "BTCUSDT",
            "category": "linear",
            "side": "buy",
            "order_type": "market",
            "qty": "0.001",
            **kwargs,
        }
        return await self.place(self.user.id, PlaceOrderInput(**args), CLIENT)

    async def parked(self, call: Any) -> PendingConfirmation:
        """Run a trading call that must end as a pending confirmation; the stored confirmation."""
        with pytest.raises(ConfirmationPending) as parked:
            await call
        return self.db.state.confirmations[parked.value.pending.id]

    async def approve(self, pending: PendingConfirmation) -> None:
        await self.resolve.approve(pending.id)

    def stored(self, pending: PendingConfirmation) -> PendingConfirmation:
        return self.db.state.confirmations[pending.id]

    def journal(self, *types: str) -> list[AuditRecord]:
        records = self.db.state.audit.get(self.user.id, [])
        return [r for r in records if not types or r.event_type in types]

    def hold(self, side: PositionSide = PositionSide.LONG, leverage: str = "10", size: str = "0.010") -> None:
        position = replace(make_position(side=side), leverage=Decimal(leverage), size=Decimal(size))
        self.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", position, Decimal(leverage))

    def switch_on(self, state: KillSwitch = KillSwitch.USER) -> None:
        self.db.state.users[self.user.id] = replace(self.db.state.users[self.user.id], kill_switch=state)


def make_world(*, cli_enabled: bool = True, max_pending: int | None = None) -> World:
    db, clock, cipher = InMemoryDatabase(), FakeClock(), FakeCipher()
    user = db.add_user(environment=Environment.TESTNET)
    db.add_credential(user, cipher, KEYS, permissions=TRADING_KEY)
    exchange = FakeExchange(
        tickers={("BTCUSDT", Category.LINEAR): make_ticker()},
        rules={("BTCUSDT", Category.LINEAR): make_rules()},
    )
    trading, changes = FakeTrading(db=db), FakeOrderChanges(db=db)
    actions = FakePositionActions(trading)
    scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
    credentials = RequireTradingCredential(
        db,
        cipher,
        RecheckCredential(db, cipher, FakeKeyReader(permissions=TRADING_KEY), clock),
        clock,
        **scope,
    )
    support = ConfirmationSupport(cli_enabled=cli_enabled, open_keys=OpenCredentialKeys(db, cipher))
    rules = InstrumentRulesCache(exchange, clock)
    base: dict[str, Any] = {
        "clock": clock,
        "ids": SequentialIds("act"),
        "reconnaissance_delay_s": 0,
        "confirmations": support,
        **scope,
    }
    place = SubmitPlaceOrder(db, credentials, rules, market=exchange, trading=trading, **base)
    cancel = SubmitCancelOrder(db, credentials, changes=changes, **base)
    amend = SubmitAmendOrder(db, credentials, rules, trading=trading, changes=changes, **base)
    cancel_all = SubmitCancelAllOrders(db, credentials, changes=changes, **base)
    stop = SubmitSetTradingStop(db, credentials, rules, actions=actions, trading=trading, **base)
    close = SubmitClosePosition(db, credentials, rules, market=exchange, trading=trading, **base)
    leverage = SubmitSetLeverage(db, credentials, actions=actions, trading=trading, **base)
    paths: dict[ActionKind, ConfirmablePath] = {
        ActionKind.PLACE_ORDER: place,
        ActionKind.CANCEL_ORDER: cancel,
        ActionKind.AMEND_ORDER: amend,
        ActionKind.CANCEL_ALL_ORDERS: cancel_all,
        ActionKind.SET_TRADING_STOP: stop,
        ActionKind.CLOSE_POSITION: close,
        ActionKind.SET_LEVERAGE: leverage,
    }
    world = World(
        db, clock, exchange, trading, changes, actions, user, place, cancel, amend, cancel_all, stop, close,
        leverage, ConfirmedActionExecutor(db, clock, paths),
        ResolveConfirmation(db, clock, cli_enabled=True),
        CancelConfirmation(db, clock),
        GetConfirmationStatus(db, clock),
    )  # fmt: skip
    world.hold()
    if max_pending is not None:
        db.state.policies  # noqa: B018 - the ceiling is set per test through ``grant``
    return world


def j(value: object) -> Any:
    """A stored JSON document, indexable in assertions."""
    return value


def decided(world: World) -> list[dict[str, Any]]:
    return [dict(r.payload) for r in world.journal("confirmation_decided")]


# --- creating a pending confirmation (FR-47 (b), FR-19, FR-25, D-17, D-20) --------------------------------


async def test_a_call_that_needs_confirmation_is_parked_and_nothing_is_sent() -> None:
    world = make_world()
    await world.grant()

    pending = await world.parked(world.buy())

    assert (pending.status, pending.tool, pending.risk.value) == (
        S.PENDING,
        ActionKind.PLACE_ORDER,
        "increases",
    )
    assert pending.expires_at - pending.created_at == timedelta(seconds=120)  # FR-23 default
    assert world.trading.sent == []
    [requested] = world.journal("confirmation_requested")
    assert dict(requested.payload)["confirmation_id"] == pending.id
    assert world.journal("order_attempt") == []
    # FR-21: the server's own plan is what the human sees, including the worst price of a market order
    assert j(pending.shown)["order"]["worst_price"] is not None
    assert j(pending.action)["qty"] == "0.001"


@pytest.mark.req("FR-25")
async def test_without_an_enabled_channel_it_is_refused_and_no_pending_is_created() -> None:
    world = make_world(cli_enabled=False)
    await world.grant()

    with pytest.raises(ConfirmationChannelUnavailable) as refused:
        await world.buy()

    assert refused.value.details["reason"] == "no_trusted_channel"
    assert world.db.state.confirmations == {}
    assert world.trading.sent == []


@pytest.mark.req("FR-19")
async def test_more_waiting_confirmations_than_the_limit_are_refused() -> None:
    world = make_world()
    await world.grant(max_pending=1)
    await world.parked(world.buy())

    with pytest.raises(PendingLimitReached) as refused:
        await world.buy(qty="0.002")

    assert refused.value.details["limit"] == "max_pending"
    assert len(world.db.state.confirmations) == 1


@pytest.mark.req("FR-26")
async def test_a_repeat_without_a_key_returns_the_same_pending_even_after_the_fingerprint_window() -> None:
    world = make_world()
    await world.grant()
    first = await world.parked(world.buy())

    world.clock.advance(timedelta(seconds=40))  # past the 30 s fingerprint window (D-20 (2))
    with pytest.raises(ConfirmationPending) as repeat:
        await world.buy()

    assert repeat.value.pending.id == first.id
    assert repeat.value.created is False
    assert len(world.db.state.confirmations) == 1


@pytest.mark.req("FR-26")
async def test_a_repeat_with_the_same_key_meets_the_same_pending_and_a_different_call_conflicts() -> None:
    world = make_world()
    await world.grant()
    first = await world.parked(world.buy(idempotency_key="k-1"))

    with pytest.raises(ConfirmationPending) as repeat:
        await world.buy(idempotency_key="k-1")
    assert repeat.value.pending.id == first.id

    from guarded_exchange.domain.errors import IdempotencyConflict  # noqa: PLC0415

    with pytest.raises(IdempotencyConflict):
        await world.buy(idempotency_key="k-1", qty="0.002")


# --- the human's decision (FR-20, FR-24, FR-42, FR-23) ---------------------------------------------------


async def test_a_double_approval_executes_exactly_once() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())

    await world.approve(pending)
    with pytest.raises(ConfirmationNotPending) as again:
        await world.approve(pending)
    await world.executor.run_once()
    await world.executor.run_once()

    assert again.value.details["status"] == "approved"
    assert len(world.trading.sent) == 1
    assert world.trading.sent[0].client_order_id == f"gx-{pending.id}"  # deterministic from the pending id
    assert world.stored(pending).status is S.EXECUTED
    [attempt] = world.journal("order_attempt")
    assert dict(attempt.payload)["decision"] == "confirmed"
    assert dict(attempt.payload)["confirmation_id"] == pending.id


async def test_two_concurrent_approvals_have_one_winner() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    outcomes: list[object] = []

    async def approve() -> None:
        try:
            outcomes.append(await world.resolve.approve(pending.id))
        except ConfirmationNotPending as lost:
            outcomes.append(lost)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(approve)
        tasks.start_soon(approve)

    assert sorted(type(o).__name__ for o in outcomes) == ["ConfirmationNotPending", "PendingConfirmation"]


@pytest.mark.req("FR-23")
async def test_approving_after_the_ttl_is_expired_and_executes_nothing() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())

    world.clock.advance(timedelta(seconds=121))
    with pytest.raises(ConfirmationExpired):
        await world.approve(pending)
    await world.executor.run_once()

    assert world.stored(pending).status is S.EXPIRED  # committed, not rolled back with the error
    assert world.trading.sent == []
    assert [d["status"] for d in decided(world)] == ["expired"]


@pytest.mark.req("FR-24", "FR-42")
async def test_a_rejection_is_journaled_with_its_channel_and_nothing_is_sent() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())

    await world.resolve.reject(pending.id)
    await world.executor.run_once()

    stored = world.stored(pending)
    assert (stored.status, stored.channel, stored.decided_by) == (S.REJECTED, DecisionChannel.CLI, "cli:user")
    assert [(d["status"], d["channel"]) for d in decided(world)] == [("rejected", "cli")]
    assert world.trading.sent == []


@pytest.mark.req("FR-42")
async def test_the_cli_cannot_decide_unless_the_admin_enabled_it() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    off = ResolveConfirmation(world.db, world.clock, cli_enabled=False)

    with pytest.raises(ConfirmationChannelUnavailable):
        await off.approve(pending.id)

    assert world.stored(pending).status is S.PENDING


@pytest.mark.req("FR-23")
async def test_an_approval_one_second_before_the_ttl_still_executes() -> None:
    """D-3: the deadline of execution is the approval plus 30 s, not the request's own expiry."""
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())

    world.clock.advance(timedelta(seconds=119))
    await world.approve(pending)
    world.clock.advance(timedelta(seconds=5))  # now past expires_at
    await world.executor.run_once()

    assert world.stored(pending).status is S.EXECUTED
    assert len(world.trading.sent) == 1


async def test_an_approval_that_is_not_started_in_time_expires_after_approval() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)

    world.clock.advance(timedelta(seconds=31))
    await world.executor.run_once()

    assert world.stored(pending).status is S.EXPIRED_AFTER_APPROVAL
    assert world.trading.sent == []
    assert decided(world)[-1]["status"] == "expired_after_approval"


async def test_waiting_confirmations_expire_in_the_sweep_and_free_their_slot() -> None:
    world = make_world()
    await world.grant(max_pending=1)
    pending = await world.parked(world.buy())

    world.clock.advance(timedelta(seconds=121))
    await world.executor.run_once()

    assert world.stored(pending).status is S.EXPIRED
    await world.parked(world.buy())  # the slot is free again


# --- what the agent has: status and cancel; never an approval (FR-47, D-6, AC-4) --------------------------


async def test_the_agent_reads_its_status_and_cannot_see_another_users_confirmation() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())

    seen = await world.status(world.user.id, pending.id)
    assert seen.status is S.PENDING
    other = world.db.add_user("mallory")
    with pytest.raises(NotFound):
        await world.status(other.id, pending.id)
    with pytest.raises(NotFound):
        await world.cancel_confirmation(other.id, pending.id)


async def test_cancelling_your_own_pending_or_approved_confirmation_executes_nothing() -> None:
    world = make_world()
    await world.grant()
    waiting = await world.parked(world.buy())
    approved = await world.parked(world.buy(qty="0.002"))
    await world.approve(approved)

    await world.cancel_confirmation(world.user.id, waiting.id)
    await world.cancel_confirmation(world.user.id, approved.id)
    await world.executor.run_once()

    assert world.stored(waiting).status is S.CANCELLED
    assert world.stored(approved).status is S.CANCELLED
    assert world.trading.sent == []
    assert ("cancelled", "mcp:agent") in [(d["status"], d["channel"]) for d in decided(world)]
    with pytest.raises(ConfirmationNotPending):  # an agent cannot revive or re-decide it
        await world.resolve.approve(waiting.id)


async def test_an_executing_confirmation_cannot_be_cancelled() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)
    await world.executor.run_once()

    with pytest.raises(ConfirmationNotPending):
        await world.cancel_confirmation(world.user.id, pending.id)
    assert len(world.trading.sent) == 1


# --- FR-29, D-9: key replaced or deleted -------------------------------------------------------------------


@pytest.mark.req("FR-29")
async def test_deleting_the_key_cancels_waiting_and_approved_confirmations() -> None:
    world = make_world()
    await world.grant()
    waiting = await world.parked(world.buy())
    approved = await world.parked(world.buy(qty="0.002"))
    await world.approve(approved)

    scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
    await DeleteCredential(world.db, world.clock, **scope)(world.user.name)
    await world.executor.run_once()

    assert world.stored(waiting).status is S.CANCELLED
    assert world.stored(approved).status is S.CANCELLED
    assert world.trading.sent == []
    assert {d["channel"] for d in decided(world)} >= {"system"}


# --- FR-48, D-8, edge cases of SPEC 6: everything is checked again at execution ---------------------


@pytest.mark.req("FR-16", "FR-48")
async def test_a_kill_switch_after_approval_refuses_an_increase_but_lets_a_reduction_through() -> None:
    world = make_world()
    await world.grant()
    increase = await world.parked(world.buy())
    reduce = await world.parked(world.buy(side="sell", reduce_only=True))
    await world.approve(increase)
    await world.approve(reduce)

    world.switch_on()
    await world.executor.run_once()

    assert world.stored(increase).status is S.FAILED
    assert j(world.stored(increase).result)["outcome"]["code"] == "kill_switch_active"
    assert world.stored(reduce).status is S.EXECUTED
    assert [o.side.value for o in world.trading.sent] == ["sell"]


@pytest.mark.req("FR-29", "FR-48")
async def test_revoked_access_after_approval_executes_nothing() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)

    world.db.state.users[world.user.id] = replace(world.user, status=UserStatus.REVOKED)
    await world.executor.run_once()

    assert world.stored(pending).status is S.FAILED
    assert world.trading.sent == []


@pytest.mark.req("FR-29", "FR-48")
async def test_a_replaced_key_after_approval_executes_nothing() -> None:
    """The credential id is part of the confirmation: a different active key is not the one approved."""
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)

    cipher = FakeCipher()
    old = world.db.state.credentials[next(iter(world.db.state.credentials))][0]
    world.db.state.credentials[old.id] = (
        replace(old, status=old.status.__class__("deleted"), deleted_at=world.clock.now()),
        world.db.state.credentials[old.id][1],
    )
    world.db.add_credential(world.user, cipher, KEYS, permissions=TRADING_KEY, credential_id="cred-new")
    await world.executor.run_once()

    assert world.stored(pending).status is S.FAILED
    assert world.trading.sent == []


@pytest.mark.req("FR-48")
async def test_a_changed_environment_after_approval_executes_nothing() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)

    world.db.state.users[world.user.id] = replace(world.user, environment=Environment.MAINNET)
    await world.executor.run_once()

    assert world.stored(pending).status is S.FAILED
    assert world.trading.sent == []


@pytest.mark.req("FR-15", "FR-48")
async def test_an_instrument_removed_from_the_allowlist_while_waiting_is_refused() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)

    await world.grant(allowlist=frozenset())
    await world.executor.run_once()

    assert world.stored(pending).status is S.FAILED
    assert j(world.stored(pending).result)["outcome"]["code"] == "limit_exceeded"
    assert world.trading.sent == []


@pytest.mark.req("FR-12", "FR-48")
async def test_a_tightened_policy_or_a_lowered_ceiling_while_waiting_is_applied_at_execution() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)

    await world.grant(max_order_notional_usdt=Decimal(10))  # the admin lowers the ceiling
    await world.executor.run_once()

    assert world.stored(pending).status is S.FAILED
    assert world.trading.sent == []

    await world.grant(max_order_notional_usdt=Decimal(1000))
    second = await world.parked(world.buy(qty="0.002"))  # the user tightens his own policy
    await world.approve(second)
    await TightenPolicy(world.db, world.clock)(world.user.name, PolicyPatch(profile=Profile.READ_ONLY))
    await world.executor.run_once()
    assert world.stored(second).status is S.FAILED


@pytest.mark.req("FR-12", "FR-48")
async def test_a_weakened_policy_neither_approves_nor_extends_a_waiting_confirmation() -> None:
    """Ticket 034 review: `reset-overrides` may loosen the effective policy while a pending exists."""
    world = make_world()
    await world.grant()
    await TightenPolicy(world.db, world.clock)(world.user.name, PolicyPatch(confirmation_ttl_s=60))
    pending = await world.parked(world.buy())
    assert pending.expires_at - pending.created_at == timedelta(seconds=60)

    await ResetOverrides(world.db, world.clock)(world.user.name, None)  # TTL is 120 s again
    await world.executor.run_once()

    assert world.stored(pending).status is S.PENDING  # not confirmed by the change
    assert world.stored(pending).expires_at == pending.expires_at  # not extended
    world.clock.advance(timedelta(seconds=61))
    await world.executor.run_once()
    assert world.stored(pending).status is S.EXPIRED
    assert world.trading.sent == []


@pytest.mark.req("FR-22")
async def test_a_changed_record_is_refused_because_it_no_longer_matches_its_digest() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)

    tampered = replace(world.stored(pending), action={**pending.action, "qty": "5"})
    world.db.state.confirmations[pending.id] = tampered
    await world.executor.run_once()

    assert world.stored(pending).status is S.FAILED
    assert j(world.stored(pending).result)["outcome"]["details"]["reason"] == "confirmation_tampered"
    assert world.trading.sent == []


@pytest.mark.req("FR-48")
async def test_a_market_order_is_held_to_the_worst_price_that_was_shown() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    shown = Decimal(j(pending.shown)["order"]["price"])
    await world.approve(pending)

    # the market runs away (up to the exchange's band) before the executor starts
    ticker = make_ticker()
    world.exchange.tickers[("BTCUSDT", Category.LINEAR)] = replace(ticker, ask_price=Decimal("81500"))
    await world.executor.run_once()

    [sent] = world.trading.sent
    assert sent.price <= shown  # never worse than shown; the IOC fills what it can, the rest is cancelled
    assert world.stored(pending).status is S.EXECUTED


@pytest.mark.req("FR-48")
async def test_what_the_bound_leaves_unfilled_is_reported_as_the_real_fill() -> None:
    from guarded_exchange.domain.orders import FillStatus, OrderFill  # noqa: PLC0415

    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)
    # the price ran away: the IOC at the shown bound filled a part and the exchange cancelled the rest
    world.trading.fills[f"gx-{pending.id}"] = OrderFill(
        "order-1",
        FillStatus.CANCELLED,
        Decimal("0.001"),
        Decimal("80900"),
        Decimal("0.0004"),
        Decimal("80900"),
    )

    await world.executor.run_once()

    stored = world.stored(pending)
    assert stored.status is S.EXECUTED
    assert j(stored.result)["outcome"]["fill"]["filled_qty"] == "0.0004"
    assert j(stored.result)["outcome"]["fill"]["status"] == "cancelled"


@pytest.mark.req("FR-43", "FR-48")
async def test_a_reduce_only_order_is_cut_to_the_position_that_is_left() -> None:
    world = make_world()
    await world.grant()
    world.hold(size="0.010")
    pending = await world.parked(world.buy(side="sell", reduce_only=True, qty="0.010"))
    await world.approve(pending)

    world.hold(size="0.004")  # the position shrank while it waited
    await world.executor.run_once()

    [sent] = world.trading.sent
    assert sent.qty == Decimal("0.004")  # min(shown 0.010, position 0.004)
    assert sent.reduce_only is True


@pytest.mark.req("FR-5", "FR-21", "FR-22")
async def test_cancel_all_cancels_exactly_the_orders_the_human_was_shown() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"), plain_order("o-2", origin=OrderOrigin.EXTERNAL))
    pending = await world.parked(world.cancel_all(world.user.id, CancelAllOrdersInput("BTCUSDT", "linear")))
    assert [o["order_id"] for o in j(pending.shown)["orders"]] == ["o-1", "o-2"]
    assert j(pending.shown)["external_count"] == 1
    await world.approve(pending)

    world.changes.add(plain_order("o-new", origin=OrderOrigin.EXTERNAL))  # appeared after the snapshot
    await world.executor.run_once()

    assert sorted(world.changes.cancels) == ["o-1", "o-2"]
    assert world.changes.orders["o-new"].status.value == "new"
    assert world.stored(pending).status is S.EXECUTED


async def test_cancel_and_amend_are_confirmed_too() -> None:
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1"), plain_order("o-2"))
    cancel = await world.parked(world.cancel(world.user.id, CancelOrderInput("BTCUSDT", "linear", "o-1")))
    amend = await world.parked(
        world.amend(world.user.id, AmendOrderInput("BTCUSDT", "linear", "o-2", qty="0.001"))
    )
    assert world.changes.cancels == []
    assert world.changes.amends == []
    await world.approve(cancel)
    await world.approve(amend)

    await world.executor.run_once()

    assert world.changes.cancels == ["o-1"]
    assert [a.order_id for a in world.changes.amends] == ["o-2"]
    assert world.stored(cancel).status is S.EXECUTED
    assert world.stored(amend).status is S.EXECUTED


# --- the position actions of ticket 014 get the same machinery ----------------------------------------------


@pytest.mark.req("FR-6")
async def test_set_trading_stop_goes_through_pending_approval_and_the_executor() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(
        world.stop(world.user.id, SetTradingStopInput("BTCUSDT", "linear", stop_loss="79000.5"), CLIENT)
    )
    assert world.actions.stops == []
    await world.approve(pending)

    await world.executor.run_once()

    assert world.actions.stops == [("BTCUSDT", None, Decimal("79000.5"))]
    assert world.stored(pending).status is S.EXECUTED


@pytest.mark.req("FR-6", "FR-48")
async def test_set_trading_stop_is_refused_at_execution_when_the_position_is_gone() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(
        world.stop(world.user.id, SetTradingStopInput("BTCUSDT", "linear", stop_loss="79000.5"), CLIENT)
    )
    await world.approve(pending)

    world.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", None, Decimal(10))
    await world.executor.run_once()

    assert world.stored(pending).status is S.FAILED
    assert world.actions.stops == []


@pytest.mark.req("FR-7")
async def test_close_position_goes_through_pending_and_is_cut_to_the_position() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.close(world.user.id, ClosePositionInput("BTCUSDT", "linear"), CLIENT))
    assert j(pending.shown)["order"]["qty"] == "0.01"
    await world.approve(pending)

    world.hold(size="0.004")
    await world.executor.run_once()

    [sent] = world.trading.sent
    assert (sent.qty, sent.reduce_only, sent.side.value) == (Decimal("0.004"), True, "sell")
    assert world.stored(pending).status is S.EXECUTED


@pytest.mark.req("FR-8")
async def test_set_leverage_goes_through_pending_and_the_executor() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(
        world.leverage(world.user.id, SetLeverageInput("BTCUSDT", "linear", "5"), CLIENT)
    )
    assert world.actions.leverages == []
    await world.approve(pending)

    await world.executor.run_once()

    assert world.actions.leverages == [("BTCUSDT", Decimal(5))]
    assert world.stored(pending).status is S.EXECUTED


@pytest.mark.req("FR-43", "FR-48")
async def test_a_leverage_change_approved_as_a_reduction_that_became_an_increase_is_refused() -> None:
    world = make_world()
    await world.grant()
    world.hold(leverage="10")
    pending = await world.parked(
        world.leverage(world.user.id, SetLeverageInput("BTCUSDT", "linear", "5"), CLIENT)
    )
    assert pending.risk.value == "reduces"  # 10 -> 5
    await world.approve(pending)

    world.hold(leverage="3")  # now 3 -> 5 would raise the leverage
    await world.executor.run_once()

    stored = world.stored(pending)
    assert stored.status is S.FAILED
    assert j(stored.result)["outcome"]["code"] == "risk_direction_changed"
    assert world.actions.leverages == []


# --- crash, restart and cancellation (ADR-006) -------------------------------------------------------


async def _stuck_executing(world: World) -> PendingConfirmation:
    """A confirmation whose executor died after taking the CAS."""
    pending = await world.parked(world.buy())
    await world.approve(pending)
    uow = world.db(write=True)
    async with uow as tx:
        await tx.confirmations.transition(
            pending.id, expected=frozenset({S.APPROVED}), to=S.EXECUTING, at=world.clock.now()
        )
    return world.stored(pending)


async def test_after_a_crash_an_executing_confirmation_is_looked_up_and_never_resent() -> None:
    world = make_world()
    await world.grant()
    pending = await _stuck_executing(world)
    from guarded_exchange.domain.orders import FillStatus, OrderFill  # noqa: PLC0415

    world.trading.fills[f"gx-{pending.id}"] = OrderFill(
        "order-9", FillStatus.FILLED, Decimal("0.001"), Decimal("80600"), Decimal("0.001"), Decimal("80600")
    )

    await world.executor.run_once()  # too early: a live executor may still be sending (D-3)
    assert world.stored(pending).status is S.EXECUTING
    world.clock.advance(timedelta(seconds=31))
    await world.executor.run_once()

    stored = world.stored(pending)
    assert stored.status is S.EXECUTED
    assert j(stored.result)["outcome"]["reconciled"] is True
    assert world.trading.sent == []  # looked up, not resent
    assert world.journal("order_outcome")[-1].actor == "system:executor"


async def test_after_a_crash_an_order_that_is_not_found_is_unknown_not_failed() -> None:
    world = make_world()
    await world.grant()
    pending = await _stuck_executing(world)

    world.clock.advance(timedelta(seconds=31))
    await world.executor.run_once()

    assert world.stored(pending).status is S.UNKNOWN
    assert world.trading.sent == []


async def test_a_cancelled_executor_still_records_the_outcome_of_a_send_in_flight() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)
    world.trading.hold_place = anyio.Event()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(world.executor.run_once)
        await world.trading.placing.wait()
        tasks.cancel_scope.cancel()  # the server shuts down while the request is out
        world.trading.hold_place.set()

    assert world.stored(pending).status is S.EXECUTED
    assert [r.event_type for r in world.journal("order_outcome")] == ["order_outcome"]


async def test_the_exchange_failing_to_answer_is_an_unknown_outcome_of_the_confirmation() -> None:
    from guarded_exchange.domain.errors import OutcomeUnknown  # noqa: PLC0415

    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)
    world.trading.place_error = OutcomeUnknown("no answer", details={"reason": "no_answer"})

    await world.executor.run_once()

    assert world.stored(pending).status is S.UNKNOWN
    assert len(world.trading.sent) == 1


# --- review round 1 ----------------------------------------------------------------------------


def _ticker(world: World, **prices: str) -> None:
    key = ("BTCUSDT", Category.LINEAR)
    changes: dict[str, Any] = {name: Decimal(value) for name, value in prices.items()}
    world.exchange.tickers[key] = replace(world.exchange.tickers[key], **changes)


async def test_a_keyed_repeat_after_execution_does_not_send_again_when_the_rule_no_longer_applies() -> None:
    world = make_world()
    await world.grant(confirm_actions=frozenset(), confirm_above_notional_usdt=Decimal(50))
    pending = await world.parked(world.buy(idempotency_key="k-1"))
    await world.approve(pending)
    await world.executor.run_once()
    await world.grant(confirm_actions=frozenset(), confirm_above_notional_usdt=None)  # the admin relaxes it

    world.clock.advance(timedelta(seconds=5))
    with pytest.raises(ConfirmationPending) as repeat:
        await world.buy(idempotency_key="k-1")

    assert (repeat.value.pending.id, repeat.value.pending.status) == (pending.id, S.EXECUTED)
    assert len(world.trading.sent) == 1


async def test_a_keyed_repeat_while_approved_does_not_send_again_when_the_rule_no_longer_applies() -> None:
    world = make_world()
    await world.grant(confirm_actions=frozenset(), confirm_above_notional_usdt=Decimal(50))
    pending = await world.parked(world.buy(idempotency_key="k-1"))
    await world.approve(pending)
    await world.grant(confirm_actions=frozenset(), confirm_above_notional_usdt=None)

    with pytest.raises(ConfirmationPending):
        await world.buy(idempotency_key="k-1")
    await world.executor.run_once()

    assert len(world.trading.sent) == 1


@pytest.mark.parametrize("key", ["k-1", None])
async def test_a_repeat_while_pending_after_the_price_dropped_below_the_threshold_stays_one_order(
    key: str | None,
) -> None:
    world = make_world()
    await world.grant(confirm_actions=frozenset(), confirm_above_notional_usdt=Decimal(80))
    pending = await world.parked(world.buy(idempotency_key=key))
    _ticker(world, bid_price="77000", ask_price="77000.1", last_price="77000")  # now below the threshold

    with pytest.raises(ConfirmationPending) as repeat:
        await world.buy(idempotency_key=key)
    await world.approve(pending)
    await world.executor.run_once()

    assert repeat.value.pending.id == pending.id
    assert len(world.trading.sent) == 1


async def test_a_keyed_repeat_of_a_rejected_confirmation_says_a_new_attempt_needs_a_new_key() -> None:
    from guarded_exchange.interface.mcp.schemas_confirmation import ConfirmationOut  # noqa: PLC0415

    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy(idempotency_key="k-1"))
    await world.resolve.reject(pending.id)

    with pytest.raises(ConfirmationPending) as repeat:
        await world.buy(idempotency_key="k-1")
    out = ConfirmationOut.from_domain(repeat.value.pending, already_waiting=not repeat.value.created)

    assert out.status == "rejected"
    assert "new idempotency_key" in out.next_step
    assert world.trading.sent == []


@pytest.mark.req("FR-48")
async def test_a_buy_is_held_to_the_shown_price_when_the_fresh_bound_is_worse_but_inside_the_band() -> None:
    world = make_world()
    await world.grant(slippage_pct=Decimal("0.1"))
    pending = await world.parked(world.buy())
    shown = Decimal(j(pending.shown)["order"]["price"])
    await world.approve(pending)

    _ticker(world, ask_price="81000")  # the fresh worst price is about 81081, below the band edge 81747.7
    await world.executor.run_once()

    assert [o.price for o in world.trading.sent] == [shown]


@pytest.mark.req("FR-48")
async def test_a_sell_is_held_to_the_shown_price_when_the_fresh_bound_is_worse_but_inside_the_band() -> None:
    world = make_world()
    await world.grant(slippage_pct=Decimal("0.1"))
    pending = await world.parked(world.buy(side="sell"))
    shown = Decimal(j(pending.shown)["order"]["price"])
    await world.approve(pending)

    _ticker(world, bid_price="80300")  # the fresh worst price is about 80220, above the band edge 80128.9
    await world.executor.run_once()

    assert [o.price for o in world.trading.sent] == [shown]


async def test_a_close_is_refused_when_the_position_flipped_side_while_it_waited() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.close(world.user.id, ClosePositionInput("BTCUSDT", "linear"), CLIENT))
    await world.approve(pending)

    world.hold(side=PositionSide.SHORT)
    await world.executor.run_once()

    stored = world.stored(pending)
    assert stored.status is S.FAILED
    assert j(stored.result)["outcome"]["details"]["reason"] == "position_changed"
    assert world.trading.sent == []


async def test_the_profile_is_checked_again_under_the_lock_not_only_before_the_reads() -> None:
    world = make_world()
    await world.grant()
    pending = await world.parked(world.buy())
    await world.approve(pending)

    def lower_profile() -> None:  # between the early authorisation and the decision transaction
        record = world.db.state.policies[world.user.id]
        world.db.state.policies[world.user.id] = replace(
            record, overrides=replace(record.overrides, profile=Profile.READ_ONLY)
        )

    world.trading.before_band = lower_profile
    await world.executor.run_once()

    stored = world.stored(pending)
    assert stored.status is S.FAILED
    assert j(stored.result)["outcome"]["code"] == "permission_denied"
    assert world.trading.sent == []
