"""``SubmitPlaceOrder`` repeats on fakes (ticket 012: FR-26, D-20): the same call twice with the same
key, without a key, with different keys; the same key with other arguments; risk-reducing calls; a
repeat during the first call; refusals and transient failures; a stale call whose claim was taken
over; carry-overs from the review of ticket 011."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import anyio
import pytest
import structlog

from guarded_exchange.application.submit_action import OrderResult, ResultSource
from guarded_exchange.domain.account import SymbolPosition
from guarded_exchange.domain.errors import (
    ExchangeUnavailable,
    IdempotencyConflict,
    InvalidParams,
    LimitExceeded,
    OutcomeUnknown,
    UnsupportedPositionMode,
)
from guarded_exchange.domain.idempotency import IN_FLIGHT_GUARD, KeyKind, RecordStatus
from guarded_exchange.domain.instruments import Category
from tests.fakes import make_position, make_ticker
from tests.unit.test_submit_action import ORDER_EVENTS, World, make_world, payload, types

pytestmark = pytest.mark.req("FR-26")

KEY = "0b6f4d7e-1c2a-4f7b-9a1e-5d3c2b1a0f9e"


def place_calls(world: World) -> int:
    return len([c for c in world.trading.calls if c[0] == "place_order"])


def records(world: World) -> dict[tuple[KeyKind, str], RecordStatus]:
    return {(kind, key): r.status for (_, kind, key), r in world.db.state.idempotency.items()}


# --- SPEC 6: the same order twice in a row -----------------------------------------------------


async def test_edge_the_same_key_twice_places_one_order_and_answers_the_first_result() -> None:
    world = make_world()
    await world.grant()

    first = await world(idempotency_key=KEY)
    calls_after_first = len(world.trading.calls) + len(world.exchange.calls)
    second = await world(idempotency_key=KEY)

    assert len(world.trading.sent) == 1
    assert (first.source, second.source) == (ResultSource.PLACED, ResultSource.REPLAY)
    assert (second.order_id, second.client_order_id, second.plan) == (
        first.order_id,
        first.client_order_id,
        first.plan,
    )
    # D-20 (1): a repeat is answered before anything that could refuse or slow it - no exchange call.
    assert len(world.trading.calls) + len(world.exchange.calls) == calls_after_first
    assert types(world.journal(*ORDER_EVENTS)) == ["order_attempt", "order_outcome"]
    assert records(world)[(KeyKind.EXPLICIT, KEY)] is RecordStatus.COMPLETED


async def test_edge_no_key_twice_within_the_window_places_one_order() -> None:
    world = make_world()
    await world.grant()

    first = await world()
    world.clock.advance(timedelta(seconds=29))
    second = await world()

    assert len(world.trading.sent) == 1
    assert second.source is ResultSource.REPLAY
    assert second.order_id == first.order_id


async def test_no_key_after_the_window_is_a_new_order() -> None:
    """The window counts from the first call's answer (30 s, FR-26); an explicit key lives 24 h."""
    world = make_world()
    await world.grant()

    await world()
    await world(idempotency_key=KEY)
    world.clock.advance(timedelta(seconds=31))
    third = await world()
    fourth = await world(idempotency_key=KEY)

    assert len(world.trading.sent) == 3
    assert third.source is ResultSource.PLACED
    assert fourth.source is ResultSource.REPLAY


async def test_edge_different_keys_place_two_orders() -> None:
    """A ladder of identical orders on purpose: one key per order."""
    world = make_world()
    await world.grant()

    first = await world(idempotency_key="ladder-1")
    second = await world(idempotency_key="ladder-2")

    assert len(world.trading.sent) == 2
    assert first.client_order_id != second.client_order_id
    assert {first.source, second.source} == {ResultSource.PLACED}


async def test_edge_the_same_key_with_other_arguments_is_idempotency_conflict() -> None:
    world = make_world()
    await world.grant()
    await world(idempotency_key=KEY)

    with pytest.raises(IdempotencyConflict) as error:
        await world(idempotency_key=KEY, qty="0.002")

    assert len(world.trading.sent) == 1
    assert "new idempotency_key" in (error.value.next_step or "")
    refused = world.journal("order_refused")[-1]
    assert (payload(refused)["code"], payload(refused)["stage"]) == ("idempotency_conflict", "idempotency")
    assert payload(refused)["idempotency"] == {"kind": "explicit", "key": KEY}


async def test_edge_decimal_spellings_are_the_same_call() -> None:
    """``"0.0010"`` and ``"0.001"``: one fingerprint, and no conflict for one key."""
    world = make_world()
    await world.grant()

    await world(qty="0.0010")
    without_key = await world(qty="0.001")
    await world(qty="0.001", idempotency_key=KEY)
    with_key = await world(qty="0.00100", idempotency_key=KEY)

    assert (without_key.source, with_key.source) == (ResultSource.REPLAY, ResultSource.REPLAY)
    assert len(world.trading.sent) == 2


