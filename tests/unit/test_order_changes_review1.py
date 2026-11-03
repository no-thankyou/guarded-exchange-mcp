"""Order changes, review 1 of ticket 013: shields and deadlines, the send's failure branches, cancel-all
verification and stopping, the confirmation threshold of an amend, a closed order after an amend, and
stale reads between concurrent changes of one order (FR-13, FR-16, FR-19, FR-26, FR-27, D-8, D-21)."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import anyio
import pytest

from guarded_exchange.application.order_change_results import ChangeState, ItemState
from guarded_exchange.application.order_results import ResultSource
from guarded_exchange.domain.account import OrderStatus
from guarded_exchange.domain.audit import AuditEvent, AuditEventType, AuditRecord, canonical_json
from guarded_exchange.domain.errors import (
    ConfirmationChannelUnavailable,
    ExchangeUnavailable,
    LimitExceeded,
    OutcomeUnknown,
    RateLimited,
)
from guarded_exchange.domain.idempotency import KeyKind, RecordStatus
from guarded_exchange.domain.kill_switch import KillSwitch
from tests.fakes import call_with_watchdog
from tests.fakes_trading import plain_order
from tests.unit.test_order_changes import UNKNOWN, World, make_world, payload

pytestmark = pytest.mark.req("FR-27")


def kinds(world: World) -> list[str]:
    return [r.event_type for r in world.journal()]


def record_status(world: World, key: str) -> RecordStatus | None:
    record = world.db.state.idempotency.get((world.user.id, KeyKind.EXPLICIT, key))
    return None if record is None else record.status


async def ready(world: World, *order_ids: str) -> None:
    await world.grant()
    world.changes.add(*(plain_order(order_id) for order_id in order_ids))


# --- one change: shield, bug, deadline ---------------------------------------------------------------


@pytest.mark.req("FR-26")
@pytest.mark.parametrize("tool", ["cancel", "amend", "cancel_all"])
async def test_a_caller_cancelled_during_the_post_still_records_the_outcome_and_keeps_the_key(
    tool: str,
) -> None:
    world = make_world()
    await ready(world, "o-1")
    world.db.checkpoint = True  # every transaction is a cancellation point, as with a real driver
    hold = world.changes.hold_send = anyio.Event()
    calls = {
        "cancel": lambda: world.cancel(idempotency_key="k-1"),
        "amend": lambda: world.amend(qty="0.001", idempotency_key="k-1"),
        "cancel_all": lambda: world.cancel_all(idempotency_key="k-1"),
    }

    async def run() -> None:
        await calls[tool]()

    async with anyio.create_task_group() as tasks:
        with anyio.CancelScope() as scope:
            tasks.start_soon(run)
            await world.changes.sending.wait()
            scope.cancel()
        tasks.cancel_scope.cancel()
        hold.set()

    assert kinds(world)[-2:] == ["order_attempt", "order_outcome"]
    assert record_status(world, "k-1") is RecordStatus.COMPLETED  # kept: a repeat is a replay
    again = await calls[tool]()
    assert again.source is ResultSource.REPLAY


@pytest.mark.parametrize("tool", ["cancel", "amend"])
async def test_a_bug_in_the_adapter_is_an_unknown_outcome_journaled_as_such(tool: str) -> None:
    world = make_world()
    await ready(world, "o-1")
    world.changes.errors["o-1"] = RuntimeError("adapter bug")

    with pytest.raises(OutcomeUnknown) as error:
        await (world.cancel() if tool == "cancel" else world.amend(qty="0.001"))

    assert error.value.details["reason"] == "internal_error"
    outcome = payload(world.journal("order_outcome")[0])
    assert (outcome["status"], outcome["error_type"]) == ("unknown", "RuntimeError")


async def test_a_cancel_that_never_answers_hits_the_deadline_and_is_looked_up() -> None:
    world = make_world()
    await ready(world, "o-1")
    hold = world.changes.hold_send = anyio.Event()  # only the watchdog ends it
    world.cancel_uc._send_deadline_s = 0.05

    _, error = await call_with_watchdog(world.cancel, hold)

    assert isinstance(error, OutcomeUnknown)
    assert (error.details["reason"], error.details["reconnaissance"]) == ("send_deadline", "found")
    reads = [c for c in world.changes.calls if c[0] == "get_order_by_id"]
    assert len(reads) == 2  # before the decision, then the reconnaissance
    assert world.changes.cancels == ["o-1"]


# --- cancel-all: stopping, verification, outcome kind -------------------------------------------------


@pytest.mark.req("FR-26")
async def test_an_accepted_cancel_then_a_rate_limit_is_a_result_and_the_key_is_kept() -> None:
    world = make_world()
    await ready(world, "a", "b")
    world.changes.apply = False  # a accepted but still listed
    world.changes.errors["b"] = RateLimited("slow down", retry_after_s=1)

    result = await world.cancel_all(idempotency_key="k-1")

    assert [i.state for i in result.items] == [ItemState.CANCEL_REQUESTED, ItemState.NOT_SENT]
    assert result.stopped_by == "rate_limited"
    assert record_status(world, "k-1") is RecordStatus.COMPLETED


async def test_a_failed_verification_read_never_turns_a_state_into_closed() -> None:
    world = make_world()
    await ready(world, "a")
    world.changes.errors["a"] = UNKNOWN
    world.changes.apply_after_error = True  # it did happen, but the list cannot be read
    world.changes.list_error = RuntimeError("list broke")

    result = await world.cancel_all()

    assert [i.state for i in result.items] == [ItemState.UNKNOWN]


async def test_an_unknown_cancel_whose_order_left_the_list_is_closed() -> None:
    world = make_world()
    await ready(world, "a")
    world.changes.errors["a"] = UNKNOWN
    world.changes.apply_after_error = True

    result = await world.cancel_all()

    assert [i.state for i in result.items] == [ItemState.CLOSED]


async def test_a_list_with_more_pages_settles_nothing() -> None:
    """An order beyond the page may be one of ours: absence from the page proves nothing."""
    world = make_world()
    await ready(world, "a")
    world.changes.has_more = True

    result = await world.cancel_all()

    assert world.changes.orders["a"].status is OrderStatus.CANCELLED
    assert [i.state for i in result.items] == [ItemState.CANCEL_REQUESTED]


async def test_an_exhausted_overall_deadline_sends_nothing_more() -> None:
    world = make_world()
    await ready(world, "a", "b")
    world.cancel_all_uc._send_deadline_s = 0

    with pytest.raises(ExchangeUnavailable) as error:
        await world.cancel_all()

    assert error.value.details["reason"] == "send_deadline"
    assert world.changes.cancels == []
    assert payload(world.journal("order_outcome")[0])["status"] == "not_sent"


async def test_a_bug_in_one_cancel_stops_the_rest() -> None:
    world = make_world()
    await ready(world, "a", "b")
    world.changes.errors["a"] = RuntimeError("adapter bug")

    result = await world.cancel_all()

    assert world.changes.cancels == ["a"]
    assert [i.state for i in result.items] == [ItemState.UNKNOWN, ItemState.NOT_SENT]
    assert result.stopped_by == "outcome_unknown"


@pytest.mark.req("FR-26")
@pytest.mark.parametrize(
    ("unknown", "status"), [(True, RecordStatus.UNKNOWN), (False, RecordStatus.COMPLETED)]
)
async def test_the_stored_outcome_is_unknown_exactly_when_an_order_is_unknown(
    unknown: bool, status: RecordStatus
) -> None:
    world = make_world()
    await ready(world, "a")
    if unknown:
        world.changes.errors["a"] = UNKNOWN
        world.changes.apply = False

    await world.cancel_all(idempotency_key="k-1")

    assert record_status(world, "k-1") is status


async def test_a_bug_after_the_sends_is_an_unknown_outcome_with_a_hint() -> None:
    world = make_world()
    await ready(world, "a")

    async def broken(*args: Any) -> Any:
        raise RuntimeError("verification bug")

    world.cancel_all_uc._verify = broken  # type: ignore[method-assign, assignment]

    with pytest.raises(OutcomeUnknown) as error:
        await world.cancel_all()

    assert error.value.details["reason"] == "internal_error"
    assert "get_open_orders" in (error.value.next_step or "")
    assert payload(world.journal("order_outcome")[0])["status"] == "unknown"


async def test_a_non_error_interruption_is_recorded_and_re_raised() -> None:
    world = make_world()
    await ready(world, "a")

    async def interrupted(*args: Any) -> Any:
        raise KeyboardInterrupt

    world.cancel_all_uc._verify = interrupted  # type: ignore[method-assign, assignment]

    with pytest.raises(KeyboardInterrupt):
        await world.cancel_all()

    assert payload(world.journal("order_outcome")[0])["error_type"] == "KeyboardInterrupt"


# --- amend: threshold and closed order ----------------------------------------------------------------


@pytest.mark.req("FR-19", "FR-25")
async def test_an_amend_at_the_confirmation_threshold_needs_confirmation() -> None:
    world = make_world()
    await world.grant(confirm_above_notional_usdt=Decimal(200))
    world.changes.add(plain_order("o-1"), plain_order("o-2"))  # 0.002 x 80000 = 160

    with pytest.raises(ConfirmationChannelUnavailable):
        await world.amend(qty="0.003")  # 240 >= 200

    assert world.changes.amends == []
    assert (await world.amend("o-2", qty="0.001")).state is ChangeState.AMENDED  # 80 < 200


async def test_an_amend_whose_order_closed_before_it_applied_says_so() -> None:
    world = make_world()
    await ready(world, "o-1")
    world.changes.apply = False
    world.changes.on_send = lambda: world.changes.add(plain_order("o-1", status=OrderStatus.FILLED))

    result = await world.amend(price="79990.5")

    assert result.state is ChangeState.ORDER_CLOSED
    assert result.after is not None
    assert result.after.status is OrderStatus.FILLED


# --- stale reads between changes of one order (D-8) ---------------------------------------------------


@pytest.mark.req("FR-13")
async def test_an_amend_after_an_accepted_but_invisible_one_is_refused() -> None:
    """Reviewer scenario 1: amend A (qty up, within the limit) is accepted but not visible; amend B reads
    the old qty and raises the price - against the real qty it would exceed the order value limit."""
    world = make_world()
    await world.grant(max_order_notional_usdt=Decimal(250))
    world.changes.add(plain_order("o-1"))  # 0.002 x 80000
    world.changes.apply = False

    first = await world.amend(qty="0.003")  # 240: allowed, not visible yet
    assert first.state is ChangeState.AMEND_REQUESTED
    world.clock.advance(timedelta(seconds=2))  # B reads after A's outcome: only its state makes B unsafe

    with pytest.raises(RateLimited) as error:
        await world.amend(price="80100")  # as read: 0.002 x 80100 = 160.2; really 0.003 x 80100 = 240.3

    assert error.value.details == {"reason": "order_change_in_progress", "order_id": "o-1"}
    assert len(world.changes.amends) == 1
    assert payload(world.journal("order_refused")[0])["code"] == "rate_limited"


@pytest.mark.req("FR-16")
async def test_a_reducing_looking_amend_cannot_slip_past_the_kill_switch_on_a_stale_read() -> None:
    """Reviewer scenario 2: under the kill switch, amend A shrinks the order 0.003 -> 0.001 (reduces,
    allowed) and is accepted but not visible; amend B to 0.002 looks reducing against the stale 0.003,
    but against the real 0.001 it grows the order. It is refused and never sent."""
    world = make_world()
    await world.grant()
    world.changes.add(plain_order("o-1", qty="0.003"))
    world.changes.apply = False
    world.switch_on(KillSwitch.USER)
    first = await world.amend(qty="0.001")
    assert first.state is ChangeState.AMEND_REQUESTED
    world.clock.advance(timedelta(seconds=2))

    with pytest.raises(RateLimited):
        await world.amend(qty="0.002")

    assert [a.qty for a in world.changes.amends] == [Decimal("0.001")]


def _append(world: World, event: AuditEventType, body: dict[str, Any], at: datetime) -> None:
    chain = world.db.state.audit.setdefault(world.user.id, [])
    chain.append(
        AuditRecord(
            chain_id=world.user.id,
            seq=len(chain) + 1,
            ts=at,
            event_type=str(event),
            actor="mcp:agent",
            payload=json.loads(canonical_json(AuditEvent(event, "mcp:agent", body).payload)),
        )
    )


@pytest.mark.req("FR-13")
async def test_a_change_confirmed_after_this_calls_read_makes_the_read_stale() -> None:
    """Another client's amend is attempted and confirmed while this call reads the exchange."""
    world = make_world()
    await world.grant(max_order_notional_usdt=Decimal(250))
    world.changes.add(plain_order("o-1"))

    def other_client_amends() -> None:
        now = world.clock.now()
        order = {"order_id": "o-1"}
        _append(
            world,
            AuditEventType.ORDER_ATTEMPT,
            {"tool": "amend_order", "action_id": "x", "order": order},
            now,
        )
        _append(
            world,
            AuditEventType.ORDER_OUTCOME,
            {"action_id": "x", "status": "accepted", "state": "amended"},
            now,
        )

    world.trading.before_band = other_client_amends

    with pytest.raises(RateLimited):
        await world.amend(price="80100")

    world.trading.before_band = None
    world.clock.advance(timedelta(seconds=1))  # the next read starts after that outcome
    assert (await world.amend(price="80100")).state is ChangeState.AMENDED  # a fresh read is fine


async def test_a_definitely_refused_earlier_change_does_not_block() -> None:
    world = make_world()
    await ready(world, "o-1")
    world.changes.errors["o-1"] = LimitExceeded("no", details={})
    with pytest.raises(LimitExceeded):
        await world.amend(qty="0.001")
    world.changes.errors.clear()

    assert (await world.amend(qty="0.001")).state is ChangeState.AMENDED


async def test_a_change_still_in_flight_blocks_an_amend_of_the_same_order() -> None:
    """Another client's amend is journaled as attempted (sent) but has no outcome yet."""
    world = make_world()
    await ready(world, "o-1", "o-2")
    attempt = {"tool": "amend_order", "action_id": "x", "order": {"order_id": "o-1"}}
    _append(world, AuditEventType.ORDER_ATTEMPT, attempt, world.clock.now())
    world.clock.advance(timedelta(seconds=2))

    with pytest.raises(RateLimited):
        await world.amend(price="80100")
    assert (await world.amend("o-2", price="80100")).state is ChangeState.AMENDED  # another order: free
    world.clock.advance(timedelta(seconds=61))  # outside the window: settled by now
    assert (await world.amend(price="80100")).state is ChangeState.AMENDED
