"""Policy over MCP with an in-process ``Client``: ``get_my_policy``, the registry (FR-44) and
per-principal tool visibility with the check on every call (FR-11, ADR-012)."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, get_type_hints

import jsonschema
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, Tool

from guarded_exchange.application.policy import ReadPolicy, ResetOverrides, SetCeiling, TightenPolicy
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import InstrumentRef, Permission, PolicyPatch, Profile
from guarded_exchange.interface.mcp.access import TOOL_PERMISSIONS
from guarded_exchange.interface.mcp.server import UseCases
from tests.e2e.support import server_for
from tests.fakes import FakeClock, InMemoryDatabase, InMemoryUnitOfWork

BTC = InstrumentRef(Category.LINEAR, "BTCUSDT")
GRANT_TRADING = PolicyPatch(profile=Profile.TRADE, allowlist=frozenset({BTC}))
WITH_TRADING_TOOL = {**TOOL_PERMISSIONS, "stub_trade": Permission.TRADE}


def with_stub_trading_tool(mcp: MCPServer[Any], calls: list[str]) -> MCPServer[Any]:
    """A stand-in trading tool with a trivial schema: records that its body ran (the real
    ``place_order`` has its own e2e tests)."""

    @mcp.tool(name="stub_trade", description="Stub trading tool.")
    async def stub_trade(symbol: str) -> str:
        calls.append(symbol)
        return "placed"

    return mcp


async def tool_names(client: Client) -> set[str]:
    return {tool.name for tool in (await client.list_tools()).tools}


def error_code(result: CallToolResult) -> str:
    assert result.is_error is True
    assert result.structured_content is not None
    code: str = result.structured_content["code"]
    return code


# --- get_my_policy ------------------------------------------------------------------------


@pytest.mark.req("FR-44", "FR-12", "FR-45")
async def test_get_my_policy_returns_the_effective_policy() -> None:
    db, clock = InMemoryDatabase(), FakeClock()
    user = db.add_user("alice")
    await SetCeiling(db, clock)("alice", GRANT_TRADING)
    await TightenPolicy(db, clock)("alice", PolicyPatch(max_order_notional_usdt=Decimal("250.50")))

    async with Client(server_for(db, user.id)) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "get_my_policy")
        result = await client.call_tool("get_my_policy", {})

    assert result.is_error is False
    assert result.structured_content is not None
    assert tool.output_schema is not None
    jsonschema.validate(result.structured_content, tool.output_schema)
    policy = result.structured_content
    assert policy["profile"] == "trade"
    assert policy["environment"] == "testnet"
    assert policy["allowed_instruments"] == ["linear:BTCUSDT"]
    assert policy["max_order_notional_usdt"] == "250.5"  # the user's tighter value, exact decimal
    assert policy["max_leverage"] == "10"
    assert policy["confirmation_ttl_s"] == 120
    assert policy["trust_client_ui"] is True
    assert "confirm_above_notional_usdt" not in policy  # no threshold: omitted (FR-3)
    assert len(policy["confirm_actions"]) == 7


@pytest.mark.req("FR-45")
async def test_new_user_sees_read_only_with_no_instruments() -> None:
    db = InMemoryDatabase()
    user = db.add_user()

    async with Client(server_for(db, user.id)) as client:
        result = await client.call_tool("get_my_policy", {})

    assert result.structured_content is not None
    assert result.structured_content["profile"] == "read_only"
    assert result.structured_content["allowed_instruments"] == []


# --- registry (FR-44) ---------------------------------------------------------------------

# Anything about the policy itself; trading verbs like set_leverage are not policy changes.
_POLICY_WORDS = re.compile(
    r"polic|ceiling|override|allowlist|whitelist|permission|profile|limit|ttl|trust|tighten|reset"
)


async def registered_tools(mcp: MCPServer[Any]) -> list[Tool]:
    """Every registered tool, bypassing the per-principal filter."""
    return await MCPServer.list_tools(mcp)


@pytest.mark.req("FR-44")
async def test_the_registry_has_no_tool_that_changes_the_policy() -> None:
    db = InMemoryDatabase()
    tools = await registered_tools(server_for(db, db.add_user().id))

    names = {tool.name for tool in tools}
    assert {name for name in names if _POLICY_WORDS.search(name)} == {"get_my_policy"}
    get_my_policy = next(tool for tool in tools if tool.name == "get_my_policy")
    assert get_my_policy.annotations is not None
    assert get_my_policy.annotations.read_only_hint is True
    assert get_my_policy.input_schema.get("properties", {}) == {}  # nothing to pass, nothing to change


@pytest.mark.req("FR-44")
def test_mcp_tools_cannot_reach_any_policy_changing_use_case() -> None:
    """The admin reset (ticket 034), the ceiling and tightening are wired to the CLI only."""
    reachable = set(get_type_hints(UseCases).values())

    assert reachable.isdisjoint({SetCeiling, TightenPolicy, ResetOverrides})
    assert ReadPolicy in reachable


@pytest.mark.req("FR-11", "NFR-1")
async def test_every_registered_tool_declares_its_permission() -> None:
    db = InMemoryDatabase()
    names = {tool.name for tool in await registered_tools(server_for(db, db.add_user().id))}

    assert names == TOOL_PERMISSIONS.keys()


# --- visibility and the check on every call (FR-11, ADR-012) -------------------------------


@pytest.mark.req("FR-11", "AC-2")
async def test_read_only_user_does_not_see_trading_tools_and_cannot_call_them() -> None:
    db, calls = InMemoryDatabase(), list[str]()
    user = db.add_user()
    mcp = with_stub_trading_tool(server_for(db, user.id, tool_permissions=WITH_TRADING_TOOL), calls)

    async with Client(mcp) as client:
        listed = await client.session.list_tools()
        result = await client.call_tool("stub_trade", {"symbol": "BTCUSDT"})
        bad_args = await client.call_tool("stub_trade", {})

    read_tools = {name for name, needed in TOOL_PERMISSIONS.items() if needed is Permission.READ}
    assert {tool.name for tool in listed.tools} == read_tools  # every read tool, no trading
    assert (listed.ttl_ms, listed.cache_scope) == (0, "private")  # per-user list, never shared
    assert error_code(result) == "permission_denied"
    assert result.structured_content is not None
    assert result.structured_content["details"] == {"profile": "read_only", "required": "trade"}
    assert result.structured_content["next_step"]
    assert error_code(bad_args) == "permission_denied"  # refused before argument validation
    assert calls == []  # the tool body never ran


@pytest.mark.req("FR-11", "FR-28")
async def test_permitted_user_with_bad_arguments_gets_invalid_params() -> None:
    db, calls = InMemoryDatabase(), list[str]()
    user = db.add_user("alice")
    await SetCeiling(db, FakeClock())("alice", GRANT_TRADING)
    mcp = with_stub_trading_tool(server_for(db, user.id, tool_permissions=WITH_TRADING_TOOL), calls)

    async with Client(mcp) as client:
        result = await client.call_tool("stub_trade", {})

    assert error_code(result) == "invalid_params"  # past the permission check, then argument validation
    assert result.structured_content is not None
    assert result.structured_content["details"]["field"] == "symbol"
    assert calls == []


class BrokenDatabase(InMemoryDatabase):
    """Every transaction fails, as a locked or corrupt database would."""

    @asynccontextmanager
    async def __call__(self, *, write: bool) -> AsyncIterator[InMemoryUnitOfWork]:
        raise RuntimeError("db password=hunter2 at /srv/secret.py:42")
        yield  # pragma: no cover


@pytest.mark.req("FR-11", "FR-28", "NFR-1")
async def test_a_failing_access_check_refuses_with_internal_error_and_no_details() -> None:
    async with Client(server_for(BrokenDatabase(), "user-local")) as client:
        result = await client.call_tool("get_ticker", {"symbol": "BTCUSDT", "category": "linear"})

    assert error_code(result) == "internal_error"  # fail closed: the tool did not run
    assert result.structured_content is not None
    assert result.structured_content["details"]["request_id"]
    assert "hunter2" not in result.model_dump_json()
    assert "RuntimeError" not in result.model_dump_json()


@pytest.mark.req("FR-11", "FR-12")
async def test_policy_changes_apply_to_the_next_list_and_call_without_reconnecting() -> None:
    db, clock, calls = InMemoryDatabase(), FakeClock(), list[str]()
    user = db.add_user("alice")
    mcp = with_stub_trading_tool(server_for(db, user.id, tool_permissions=WITH_TRADING_TOOL), calls)

    async with Client(mcp) as client:
        before = await tool_names(client)
        await SetCeiling(db, clock)("alice", GRANT_TRADING)
        granted = await tool_names(client)
        placed = await client.call_tool("stub_trade", {"symbol": "BTCUSDT"})
        await TightenPolicy(db, clock)("alice", PolicyPatch(profile=Profile.READ_ONLY))
        after = await tool_names(client)
        refused = await client.call_tool("stub_trade", {"symbol": "BTCUSDT"})

    assert "stub_trade" not in before
    assert "stub_trade" in granted
    assert placed.is_error is False
    assert "stub_trade" not in after
    assert error_code(refused) == "permission_denied"
    assert calls == ["BTCUSDT"]


@pytest.mark.req("FR-11", "NFR-1")
async def test_a_tool_without_a_declared_permission_is_hidden_and_refused() -> None:
    db, calls = InMemoryDatabase(), list[str]()
    owner = db.add_user("alice")
    await SetCeiling(db, FakeClock())("alice", GRANT_TRADING)
    mcp = with_stub_trading_tool(server_for(db, owner.id), calls)  # stub_trade not declared

    async with Client(mcp) as client:
        names = await tool_names(client)
        result = await client.call_tool("stub_trade", {"symbol": "BTCUSDT"})

    assert "stub_trade" not in names
    assert error_code(result) == "permission_denied"
    assert calls == []


@pytest.mark.req("FR-11", "NFR-1")
@pytest.mark.parametrize("principal", [None, "user-ghost"])
async def test_unknown_principal_sees_nothing_and_every_call_is_refused(principal: str | None) -> None:
    db = InMemoryDatabase()
    db.add_user()

    async with Client(server_for(db, principal)) as client:
        names = await tool_names(client)
        ticker = await client.call_tool("get_ticker", {"symbol": "BTCUSDT", "category": "linear"})
        policy = await client.call_tool("get_my_policy", {})

    assert names == set()
    assert error_code(ticker) == "permission_denied"
    assert error_code(policy) == "permission_denied"