async def test_a_malformed_key_is_invalid_params_before_anything_else() -> None:
    world = make_world()
    await world.grant()

    with pytest.raises(InvalidParams):
        await world(idempotency_key="has space")

    assert world.trading.calls == []
    assert world.db.state.idempotency == {}


# --- D-20 (3): risk-reducing calls are not deduplicated by the window ---------------------------


async def test_risk_reducing_calls_are_not_deduplicated_without_a_key() -> None:
    """``cancel_all``-like calls: repeating a reduce-only order must keep working (D-20 (3)); with an
    explicit key it is still a repeat."""
    world = make_world()
    await world.grant()
    world.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", make_position(), leverage=Decimal(10))
    reduce = {"side": "sell", "reduce_only": True, "qty": "0.004"}

    await world(**reduce)
    await world(**reduce)
    await world(**reduce, idempotency_key=KEY)
    repeat = await world(**reduce, idempotency_key=KEY)

    assert len(world.trading.sent) == 3
    assert repeat.source is ResultSource.REPLAY
    assert set(records(world)) == {(KeyKind.EXPLICIT, KEY)}


# --- a repeat while the first call is still running -------------------------------------------


async def test_a_repeat_during_the_first_call_is_in_progress_and_sends_nothing() -> None:
    world = make_world()
    await world.grant()
    world.trading.hold_place = anyio.Event()
    results: list[OrderResult] = []

    async with anyio.create_task_group() as tasks:

        async def first() -> None:
            results.append(await world(idempotency_key=KEY))

        tasks.start_soon(first)
        await world.trading.placing.wait()  # the first order is on its way
        with pytest.raises(OutcomeUnknown) as error:
            await world(idempotency_key=KEY)
        world.trading.hold_place.set()

    assert error.value.details["reason"] == "in_progress"
    assert error.value.details["client_order_id"] == results[0].client_order_id
    assert "Do not place the order again" in (error.value.next_step or "")
    assert len(world.trading.sent) == 1
    assert (await world(idempotency_key=KEY)).source is ResultSource.REPLAY


async def test_a_stale_call_whose_claim_was_taken_over_sends_nothing() -> None:
    """A fingerprint claim expires: a call stuck before its decision longer than ``IN_FLIGHT_GUARD``
    loses its key to an identical call; its decision transaction sees that and does not send."""
    world = make_world()
    await world.grant()
    release = world.trading.hold_band = anyio.Event()
    outcome: list[BaseException] = []

    async with anyio.create_task_group() as tasks:

        async def stuck() -> None:
            try:
                await world()
            except OutcomeUnknown as exc:
                outcome.append(exc)

        tasks.start_soon(stuck)
        await world.trading.in_band.wait()
        world.clock.advance(IN_FLIGHT_GUARD + timedelta(seconds=1))
        fresh = await world()  # takes the expired key over and places the order
        release.set()

    assert fresh.source is ResultSource.PLACED
    assert len(world.trading.sent) == 1
    [superseded] = outcome
    assert isinstance(superseded, OutcomeUnknown)
    assert superseded.details["reason"] == "superseded"
    assert payload(world.journal("order_refused")[-1])["details"]["reason"] == "superseded"


# --- which answers are kept --------------------------------------------------------------------


async def test_a_refusal_with_a_key_is_answered_again_and_a_new_key_is_a_new_attempt() -> None:
    """D-20 (5): same key, same answer, even after the limit was raised; new attempt, new key."""
    world = make_world()
    await world.grant(max_order_notional_usdt=Decimal(50))
    with pytest.raises(LimitExceeded):
        await world(idempotency_key=KEY)
    await world.grant(max_order_notional_usdt=Decimal(1000))

    with pytest.raises(LimitExceeded) as again:
        await world(idempotency_key=KEY)
    placed = await world(idempotency_key="attempt-2")

    assert again.value.details["replayed"] == "true"
    assert again.value.details["limit"] == "max_order_notional_usdt"
    assert placed.source is ResultSource.PLACED
    assert len(world.trading.sent) == 1


async def test_a_refusal_without_a_key_does_not_block_the_same_call_later() -> None:
    """The window only stops duplicate orders: a refused call leaves nothing to repeat."""
    world = make_world()
    await world.grant(max_order_notional_usdt=Decimal(50))
    with pytest.raises(LimitExceeded):
        await world()
    await world.grant(max_order_notional_usdt=Decimal(1000))

    placed = await world()

    assert placed.source is ResultSource.PLACED
    assert world.db.state.idempotency.keys() == {
        k for k in world.db.state.idempotency if k[1] is KeyKind.FINGERPRINT
    }


