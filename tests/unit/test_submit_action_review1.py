"""``SubmitPlaceOrder``, review round 1 of ticket 011: D-8 re-checks inside the decision, early
refusals in the journal, the shielded send, failures after an accepted order (FR-27, FR-29, FR-36,
FR-11, D-8, D-15)."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import anyio
import pytest
import structlog

from guarded_exchange.application.submit_action import PlaceOrderInput
from guarded_exchange.domain.account import SymbolPosition
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT, Credential, CredentialStatus
from guarded_exchange.domain.errors import (
    ConfirmationChannelUnavailable,
    ExchangeCredentialsInvalid,
    ExchangeUnavailable,
    InvalidParams,
    PermissionDenied,
    RateLimited,
)
from guarded_exchange.domain.policy import Profile
from guarded_exchange.domain.users import Environment, UserStatus
from tests.fakes import make_position
from tests.unit.test_submit_action import (
    ORDER_EVENTS,
    TRADING_KEY,
    World,
    assert_decided_under_lock,
    make_world,
    payload,
    types,
)

pytestmark = pytest.mark.req("FR-4")

MARKET_BUY = PlaceOrderInput(
    symbol="BTCUSDT", category="linear", side="buy", order_type="market", qty="0.001"
)


def _refusal(world: World) -> dict[str, object]:
    [refused] = world.journal(*ORDER_EVENTS)
    assert refused.event_type == "order_refused"
    return payload(refused)


# --- D-8: what changed between the checks and the decision ---------------------------------------


@pytest.mark.req("FR-29", "FR-36")
async def test_a_key_replaced_by_a_new_one_after_the_key_check_is_refused() -> None:
    """``keys replace``: the old key is soft-deleted and a new active key with another id appears."""
    world = make_world()
    await world.grant()

    def replace_key() -> None:
        [(old_id, (old, sealed))] = world.db.state.credentials.items()
        world.db.state.credentials[old_id] = (replace(old, status=CredentialStatus.DELETED), sealed)
        new = Credential(
            id="cred-new",
            user_id=world.user.id,
            exchange=EXCHANGE_BYBIT,
            environment=Environment.TESTNET,
            status=CredentialStatus.ACTIVE,
            permissions=TRADING_KEY,
            permissions_checked_at=world.clock.now(),
            admin_attested=False,
            created_at=world.clock.now(),
        )
        world.db.state.credentials[new.id] = (new, sealed)

    world.trading.before_band = replace_key

    with pytest.raises(ExchangeCredentialsInvalid) as error:
        await world()

    assert error.value.details == {"reason": "key_replaced_or_deleted"}
    assert world.trading.sent == []
    assert _refusal(world)["code"] == "exchange_credentials_invalid"
    assert_decided_under_lock(world)


@pytest.mark.req("FR-29", "FR-11")
async def test_access_revoked_after_the_first_check_is_refused_in_the_decision() -> None:
    world = make_world()
    await world.grant()

    def revoke() -> None:
        world.db.state.users[world.user.id] = replace(world.user, status=UserStatus.REVOKED)

    world.trading.before_band = revoke

    with pytest.raises(PermissionDenied):
        await world()

    assert world.trading.sent == []
    assert _refusal(world)["code"] == "permission_denied"
    assert_decided_under_lock(world)


@pytest.mark.req("FR-11", "FR-12")
async def test_a_profile_lowered_to_read_only_after_the_first_check_is_refused_in_the_decision() -> None:
    world = make_world()
    await world.grant()

    def lower_profile() -> None:
        record = world.db.state.policies[world.user.id]
        assert record.ceiling is not None
        lowered = replace(record.ceiling, profile=Profile.READ_ONLY)
        world.db.state.policies[world.user.id] = replace(record, ceiling=lowered)

    world.trading.before_band = lower_profile

    with pytest.raises(PermissionDenied) as error:
        await world()

    assert error.value.details["profile"] == "read_only"
    assert world.trading.sent == []
    assert_decided_under_lock(world)


# --- early refusals are attempts too (FR-36) -----------------------------------------------------


@pytest.mark.req("FR-36", "FR-11")
async def test_a_read_only_user_is_refused_and_the_attempt_is_journaled() -> None:
    world = make_world()

    with pytest.raises(PermissionDenied):
        await world()

    refused = _refusal(world)
    assert (refused["code"], refused["stage"]) == ("permission_denied", "pre_check")
    assert refused["client"] == {"name": "claude-code", "version": "2.1.312"}
    assert world.key_reader.calls == []


@pytest.mark.req("FR-36")
async def test_an_environment_mismatch_is_journaled() -> None:
    world = make_world(environment=Environment.MAINNET)
    await world.grant()

    with pytest.raises(PermissionDenied):
        await world()

    assert _refusal(world)["details"] == {"reason": "environment_mismatch"}


@pytest.mark.req("FR-36", "FR-49")
async def test_a_refusal_of_the_key_check_is_journaled() -> None:
    world = make_world()
    await world.grant()
    world.clock.advance(timedelta(hours=2))
    world.key_reader.fail_with = RateLimited("slow down", retry_after_s=3.0)

    with pytest.raises(RateLimited):
        await world()

    assert _refusal(world)["code"] == "rate_limited"


@pytest.mark.req("FR-36")
async def test_invalid_input_and_an_unknown_caller_are_not_journaled() -> None:
    world = make_world()
    await world.grant()

    with pytest.raises(InvalidParams):
        await world(qty="1e3")
    with pytest.raises(PermissionDenied):
        await world.place("user-ghost", MARKET_BUY)

    assert world.journal(*ORDER_EVENTS) == []


# --- sending -------------------------------------------------------------------------------------


@pytest.mark.req("FR-27")
async def test_a_cancelled_call_still_records_the_outcome_of_a_sent_order() -> None:
    """The client goes away while the exchange is answering: the shielded send finishes and journals."""
    world = make_world()
    await world.grant()
    world.db.checkpoint = True  # every transaction is a cancellation point, as with a real driver
    world.trading.hold_place = anyio.Event()

    async with anyio.create_task_group() as tasks:
        with anyio.CancelScope() as scope:
            tasks.start_soon(world.__call__)
            await world.trading.placing.wait()
            scope.cancel()
        tasks.cancel_scope.cancel()
        world.trading.hold_place.set()

    assert len(world.trading.sent) == 1
    assert types(world.journal(*ORDER_EVENTS)) == ["order_attempt", "order_outcome"]


@pytest.mark.req("FR-27")
async def test_a_journal_failure_after_an_accepted_order_still_answers_accepted() -> None:
    world = make_world()
    await world.grant()

    def lock_database() -> None:
        world.db.fail_writes = RuntimeError("database is locked")

    world.trading.after_place = lock_database

    with structlog.testing.capture_logs() as logs:
        result = await world()

    assert result.order_id == "order-1"
    assert result.fill is None  # the agent is told: filled unknown, do not place it again
    assert result.client_order_id == "gx-act-0001"
    assert any(e["event"] == "order_outcome_not_recorded" and e.get("exc_info") for e in logs)
    assert types(world.journal(*ORDER_EVENTS)) == ["order_attempt"]


@pytest.mark.req("FR-27")
async def test_a_connection_failure_before_sending_is_journaled_as_not_sent() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = ExchangeUnavailable("could not connect")

    with pytest.raises(ExchangeUnavailable):
        await world()

    assert payload(world.journal("order_outcome")[0])["status"] == "not_sent"


@pytest.mark.req("FR-43")
async def test_a_reduce_only_order_travels_with_the_flag() -> None:
    world = make_world()
    await world.grant()
    world.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", make_position(), leverage=Decimal(10))

    await world(side="sell", qty="0.005", reduce_only=True)

    [sent] = world.trading.sent
    assert sent.reduce_only is True


@pytest.mark.req("FR-19", "FR-13")
async def test_a_market_sell_needs_confirmation_by_its_risk_sum_not_its_order_value() -> None:
    """Bid 80571.1; the IOC bound is the band's sell_min 80128.9: order value 80.1289 USDT, risk sum
    80.5711 USDT. A threshold of 80.3 sits between them: confirmation is required."""
    world = make_world()
    await world.grant(confirm_above_notional_usdt=Decimal("80.3"))

    with pytest.raises(ConfirmationChannelUnavailable):
        await world(side="sell")

    assert world.trading.sent == []
    await world.grant(confirm_above_notional_usdt=Decimal("80.6"))
    result = await world(side="sell")
    assert (result.plan.notional, result.plan.order_value) == (Decimal("80.5711"), Decimal("80.1289"))
    attempt = payload(world.journal("order_attempt")[0])["order"]
    assert (attempt["notional_usdt"], attempt["notional_price"]) == ("80.5711", "80571.1")
