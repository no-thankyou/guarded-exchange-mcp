"""``SubmitPlaceOrder`` on fakes: the single path auth -> key -> rules -> policy/limits -> confirmation
stub -> send, and what the journal records in which transaction (ticket 011; FR-13 - FR-17, FR-25,
FR-36, FR-43, D-8, D-10, D-15)."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from guarded_exchange.application.credentials import RecheckCredential, RequireTradingCredential
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.application.submit_action import OrderResult, PlaceOrderInput, SubmitPlaceOrder
from guarded_exchange.domain.account import PositionSide, SymbolPosition
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.audit import AuditRecord, ClientDescription
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT, ApiKeyPair, CredentialStatus, KeyPermissions
from guarded_exchange.domain.errors import (
    ConfirmationChannelUnavailable,
    DomainError,
    ExchangeCredentialsInvalid,
    ExchangeRejected,
    ExchangeUnavailable,
    InstrumentRuleViolation,
    LimitExceeded,
    OutcomeUnknown,
    PermissionDenied,
    RateLimited,
)
from guarded_exchange.domain.instruments import Category, InstrumentStatus
from guarded_exchange.domain.orders import FillStatus, OrderFill, OrderTimeInForce, QtyAdjustment
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from guarded_exchange.domain.users import Environment, User
from tests.fakes import (
    LISTED_BY_DEFAULT,
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

pytestmark = pytest.mark.req("FR-4")

KEYS = ApiKeyPair(api_key="TRADEKEY01234567", api_secret="trade-secret-abcdefghijklmnopqrstuv")  # noqa: S106
TRADING_KEY = KeyPermissions(
    read_only=False,
    permissions=frozenset({"ContractTrade.Order", "ContractTrade.Position", "Spot.SpotTrade"}),
    expires_at=None,
    ip_restricted=True,
)
BTC_PERP = InstrumentRef(Category.LINEAR, "BTCUSDT")
CLIENT = ClientDescription.sanitized("claude-code", "2.1.312")
LINEAR, SPOT = Category.LINEAR, Category.SPOT


@dataclass
class World:
    db: InMemoryDatabase
    clock: FakeClock
    exchange: FakeExchange
    trading: FakeTrading
    key_reader: FakeKeyReader
    user: User
    place: SubmitPlaceOrder

    async def grant(self, **changes: Any) -> None:
        patch = {
            "profile": Profile.TRADE,
            "allowlist": frozenset({BTC_PERP}),
            "confirm_actions": frozenset(),
            **changes,
        }
        await SetCeiling(self.db, self.clock)(self.user.name, PolicyPatch(**patch))

    async def __call__(self, **kwargs: Any) -> OrderResult:
        args: dict[str, Any] = {
            "symbol": "BTCUSDT",
            "category": "linear",
            "side": "buy",
            "order_type": "market",
            "qty": "0.001",
            **kwargs,
        }
        return await self.place(self.user.id, PlaceOrderInput(**args), CLIENT)

    def journal(self, *types: str) -> list[AuditRecord]:
        records = self.db.state.audit.get(self.user.id, [])
        return [r for r in records if not types or r.event_type in types]


def make_world(*, environment: Environment = Environment.TESTNET) -> World:
    db, clock, cipher = InMemoryDatabase(), FakeClock(), FakeCipher()
    user = db.add_user(environment=environment)
    db.add_credential(user, cipher, KEYS, permissions=TRADING_KEY)
    exchange = FakeExchange(
        tickers={("BTCUSDT", LINEAR): make_ticker(), ("BTCUSDT", SPOT): make_ticker(category=SPOT)},
        rules={("BTCUSDT", LINEAR): make_rules(), ("BTCUSDT", SPOT): make_rules(category=SPOT)},
    )
    trading, reader = FakeTrading(db=db), FakeKeyReader(permissions=TRADING_KEY)
    scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
    recheck = RecheckCredential(db, cipher, reader, clock)
    place = SubmitPlaceOrder(
        db,
        RequireTradingCredential(db, cipher, recheck, clock, **scope),
        InstrumentRulesCache(exchange, clock),
        market=exchange,
        trading=trading,
        clock=clock,
        ids=SequentialIds("act"),
        **scope,
    )
    return World(db, clock, exchange, trading, reader, user, place)


def payload(record: AuditRecord) -> dict[str, Any]:
    return dict(record.payload)


def types(records: Sequence[AuditRecord]) -> list[str]:
    return [r.event_type for r in records]


ORDER_EVENTS = ("order_refused", "order_attempt", "order_outcome")


# --- confirmation stub (FR-19, FR-25) ------------------------------------------------------------


@pytest.mark.req("FR-25", "FR-19", "NFR-1")
async def test_policy_requiring_confirmation_refuses_and_nothing_reaches_the_exchange() -> None:
    world = make_world()
    await world.grant(confirm_actions=frozenset({ActionKind.PLACE_ORDER}))

    with pytest.raises(ConfirmationChannelUnavailable) as error:
        await world()

    assert error.value.code == "confirmation_channel_unavailable"
    assert "Do not retry" in (error.value.next_step or "")
    assert world.trading.sent == []
    [refused] = world.journal(*ORDER_EVENTS)
    assert refused.event_type == "order_refused"
    assert payload(refused)["code"] == "confirmation_channel_unavailable"


@pytest.mark.req("FR-25", "FR-19")
async def test_an_order_at_the_confirmation_threshold_is_refused_too() -> None:
    world = make_world()
    await world.grant(confirm_above_notional_usdt=Decimal(50))

    with pytest.raises(ConfirmationChannelUnavailable):
        await world(qty="0.001")  # 0.001 x ~81747 = 81.7 USDT >= 50

    assert world.trading.sent == []


@pytest.mark.req("FR-25", "FR-45")
async def test_the_default_policy_of_a_trading_user_confirms_everything() -> None:
    world = make_world()
    await SetCeiling(world.db, world.clock)(
        world.user.name, PolicyPatch(profile=Profile.TRADE, allowlist=frozenset({BTC_PERP}))
    )

    with pytest.raises(ConfirmationChannelUnavailable):
        await world()
    assert world.trading.sent == []


# --- the happy path and the journal transactions (ADR-008, D-10, D-15, FR-36) --------------------


@pytest.mark.req("FR-36", "FR-48", "NFR-2")
async def test_a_market_order_is_sent_as_ioc_at_the_bound_with_attempt_before_and_outcome_after() -> None:
    world = make_world()
    await world.grant()
    world.trading.fills["gx-act-0001"] = OrderFill(
        order_id="order-1",
        status=FillStatus.FILLED,
        qty=Decimal("0.001"),
        price=Decimal("81747.7"),
        filled_qty=Decimal("0.001"),
        avg_fill_price=Decimal("80571.2"),
    )

    result = await world()

    [sent] = world.trading.sent
    assert sent.time_in_force is OrderTimeInForce.IOC
    # ask 80571.2 x 1.02 = 82182.6 (down to the tick), kept inside the band max 81747.7
    assert (result.plan.worst_price, sent.price) == (Decimal("82182.6"), Decimal("81747.7"))
    assert sent.client_order_id == result.client_order_id == "gx-act-0001"
    assert result.fill is not None
    assert result.fill.filled_qty == Decimal("0.001")
    assert result.price == Decimal("81747.7")
    # the attempt was committed before the request left; the outcome came after (ADR-008)
    assert world.trading.audit_at_send[0][-1] == "order_attempt"
    assert types(world.journal(*ORDER_EVENTS)) == ["order_attempt", "order_outcome"]
    assert world.trading.calls_in_transaction == 0  # D-10: no exchange call inside a transaction
    attempt, outcome = world.journal(*ORDER_EVENTS)
    assert payload(attempt)["client"] == {"name": "claude-code", "version": "2.1.312"}
    assert payload(attempt)["order"]["price"] == "81747.7"
    assert payload(attempt)["order"]["time_in_force"] == "ioc"
    assert payload(attempt)["decision"] == "allowed_without_confirmation"
    assert payload(outcome)["status"] == "accepted"
    assert payload(outcome)["fill"]["status"] == "filled"
    assert attempt.actor == "mcp:agent"


@pytest.mark.req("FR-48")
async def test_execution_not_visible_yet_is_unknown_not_zero() -> None:
    world = make_world()
    await world.grant()

    result = await world()

    assert result.fill is None
    assert result.price == result.plan.price  # what was sent, until the order is visible
    assert payload(world.journal("order_outcome")[0])["fill"] == "unknown"


async def test_a_failed_read_after_placing_is_not_a_failed_order() -> None:
    world = make_world()
    await world.grant()
    world.trading.read_error = ExchangeUnavailable("down")

    result = await world()

    assert result.order_id == "order-1"
    assert result.fill is None


# --- refusals: journaled when the policy decides (FR-17) -----------------------------------------


@pytest.mark.req("FR-13", "FR-17")
async def test_a_limit_refusal_is_journaled_with_the_limit_and_nothing_is_sent() -> None:
    world = make_world()
    await world.grant(max_order_notional_usdt=Decimal(50))

    with pytest.raises(LimitExceeded) as error:
        await world()

    assert error.value.details["limit"] == "max_order_notional_usdt"
    assert world.trading.sent == []
    [refused] = world.journal(*ORDER_EVENTS)
    assert payload(refused)["code"] == "limit_exceeded"
    assert payload(refused)["details"]["allowed"] == "50"
    assert payload(refused)["request"]["qty"] == "0.001"


@pytest.mark.req("FR-15", "FR-17")
async def test_an_instrument_outside_the_allowlist_is_refused() -> None:
    world = make_world()
    await world.grant()

    with pytest.raises(LimitExceeded) as error:
        await world(category="spot")

    assert error.value.details == {"limit": "allowlist", "instrument": "spot:BTCUSDT"}
    assert world.trading.sent == []


@pytest.mark.req("FR-10")
async def test_a_rule_violation_is_an_input_error_answered_before_any_decision() -> None:
    world = make_world()
    await world.grant()

    with pytest.raises(InstrumentRuleViolation):
        await world(order_type="limit", price="80000.05")

    assert world.trading.sent == []
    assert world.journal(*ORDER_EVENTS) == []


@pytest.mark.req("FR-11")
async def test_a_read_only_user_is_refused_before_the_key_is_even_checked() -> None:
    world = make_world()
    world.clock.advance(timedelta(hours=2))  # a stale key would be re-checked

    with pytest.raises(PermissionDenied):
        await world()

    assert world.key_reader.calls == []
    assert world.trading.calls == []


@pytest.mark.req("FR-32")
async def test_trading_is_testnet_only_in_v1() -> None:
    world = make_world(environment=Environment.MAINNET)
    await world.grant()

    with pytest.raises(PermissionDenied) as error:
        await world()
    assert error.value.details["reason"] == "environment_mismatch"  # this server is a testnet server


# --- re-checks under lock_user (D-8) ------------------------------------------------------------


@pytest.mark.req("FR-29", "FR-36")
async def test_a_key_replaced_after_the_key_check_is_caught_in_the_decision() -> None:
    world = make_world()
    await world.grant()

    def delete_key() -> None:
        for cred_id, (cred, sealed) in list(world.db.state.credentials.items()):
            world.db.state.credentials[cred_id] = (replace(cred, status=CredentialStatus.DELETED), sealed)

    world.trading.before_band = delete_key

    with pytest.raises(ExchangeCredentialsInvalid) as error:
        await world()

    assert error.value.details == {"reason": "key_replaced_or_deleted"}
    assert world.trading.sent == []
    assert world.db.locked_users[-1] == world.user.id  # decided under lock_user
    assert payload(world.journal("order_refused")[0])["code"] == "exchange_credentials_invalid"


@pytest.mark.req("FR-12", "FR-15")
async def test_a_policy_tightened_while_reading_the_market_applies_to_the_decision() -> None:
    world = make_world()
    await world.grant()

    def remove_from_allowlist() -> None:
        record = world.db.state.policies[world.user.id]
        assert record.ceiling is not None
        world.db.state.policies[world.user.id] = replace(
            record, ceiling=replace(record.ceiling, allowlist=frozenset())
        )

    world.trading.before_band = remove_from_allowlist

    with pytest.raises(LimitExceeded):
        await world()
    assert world.trading.sent == []


@pytest.mark.req("FR-49")
async def test_a_stale_key_whose_recheck_hits_the_rate_limit_is_refused_with_retry_after() -> None:
    world = make_world()
    await world.grant()
    world.clock.advance(timedelta(hours=2))
    world.key_reader.fail_with = RateLimited("slow down", retry_after_s=3.0)

    with pytest.raises(RateLimited) as error:
        await world()

    assert error.value.retry_after_s == 3.0
    assert error.value.details == {"reason": "permissions_recheck_failed"}
    assert world.trading.calls == []


# --- exchange answers (FR-27, FR-28) -------------------------------------------------------------


async def test_a_definite_rejection_is_journaled_as_rejected() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = ExchangeRejected("no money", details={"exchange_code": "110007"})

    with pytest.raises(ExchangeRejected):
        await world()

    outcome = payload(world.journal("order_outcome")[0])
    assert (outcome["status"], outcome["code"], outcome["details"]["exchange_code"]) == (
        "rejected",
        "exchange_rejected",
        "110007",
    )


@pytest.mark.req("FR-27")
async def test_an_unknown_outcome_is_journaled_and_the_agent_is_told_not_to_repeat() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = OutcomeUnknown("timeout", details={"reason": "no_answer"})

    with pytest.raises(OutcomeUnknown) as error:
        await world()

    assert error.value.details["client_order_id"] == "gx-act-0001"
    assert "Do not place it again" in (error.value.next_step or "")
    assert payload(world.journal("order_outcome")[0])["status"] == "unknown"
    assert len(world.trading.sent) == 1  # never retried


@pytest.mark.req("FR-27")
async def test_a_crash_while_sending_still_records_an_unknown_outcome() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await world()

    outcome = payload(world.journal("order_outcome")[0])
    assert (outcome["status"], outcome["error_type"]) == ("unknown", "RuntimeError")


# --- reduce-only (FR-15, FR-43) -------------------------------------------------------------------


@pytest.mark.req("FR-43", "FR-15")
async def test_reduce_only_on_a_delisting_perpetual_outside_the_allowlist_closes_up_to_the_position() -> None:
    world = make_world()
    await world.grant(allowlist=frozenset())
    world.exchange.listed_statuses = LISTED_BY_DEFAULT  # like Bybit: not found without `status`
    world.exchange.rules[("BTCUSDT", LINEAR)] = make_rules(status=InstrumentStatus.SETTLING)
    long = make_position()  # 0.010 BTC long
    world.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", long, leverage=Decimal(50))

    result = await world(side="sell", qty="1", reduce_only=True)

    [sent] = world.trading.sent
    assert (sent.qty, sent.reduce_only) == (Decimal("0.010"), True)
    assert result.plan.qty_adjustment is QtyAdjustment.CLAMPED_TO_POSITION
    assert ("get_instrument_rules", "BTCUSDT", LINEAR, "any_status") in world.exchange.calls
    assert long.side is PositionSide.LONG


@pytest.mark.req("FR-43")
async def test_reduce_only_without_a_position_is_refused_before_any_decision() -> None:
    world = make_world()
    await world.grant()

    with pytest.raises(DomainError) as error:
        await world(side="sell", reduce_only=True)

    assert error.value.code == "position_not_found"
    assert world.trading.sent == []
    assert world.journal(*ORDER_EVENTS) == []  # an input error, like a rule violation
