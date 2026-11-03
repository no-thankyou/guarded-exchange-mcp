"""FR-27 on fakes (ticket 012): after an unknown outcome the order is looked up by its client order
id, never sent again; "not found anywhere" stays unknown; the send and the lookup are bounded."""

from decimal import Decimal

import anyio
import pytest

from guarded_exchange.application.submit_action import ResultSource
from guarded_exchange.domain.errors import ExchangeUnavailable, OutcomeUnknown
from guarded_exchange.domain.idempotency import KeyKind, RecordStatus
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.orders import FillStatus, OrderFill
from tests.unit.test_submit_action import ORDER_EVENTS, World, make_world, payload, types

pytestmark = pytest.mark.req("FR-27")

CLIENT_ID = "gx-act-0001"
FOUND = OrderFill(
    order_id="exchange-order-7",
    status=FillStatus.FILLED,
    qty=Decimal("0.001"),
    price=Decimal("81747.7"),
    filled_qty=Decimal("0.001"),
    avg_fill_price=Decimal("80571.2"),
)
TIMEOUT = OutcomeUnknown("no reliable answer from the exchange: ReadTimeout", details={"reason": "no_answer"})


def lookups(world: World) -> list[tuple[object, ...]]:
    return [c for c in world.trading.calls if c[0] == "find_order"]


async def test_edge_a_timeout_after_sending_then_found_is_accepted_without_resending() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = TIMEOUT
    world.trading.fills[CLIENT_ID] = FOUND  # the exchange did create it

    result = await world()

    assert result.source is ResultSource.RECONNAISSANCE
    assert (result.order_id, result.fill) == ("exchange-order-7", FOUND)
    assert len(world.trading.sent) == 1
    assert lookups(world) == [("find_order", Category.LINEAR, "BTCUSDT", CLIENT_ID)]
    attempt, outcome = world.journal(*ORDER_EVENTS)
    assert types([attempt, outcome]) == ["order_attempt", "order_outcome"]
    assert payload(outcome)["status"] == "accepted"
    assert payload(outcome)["reconnaissance"] == "found"
    assert payload(outcome)["send_error"] == {"code": "outcome_unknown", "details": {"reason": "no_answer"}}


async def test_an_order_only_in_the_history_is_found_too() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = TIMEOUT
    world.trading.history[CLIENT_ID] = FOUND

    result = await world(idempotency_key="k-1")

    assert result.source is ResultSource.RECONNAISSANCE
    [record] = world.db.state.idempotency.values()
    assert record.status is RecordStatus.COMPLETED
    assert (await world(idempotency_key="k-1")).order_id == "exchange-order-7"  # replayed as found


async def test_edge_not_found_anywhere_is_outcome_unknown_never_failed() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = TIMEOUT

    with pytest.raises(OutcomeUnknown) as error:
        await world(idempotency_key="k-1")

    details = error.value.details
    assert (details["client_order_id"], details["reconnaissance"], details["reason"]) == (
        CLIENT_ID,
        "not_found",
        "no_answer",
    )
    assert "Do not place it again" in (error.value.next_step or "")
    assert len(world.trading.sent) == 1  # no automatic resend
    outcome = world.journal("order_outcome")[-1]
    assert (payload(outcome)["status"], payload(outcome)["reconnaissance"]) == ("unknown", "not_found")
    assert world.db.state.idempotency[(world.user.id, KeyKind.EXPLICIT, "k-1")].status is RecordStatus.UNKNOWN


@pytest.mark.parametrize(
    "failure", [ExchangeUnavailable("down"), RuntimeError("a bug")], ids=["unavailable", "bug"]
)
async def test_a_failed_lookup_is_still_unknown(failure: Exception) -> None:
    """Never ``internal_error`` after a possibly-sent order: the agent could place it again."""
    world = make_world()
    await world.grant()
    world.trading.place_error = TIMEOUT
    world.trading.find_error = failure

    with pytest.raises(OutcomeUnknown) as error:
        await world()

    assert error.value.details["reconnaissance"] == "failed"
    assert "looking it up" in error.value.message
    assert len(world.trading.sent) == 1


async def test_a_lookup_that_never_answers_is_bounded() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = TIMEOUT
    world.trading.hold_find = anyio.Event()  # never set
    world.place._reconnaissance_deadline_s = 0.05

    with anyio.fail_after(5), pytest.raises(OutcomeUnknown) as error:
        await world()

    assert error.value.details["reconnaissance"] == "failed"
    assert types(world.journal(*ORDER_EVENTS)) == ["order_attempt", "order_outcome"]


async def test_a_send_that_never_answers_hits_the_overall_deadline_and_is_looked_up() -> None:
    """011 review: the HTTP read timeout bounds only the silence between chunks; the send as a whole
    has its own deadline, after which the outcome is unknown and the order is looked up."""
    world = make_world()
    await world.grant()
    world.trading.hold_place = anyio.Event()  # never set: a slowly dripping answer
    world.place._send_deadline_s = 0.05

    with anyio.fail_after(5), pytest.raises(OutcomeUnknown) as error:
        await world()

    assert error.value.details["reason"] == "send_deadline"
    assert len(world.trading.sent) == 1
    assert len(lookups(world)) == 1


@pytest.mark.parametrize(
    "cause",
    [
        OutcomeUnknown("x", details={"reason": "exchange_internal_error", "exchange_code": "10016"}),
        OutcomeUnknown("x", details={"reason": "duplicate_client_order_id", "exchange_code": "110072"}),
        OutcomeUnknown("x", details={"reason": "unrecognised_exchange_code", "exchange_code": "99999"}),
    ],
    ids=["internal_error", "duplicate_id", "unrecognised_code"],
)
async def test_every_unknown_outcome_is_looked_up(cause: OutcomeUnknown) -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = cause
    world.trading.fills[CLIENT_ID] = FOUND

    result = await world()

    assert result.source is ResultSource.RECONNAISSANCE
    assert len(world.trading.sent) == 1
