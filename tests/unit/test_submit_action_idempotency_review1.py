"""Ticket 012, review round 1: expiry fencing, keyed calls also holding the fingerprint, answers by
record kind, a bug while sending, bounded read-back, stale records, cancellation and failures around
the claim, the fake's writer lock, ``order_repeat`` in the journal (FR-26, FR-27, FR-36)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anyio
import anyio.lowlevel
import pytest
import structlog

from guarded_exchange.application.submit_action import PlaceOrderInput, ResultSource
from guarded_exchange.domain.account import SymbolPosition
from guarded_exchange.domain.errors import OutcomeUnknown
from guarded_exchange.domain.idempotency import IN_FLIGHT_GUARD, KeyKind, RecordStatus, new_claim
from tests.fakes import InMemoryDatabase, call_with_watchdog, make_position
from tests.unit.test_submit_action import ORDER_EVENTS, World, make_world, payload, types
from tests.unit.test_submit_action_idempotency import KEY, records

pytestmark = pytest.mark.req("FR-26")

TIMEOUT = OutcomeUnknown("no reliable answer from the exchange: ReadTimeout", details={"reason": "no_answer"})
MARKET_BUY = PlaceOrderInput(
    symbol="BTCUSDT", category="linear", side="buy", order_type="market", qty="0.001"
)
AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def lock_database_on(world: World) -> None:
    world.db.fail_writes = RuntimeError("database is locked")


# --- 2: expiry fencing --------------------------------------------------------------------------


async def test_a_claim_that_expired_during_the_reads_sends_nothing_and_the_next_call_sends_once() -> None:
    world = make_world()
    await world.grant()
    release = world.trading.hold_band = anyio.Event()
    errors: list[OutcomeUnknown] = []

    async def stuck() -> None:
        try:
            await world()
        except OutcomeUnknown as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(stuck)
        await world.trading.in_band.wait()
        world.clock.advance(IN_FLIGHT_GUARD + timedelta(seconds=1))  # nobody took the key over
        release.set()
    placed = await world()  # the identical call after it

    [superseded] = errors
    assert superseded.details["reason"] == "superseded"
    assert "was never sent" in superseded.message
    assert placed.source is ResultSource.PLACED
    assert len(world.trading.sent) == 1


async def test_sending_extends_the_claim_so_a_slow_send_is_not_taken_over() -> None:
    """Decided a second before the claim would expire; the send still runs two minutes later."""
    world = make_world()
    await world.grant()
    world.trading.hold_place = anyio.Event()
    release = world.trading.hold_band = anyio.Event()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(world.place, world.user.id, MARKET_BUY, None)
        await world.trading.in_band.wait()
        world.clock.advance(IN_FLIGHT_GUARD - timedelta(seconds=1))
        release.set()
        await world.trading.placing.wait()
        world.clock.advance(timedelta(minutes=2))
        with pytest.raises(OutcomeUnknown) as repeat:
            await world()
        world.trading.hold_place.set()

    assert repeat.value.details["reason"] == "in_progress"
    assert len(world.trading.sent) == 1


async def test_housekeeping_keeps_an_expired_unanswered_record() -> None:
    """Its holder may still be in flight; only a takeover (fenced) may replace it."""
    db = InMemoryDatabase()
    user = db.add_user()
    sent = new_claim(
        user_id=user.id,
        kind=KeyKind.FINGERPRINT,
        key="a" * 64,
        request_hash="a" * 64,
        action_id="a-1",
        now=AT,
    )
    async with db(write=True) as uow:
        await uow.idempotency.claim(sent, AT)
        assert await uow.idempotency.mark_sent(sent, AT)
    later = AT + 2 * IN_FLIGHT_GUARD
    other = new_claim(
        user_id=user.id, kind=KeyKind.EXPLICIT, key="k", request_hash="b" * 64, action_id="a-2", now=later
    )
    async with db(write=True) as uow:
        await uow.idempotency.claim(other, later)

    assert db.state.idempotency[(user.id, KeyKind.FINGERPRINT, "a" * 64)].status is RecordStatus.SENT


# --- design decision: a keyed call also holds the fingerprint ------------------------------------


async def test_a_keyed_call_then_the_same_call_without_a_key_places_one_order() -> None:
    """Client A passes a key, client B repeats without one within the window: one order."""
    world = make_world()
    await world.grant()

    first = await world(idempotency_key=KEY)
    second = await world()

    assert second.source is ResultSource.REPLAY
    assert second.order_id == first.order_id
    assert len(world.trading.sent) == 1
    assert set(records(world)) == {(KeyKind.EXPLICIT, KEY), (KeyKind.FINGERPRINT, records_key(world))}


def records_key(world: World) -> str:
    return next(key for (kind, key) in records(world) if kind is KeyKind.FINGERPRINT)


async def test_keyed_calls_never_consult_the_fingerprint() -> None:
    """A ladder of identical orders with different keys stays possible, after a keyless one too."""
    world = make_world()
    await world.grant()

    await world()
    await world(idempotency_key="ladder-1")
    await world(idempotency_key="ladder-2")

    assert len(world.trading.sent) == 3


# --- 3: what an unknown outcome tells the agent, by record kind -----------------------------------


@pytest.mark.req("FR-27")
async def test_an_unknown_outcome_warns_a_keyless_caller_that_a_later_repeat_is_a_new_order() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = TIMEOUT

    with pytest.raises(OutcomeUnknown) as keyless:
        await world()
    with pytest.raises(OutcomeUnknown) as keyed:
        await world(qty="0.002", idempotency_key=KEY)
    world.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", make_position(), leverage=Decimal(10))
    with pytest.raises(OutcomeUnknown) as reducing:  # reduce-only: no record at all
        await world(side="sell", qty="0.001", reduce_only=True)

    assert "places a NEW order" in (keyless.value.next_step or "")
    assert "same idempotency_key" not in (keyless.value.next_step or "")
    assert "same idempotency_key returns this answer" in (keyed.value.next_step or "")
    assert "places a NEW order" in (reducing.value.next_step or "")


@pytest.mark.req("FR-27")
async def test_a_keyless_unknown_outcome_is_kept_ten_minutes_not_thirty_seconds() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = TIMEOUT
    with pytest.raises(OutcomeUnknown):
        await world()
    world.trading.place_error = None

    world.clock.advance(timedelta(minutes=5))
    with pytest.raises(OutcomeUnknown) as repeat:
        await world()
    world.clock.advance(IN_FLIGHT_GUARD)
    later = await world()

    assert repeat.value.details["replayed"] == "true"
    assert later.source is ResultSource.PLACED
    assert len(world.trading.sent) == 2


# --- 4: a bug while sending ------------------------------------------------------------------------


@pytest.mark.req("FR-27")
async def test_a_bug_while_sending_is_outcome_unknown_and_repeats_send_nothing() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = RuntimeError("bug in the adapter")

    with structlog.testing.capture_logs() as logs, pytest.raises(OutcomeUnknown) as error:
        await world(idempotency_key=KEY)
    world.trading.place_error = None
    with pytest.raises(OutcomeUnknown):
        await world(idempotency_key=KEY)
    with pytest.raises(OutcomeUnknown):
        await world()  # keyless, from another client: the keyed call held the fingerprint too

    assert error.value.details["reason"] == "internal_error"
    assert error.value.details["client_order_id"] == "gx-act-0001"
    assert any(e["event"] == "order_send_failed" and e.get("exc_info") for e in logs)
    assert set(records(world).values()) == {RecordStatus.UNKNOWN}
    assert len(world.trading.sent) == 1


# --- 6: bounded read-back ---------------------------------------------------------------------------


@pytest.mark.req("FR-27")
async def test_a_read_back_that_never_answers_is_fill_unknown() -> None:
    world = make_world()
    await world.grant()
    hold = world.trading.hold_get = anyio.Event()  # set only by the watchdog
    world.place._fill_read_deadline_s = 0.05

    result, error = await call_with_watchdog(world, hold)

    assert error is None
    assert result is not None
    assert result.source is ResultSource.PLACED
    assert result.fill is None
    assert types(world.journal(*ORDER_EVENTS)) == ["order_attempt", "order_outcome"]


# --- 7, 8: stale records -----------------------------------------------------------------------------


async def test_a_sent_record_that_never_completed_is_answered_as_incomplete() -> None:
    world = make_world()
    await world.grant()
    world.trading.after_place = lambda: lock_database_on(world)
    await world(idempotency_key=KEY)  # accepted; the outcome could not be written: record stays sent
    world.db.fail_writes = None
    world.trading.after_place = None

    world.clock.advance(IN_FLIGHT_GUARD + timedelta(seconds=1))
    with pytest.raises(OutcomeUnknown) as repeat:
        await world(idempotency_key=KEY)

    assert repeat.value.details["reason"] == "incomplete"
    assert "new idempotency_key" in (repeat.value.next_step or "")
    assert len(world.trading.sent) == 1


async def test_a_stale_unsent_claim_is_taken_over_and_its_holder_sends_nothing() -> None:
    """Explicit key, first call stuck before its decision for longer than the guard: the key is
    reclaimed (nothing was sent); the stuck call learns which client order id took over."""
    world = make_world()
    await world.grant()
    release = world.trading.hold_band = anyio.Event()
    errors: list[OutcomeUnknown] = []

    async def stuck() -> None:
        try:
            await world(idempotency_key=KEY)
        except OutcomeUnknown as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(stuck)
        await world.trading.in_band.wait()
        world.clock.advance(IN_FLIGHT_GUARD + timedelta(seconds=1))
        fresh = await world(idempotency_key=KEY)
        release.set()

    [superseded] = errors
    assert fresh.source is ResultSource.PLACED
    assert superseded.details["reason"] == "superseded"
    assert superseded.details["holder_client_order_id"] == fresh.client_order_id
    assert superseded.details["client_order_id"] != fresh.client_order_id
    assert len(world.trading.sent) == 1


# --- 9: cancellation and failures around the claim ------------------------------------------------


async def test_a_call_cancelled_while_its_claim_commits_releases_the_claim() -> None:
    world = make_world()
    await world.grant()
    world.db.checkpoint = True
    scope = anyio.CancelScope()

    def cancel_once_claimed() -> None:
        if world.db.state.idempotency:
            scope.cancel()
            world.db.after_commit = None

    world.db.after_commit = cancel_once_claimed
    with scope:
        await world(idempotency_key=KEY)

    assert scope.cancelled_caught
    assert world.trading.sent == []
    assert world.db.state.idempotency == {}


async def test_a_failing_decision_transaction_releases_the_claim() -> None:
    world = make_world()
    await world.grant()

    def fail_the_decision() -> None:
        world.db.fail_next_write = RuntimeError("database is locked")

    world.trading.before_band = fail_the_decision

    with pytest.raises(RuntimeError, match="locked"):
        await world(idempotency_key=KEY)

    assert world.trading.sent == []
    assert world.db.state.idempotency == {}


# --- 10: the fake serialises writers, as SQLite does --------------------------------------------


@pytest.mark.parametrize("serialized", [True, False], ids=["with_lock", "without_lock"])
async def test_the_fake_database_serialises_writers(serialized: bool) -> None:
    """Without the writer lock two concurrent claims of one key both win (a lost update); with it
    exactly one does. The concurrency tests on fakes rely on this."""
    db = InMemoryDatabase()
    db.serialize_writers = serialized
    user = db.add_user()
    winners: list[str] = []

    async def claim(action_id: str) -> None:
        record = new_claim(
            user_id=user.id,
            kind=KeyKind.EXPLICIT,
            key="k",
            request_hash="a" * 64,
            action_id=action_id,
            now=AT,
        )
        async with db(write=True) as uow:
            held = await uow.idempotency.claim(record, AT)
            await anyio.lowlevel.checkpoint()  # another writer may run here
        if held.held_by(action_id):
            winners.append(action_id)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(claim, "a-1")
        tasks.start_soon(claim, "a-2")

    assert len(winners) == (1 if serialized else 2)


# --- 11: repeats are journaled ----------------------------------------------------------------------


@pytest.mark.req("FR-36")
async def test_a_repeat_is_journaled_without_any_exchange_call() -> None:
    world = make_world()
    await world.grant()
    first = await world(idempotency_key=KEY)
    calls = len(world.trading.calls) + len(world.exchange.calls)

    await world(idempotency_key=KEY)

    [repeat] = world.journal("order_repeat")
    assert payload(repeat)["status"] == "replay"
    assert payload(repeat)["holder_action_id"] == first.action_id
    assert payload(repeat)["client_order_id"] == first.client_order_id
    assert payload(repeat)["client"] == {"name": "claude-code", "version": "2.1.312"}
    assert len(world.trading.calls) + len(world.exchange.calls) == calls
