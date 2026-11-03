"""``SubmitPlaceOrder``, review round 2 of ticket 011: a journal failure never hides an unknown
outcome, a committed attempt always reaches the send, exchange reads before the decision are
journaled as refusals (FR-27, FR-36, ADR-008, ADR-014, D-10)."""

import anyio
import pytest
import structlog

from guarded_exchange.domain.errors import (
    ExchangeRejected,
    ExchangeUnavailable,
    InstrumentRuleViolation,
    InvalidParams,
    NotFound,
    OutcomeUnknown,
    PositionNotFound,
    UnsupportedPositionMode,
)
from guarded_exchange.domain.instruments import Category
from tests.fakes import make_rules
from tests.unit.test_submit_action import ORDER_EVENTS, make_world, payload, types

pytestmark = pytest.mark.req("FR-4")


# --- a journal failure after sending must not change the answer ----------------------------------


@pytest.mark.req("FR-27")
async def test_a_journal_failure_after_an_unknown_outcome_still_answers_outcome_unknown() -> None:
    """``internal_error`` instead of ``outcome_unknown`` could make the agent place a second order."""
    world = make_world()
    await world.grant()
    world.trading.place_error = OutcomeUnknown("read timeout", details={"reason": "no_answer"})

    def lock_database() -> None:
        world.db.fail_writes = RuntimeError("database is locked")

    world.trading.during_place = lock_database

    with structlog.testing.capture_logs() as logs, pytest.raises(OutcomeUnknown) as error:
        await world()

    assert error.value.details["client_order_id"] == "gx-act-0001"
    assert error.value.details["reason"] == "no_answer"
    assert any(e["event"] == "order_outcome_not_recorded" and e.get("exc_info") for e in logs)
    assert types(world.journal(*ORDER_EVENTS)) == ["order_attempt"]


@pytest.mark.req("FR-27")
@pytest.mark.parametrize(
    "error",
    [ExchangeUnavailable("could not connect"), ExchangeRejected("insufficient balance")],
    ids=["not_sent", "rejected"],
)
async def test_a_journal_failure_after_a_definite_failure_keeps_the_exchange_error(
    error: Exception,
) -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = error

    def lock_database() -> None:
        world.db.fail_writes = RuntimeError("database is locked")

    world.trading.during_place = lock_database

    with pytest.raises(type(error)):
        await world()


# --- a committed attempt is always followed by the send and the outcome ---------------------------


@pytest.mark.req("FR-27")
async def test_a_cancellation_right_after_the_attempt_commits_still_sends_and_records_the_outcome() -> None:
    """The commit of ``order_attempt`` completes, but its ``await`` is cancelled (client went away)."""
    world = make_world()
    await world.grant()
    world.db.checkpoint = True

    async with anyio.create_task_group() as tasks:
        scope = anyio.CancelScope()

        def cancel_once_attempted() -> None:
            if world.journal("order_attempt"):
                scope.cancel()

        world.db.after_commit = cancel_once_attempted

        async def call() -> None:
            with scope:
                await world()

        tasks.start_soon(call)

    assert len(world.trading.sent) == 1
    assert types(world.journal(*ORDER_EVENTS)) == ["order_attempt", "order_outcome"]


@pytest.mark.req("FR-27")
async def test_a_call_cancelled_before_the_decision_decides_and_sends_nothing() -> None:
    world = make_world()
    await world.grant()
    scope = anyio.CancelScope()
    world.trading.before_band = scope.cancel  # the cancellation is pending before the decision

    with scope:
        await world()

    assert scope.cancelled_caught
    assert world.trading.sent == []
    assert world.journal(*ORDER_EVENTS) == []


# --- exchange reads before the decision are attempts too (FR-36) ---------------------------------


@pytest.mark.req("FR-36")
async def test_an_unavailable_exchange_before_the_decision_is_journaled() -> None:
    world = make_world()
    await world.grant()
    world.exchange.fail_with = ExchangeUnavailable("connect timeout")

    with pytest.raises(ExchangeUnavailable):
        await world()

    [refused] = world.journal(*ORDER_EVENTS)
    assert refused.event_type == "order_refused"
    assert (payload(refused)["code"], payload(refused)["stage"]) == ("exchange_unavailable", "pre_check")
    assert world.trading.sent == []


@pytest.mark.req("FR-36")
async def test_hedge_mode_and_an_unknown_instrument_are_journaled() -> None:
    world = make_world()
    await world.grant()
    world.trading.position_error = UnsupportedPositionMode()

    with pytest.raises(UnsupportedPositionMode):
        await world()
    with pytest.raises(NotFound):
        await world(symbol="ETHUSDT")

    codes = [payload(r)["code"] for r in world.journal("order_refused")]
    assert codes == ["unsupported_position_mode", "not_found"]
    assert all(payload(r)["stage"] == "pre_check" for r in world.journal("order_refused"))


@pytest.mark.req("FR-36")
async def test_input_errors_found_while_reading_and_planning_are_not_journaled() -> None:
    world = make_world()
    await world.grant()
    world.exchange.rules[("BTCUSDC", Category.LINEAR)] = make_rules("BTCUSDC", quote="USDC")

    with pytest.raises(InvalidParams):  # found while reading the market: a USDC-quoted instrument
        await world(symbol="BTCUSDC")
    with pytest.raises(InstrumentRuleViolation):
        await world(order_type="limit", price="80000.05")  # off the 0.10 tick
    with pytest.raises(PositionNotFound):
        await world(side="sell", reduce_only=True)

    assert world.journal(*ORDER_EVENTS) == []