async def test_a_transient_failure_releases_the_key_so_the_retry_runs() -> None:
    """Exchange unavailable before the decision: the agent was told to retry; the same key works."""
    world = make_world()
    await world.grant()
    world.exchange.fail_with = ExchangeUnavailable("connect timeout")
    with pytest.raises(ExchangeUnavailable):
        await world(idempotency_key=KEY)
    assert world.db.state.idempotency == {}
    world.exchange.fail_with = None

    placed = await world(idempotency_key=KEY)

    assert placed.source is ResultSource.PLACED
    assert len(world.trading.sent) == 1


async def test_a_definite_exchange_failure_after_sending_releases_or_keeps_by_kind() -> None:
    """``not_sent`` releases the key; the order did not reach the exchange."""
    world = make_world()
    await world.grant()
    world.trading.place_error = ExchangeUnavailable("could not connect")
    with pytest.raises(ExchangeUnavailable):
        await world(idempotency_key=KEY)
    world.trading.place_error = None

    placed = await world(idempotency_key=KEY)

    assert placed.source is ResultSource.PLACED
    assert len(world.trading.sent) == 2  # the first never left; the retry is the only order


@pytest.mark.req("FR-27")
async def test_an_unknown_outcome_is_kept_and_a_repeat_never_sends_again() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = OutcomeUnknown("read timeout", details={"reason": "no_answer"})
    with pytest.raises(OutcomeUnknown):
        await world(idempotency_key=KEY)
    world.trading.place_error = None

    with pytest.raises(OutcomeUnknown) as again:
        await world(idempotency_key=KEY)
    world.trading.place_error = OutcomeUnknown("read timeout", details={"reason": "no_answer"})
    with pytest.raises(OutcomeUnknown):
        await world(qty="0.002")  # without a key: the fingerprint keeps an unknown outcome too
    world.trading.place_error = None
    world.clock.advance(timedelta(seconds=20))
    with pytest.raises(OutcomeUnknown) as unkeyed:
        await world(qty="0.002")

    assert again.value.details["replayed"] == "true"
    assert again.value.details["client_order_id"] == "gx-act-0001"
    assert unkeyed.value.details["replayed"] == "true"
    assert len(world.trading.sent) == 2  # one per call that was not a repeat; nothing resent
    statuses = set(records(world).values())
    assert statuses == {RecordStatus.UNKNOWN}


# --- carry-overs from the review of ticket 011 ----------------------------------------------


@pytest.mark.req("FR-27", "FR-36")
async def test_a_journal_failure_after_an_accepted_order_leaves_the_key_claimed() -> None:
    """Accepted, but ``order_outcome`` cannot be written: the agent gets "accepted", and a repeat is
    told "in progress" instead of placing a second order (011 review)."""
    world = make_world()
    await world.grant()

    def lock_database() -> None:
        world.db.fail_writes = RuntimeError("database is locked")

    world.trading.after_place = lock_database

    first = await world(idempotency_key=KEY)
    world.db.fail_writes = None
    with pytest.raises(OutcomeUnknown) as repeat:
        await world(idempotency_key=KEY)

    assert first.source is ResultSource.PLACED
    assert repeat.value.details["reason"] == "in_progress"
    assert len(world.trading.sent) == 1
    assert set(records(world).values()) == {RecordStatus.SENT}  # the key and its fingerprint


@pytest.mark.req("FR-36")
async def test_a_market_order_without_a_best_price_is_journaled_as_a_refusal() -> None:
    world = make_world()
    await world.grant()
    world.exchange.tickers[("BTCUSDT", Category.LINEAR)] = replace(make_ticker(), ask_price=None)

    with pytest.raises(ExchangeUnavailable) as error:
        await world()

    assert error.value.details["reason"] == "no_market_price"
    [refused] = world.journal(*ORDER_EVENTS)
    assert (payload(refused)["code"], payload(refused)["stage"]) == ("exchange_unavailable", "pre_check")
    assert world.db.state.idempotency == {}


@pytest.mark.req("FR-36")
async def test_a_journal_failure_of_an_early_refusal_keeps_the_original_error() -> None:
    world = make_world()
    await world.grant()

    def lock_database() -> None:
        world.db.fail_writes = RuntimeError("database is locked")

    world.trading.before_band = lock_database
    world.trading.position_error = UnsupportedPositionMode()

    with structlog.testing.capture_logs() as logs, pytest.raises(UnsupportedPositionMode):
        await world()

    assert any(e["event"] == "order_refused_not_recorded" for e in logs)
    assert world.trading.sent == []


async def test_a_call_cancelled_before_its_decision_releases_its_key() -> None:
    world = make_world()
    await world.grant()
    scope = anyio.CancelScope()
    world.trading.before_band = scope.cancel  # the client went away while the market was read

    with scope:
        await world(idempotency_key=KEY)

    assert scope.cancelled_caught
    assert world.db.state.idempotency == {}
    world.trading.before_band = None
    assert (await world(idempotency_key=KEY)).source is ResultSource.PLACED
