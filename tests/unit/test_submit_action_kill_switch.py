"""The kill switch on the single path of a trading action (ticket 015; SPEC FR-16, FR-26, FR-36,
FR-43, GP-5; PLAN D-8): refused before anything touches the exchange, re-checked in the decision
under ``lock_user``, never in the way of a repeat or of a risk-reducing order."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from guarded_exchange.application.kill_switch import SetKillSwitch
from guarded_exchange.domain.account import SymbolPosition
from guarded_exchange.domain.errors import KillSwitchActive
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.kill_switch import KillSwitch, KillSwitchActor
from guarded_exchange.domain.policy import InstrumentRef
from tests.fakes import make_position
from tests.unit.test_submit_action import (
    BTC_PERP,
    ORDER_EVENTS,
    World,
    assert_decided_under_lock,
    make_world,
    payload,
)

pytestmark = pytest.mark.req("FR-16")

BTC_SPOT = InstrumentRef(Category.SPOT, "BTCUSDT")


def switch_on(world: World, state: KillSwitch = KillSwitch.USER) -> None:
    """Flip the stored switch directly (as a concurrent CLI or MCP call would have committed it)."""
    world.db.state.users[world.user.id] = replace(world.db.state.users[world.user.id], kill_switch=state)


@pytest.mark.req("FR-36")
@pytest.mark.parametrize("state", [KillSwitch.USER, KillSwitch.ADMIN])
async def test_a_risk_increasing_order_is_refused_before_the_key_or_the_exchange_is_touched(
    state: KillSwitch,
) -> None:
    world = make_world()
    await world.grant()
    switch_on(world, state)
    world.clock.advance(timedelta(hours=2))  # a stale key would be re-checked on the exchange

    with pytest.raises(KillSwitchActive) as error:
        await world()

    assert error.value.details == {"kill_switch": state.value, "action": "place_order", "risk": "increases"}
    assert world.key_reader.calls == []
    assert world.exchange.calls == []
    assert world.trading.calls == []
    [refused] = world.journal(*ORDER_EVENTS)
    assert refused.event_type == "order_refused"
    assert (payload(refused)["stage"], payload(refused)["code"]) == ("pre_check", "kill_switch_active")


@pytest.mark.req("FR-43")
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_any_spot_order_counts_as_risk_increasing(side: str) -> None:
    """GP-5 (strict): a spot sell is not modelled as closing anything."""
    world = make_world()
    await world.grant(allowlist=frozenset({BTC_PERP, BTC_SPOT}))
    switch_on(world)

    with pytest.raises(KillSwitchActive):
        await world(category="spot", side=side)

    assert world.trading.sent == []


@pytest.mark.req("FR-43")
async def test_a_reduce_only_order_still_goes_out_under_the_kill_switch() -> None:
    world = make_world()
    await world.grant()
    world.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", make_position(), leverage=Decimal(5))
    switch_on(world, KillSwitch.ADMIN)

    result = await world(side="sell", qty="0.004", reduce_only=True)

    [sent] = world.trading.sent
    assert (sent.qty, sent.reduce_only) == (Decimal("0.004"), True)
    assert result.order_id is not None
    assert world.journal("order_refused") == []


@pytest.mark.req("FR-48")
async def test_a_kill_switch_turned_on_while_reading_the_market_is_caught_in_the_decision() -> None:
    """D-8: the first check passed; the switch flipped before the decision, which holds lock_user."""
    world = make_world()
    await world.grant()
    world.trading.before_band = lambda: switch_on(world)

    with pytest.raises(KillSwitchActive):
        await world()

    assert world.trading.sent == []
    assert_decided_under_lock(world)
    refused = payload(world.journal("order_refused")[0])
    assert refused["code"] == "kill_switch_active"
    assert "stage" not in refused  # the decision, not the pre-check
    assert world.journal("order_attempt") == []


@pytest.mark.req("FR-26")
async def test_a_repeat_of_an_order_sent_before_the_kill_switch_gets_the_first_answer() -> None:
    """The kill switch is checked after the repeat check: the agent must learn that the first call
    placed its order, not read ``kill_switch_active`` as "nothing happened"."""
    world = make_world()
    await world.grant()
    first = await world(idempotency_key="order-1")
    switch_on(world)

    repeat = await world(idempotency_key="order-1")

    assert repeat.order_id == first.order_id
    assert len(world.trading.sent) == 1
    assert world.journal("order_refused") == []


@pytest.mark.req("FR-26")
async def test_a_keyed_order_refused_by_the_kill_switch_in_the_decision_gets_the_same_answer_again() -> None:
    world = make_world()
    await world.grant()
    world.trading.before_band = lambda: switch_on(world)
    with pytest.raises(KillSwitchActive):
        await world(idempotency_key="order-1")
    world.trading.before_band = None

    with pytest.raises(KillSwitchActive) as repeat:
        await world(idempotency_key="order-1")

    assert repeat.value.details["kill_switch"] == "user"
    assert world.trading.sent == []


async def test_trading_works_again_once_the_user_turns_it_off() -> None:
    world = make_world()
    await world.grant()
    switch = SetKillSwitch(world.db, world.clock)
    await switch(world.user.name, on=True, by=KillSwitchActor.USER)
    with pytest.raises(KillSwitchActive):
        await world()

    await switch(world.user.name, on=False, by=KillSwitchActor.USER)
    await world()

    assert len(world.trading.sent) == 1
