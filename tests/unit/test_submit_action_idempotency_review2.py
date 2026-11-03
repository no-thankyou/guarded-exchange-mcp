"""Ticket 012, review round 2: a keyed call keeps its fingerprint through a long stall, a keyless
repeat of a keyed unknown outcome is warned, a lost keyed repeat never touches the fingerprint."""

from datetime import timedelta
from decimal import Decimal

import anyio
import pytest

from guarded_exchange.application.submit_action import OrderResult, ResultSource
from guarded_exchange.domain.errors import LimitExceeded, OutcomeUnknown
from guarded_exchange.domain.idempotency import IN_FLIGHT_GUARD, KeyKind
from tests.unit.test_submit_action import make_world
from tests.unit.test_submit_action_idempotency import KEY, records

pytestmark = pytest.mark.req("FR-26")


async def test_a_keyed_call_stuck_past_the_guard_takes_its_fingerprint_again() -> None:
    """The keyed call's fingerprint expired while it was stuck before its decision; a keyless
    identical call from another client during its send must be a repeat, not a second order."""
    world = make_world()
    await world.grant()
    band = world.trading.hold_band = anyio.Event()
    world.trading.hold_place = anyio.Event()
    results: list[OrderResult] = []

    async def keyed() -> None:
        results.append(await world(idempotency_key=KEY))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(keyed)
        await world.trading.in_band.wait()
        world.clock.advance(IN_FLIGHT_GUARD + timedelta(seconds=1))
        band.set()
        await world.trading.placing.wait()
        with pytest.raises(OutcomeUnknown) as keyless:
            await world()
        world.trading.hold_place.set()

    assert keyless.value.details["reason"] == "in_progress"
    assert len(world.trading.sent) == 1
    assert results[0].source is ResultSource.PLACED


@pytest.mark.req("FR-27")
async def test_a_keyless_repeat_of_a_keyed_unknown_outcome_is_told_a_later_repeat_is_a_new_order() -> None:
    world = make_world()
    await world.grant()
    world.trading.place_error = OutcomeUnknown("read timeout", details={"reason": "no_answer"})
    with pytest.raises(OutcomeUnknown) as keyed:
        await world(idempotency_key=KEY)
    world.trading.place_error = None

    with pytest.raises(OutcomeUnknown) as keyless:
        await world()  # another client, no key: answered from the fingerprint the keyed call stored

    assert "same idempotency_key returns this answer" in (keyed.value.next_step or "")
    assert "places a NEW order" in (keyless.value.next_step or "")
    assert "same idempotency_key" not in (keyless.value.next_step or "")
    assert len(world.trading.sent) == 1


async def test_a_keyed_repeat_that_loses_its_key_never_touches_the_fingerprint() -> None:
    """A refused keyed call keeps its key but releases the fingerprint; repeating it with the key must
    not claim that free fingerprint (it would stay ``in_progress`` and block the keyless call)."""
    world = make_world()
    await world.grant(max_order_notional_usdt=Decimal(50))
    with pytest.raises(LimitExceeded):
        await world(idempotency_key=KEY)
    await world.grant(max_order_notional_usdt=Decimal(1000))

    with pytest.raises(LimitExceeded):
        await world(idempotency_key=KEY)  # replayed refusal

    assert set(records(world)) == {(KeyKind.EXPLICIT, KEY)}
    assert (await world()).source is ResultSource.PLACED
