"""``activate_kill_switch`` through an in-process MCP ``Client`` on fakes (ticket 015; SPEC FR-16,
FR-44): the agent turns its kill switch on and the next risk-increasing order is refused; nothing in
the MCP surface can turn it off."""

from typing import Any, get_type_hints

import jsonschema
import pytest
from mcp import Client

from guarded_exchange.application.kill_switch import ActivateKillSwitch, SetKillSwitch
from guarded_exchange.domain.kill_switch import KillSwitch, KillSwitchActor
from guarded_exchange.interface.mcp.server import UseCases
from tests.e2e.support import server_for
from tests.e2e.test_mcp_place_order import MARKET_BUY, Setup
from tests.e2e.test_mcp_policy import error_code, registered_tools
from tests.fakes import FakeClock, InMemoryDatabase

pytestmark = pytest.mark.req("FR-16", "FR-44")


def _structured(content: dict[str, Any] | None) -> dict[str, Any]:
    assert content is not None
    return content


async def test_the_agent_turns_the_kill_switch_on_and_the_next_order_is_refused() -> None:
    setup = Setup()
    await setup.grant()

    async with Client(setup.server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        activated = await client.call_tool("activate_kill_switch", {})
        policy = await client.call_tool("get_my_policy", {})
        order = await client.call_tool("place_order", {**MARKET_BUY, "idempotency_key": "after-switch"})

    assert activated.is_error is False, activated.content
    out = _structured(activated.structured_content)
    assert out == {"kill_switch": "user", "changed": True}
    output_schema = tools["activate_kill_switch"].output_schema
    assert output_schema is not None
    jsonschema.validate(out, output_schema)
    assert _structured(policy.structured_content)["kill_switch"] == "user"
    assert order.is_error is True
    assert error_code(order) == "kill_switch_active"
    assert "Risk-reducing actions still work" in _structured(order.structured_content)["next_step"]
    assert setup.trading.sent == []
    assert setup.db.state.users[setup.user.id].kill_switch is KillSwitch.USER
    events = [r.event_type for r in setup.db.state.audit[setup.user.id]]
    assert events[-2:] == ["kill_switch_on", "order_refused"]


async def test_the_agent_cannot_take_over_or_weaken_the_admins_switch() -> None:
    setup = Setup()
    await setup.grant()
    await SetKillSwitch(setup.db, setup.clock)(setup.user.name, on=True, by=KillSwitchActor.ADMIN)

    async with Client(setup.server) as client:
        again = await client.call_tool("activate_kill_switch", {})

    assert _structured(again.structured_content) == {"kill_switch": "admin", "changed": False}
    assert setup.db.state.users[setup.user.id].kill_switch is KillSwitch.ADMIN


@pytest.mark.req("FR-11")
async def test_a_read_only_user_can_turn_the_kill_switch_on() -> None:
    db = InMemoryDatabase()
    user = db.add_user()  # default policy: read_only

    async with Client(server_for(db, user.id, clock=FakeClock())) as client:
        listed = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("activate_kill_switch", {})

    assert "activate_kill_switch" in listed
    assert "place_order" not in listed
    assert result.is_error is False
    assert db.state.users[user.id].kill_switch is KillSwitch.USER


async def test_nothing_in_the_mcp_registry_can_turn_the_kill_switch_off() -> None:
    """FR-44: the only kill switch tool takes no arguments and can only turn it on; the use case that
    turns it off is wired to the CLI only."""
    db = InMemoryDatabase()
    tools = await registered_tools(server_for(db, db.add_user().id))

    switch_tools = [t for t in tools if "kill" in t.name or "switch" in t.name]
    assert [t.name for t in switch_tools] == ["activate_kill_switch"]
    assert switch_tools[0].input_schema.get("properties", {}) == {}
    assert not any("kill" in name for t in tools for name in t.input_schema.get("properties", {}))
    reachable = set(get_type_hints(UseCases).values())
    assert SetKillSwitch not in reachable
    assert ActivateKillSwitch in reachable
