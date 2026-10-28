"""Spike 002, Q2: ``RequestStateSecurity`` - keys from config, restart, audience, principal, TTL.

Source: ``mcp/server/request_state.py`` (``mcp`` 2.3.0, read 2026-10-27). The sealed token is
AES-256-GCM (HKDF-SHA256 of each configured key, keys >= 32 bytes, ``keys[0]`` seals, all unseal)
over claims ``{v, iat, exp, m, t, a, s, aud?, p?}``: method, tool name, SHA-256 of the raw
``arguments``, our plaintext, audience and a salted hash of the principal. Every failure is the
same ``-32602 "Invalid or expired requestState"``; the reason goes to the server log only.

A "restart" is modelled as a second ``MCPServer`` instance: nothing else is shared between them.
"""

import contextvars
import json
import time
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context, RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    InputResponses,
)

KEY_A = b"a" * 32  # in the product: HKDF subkey "request_state" of the master key (D-5, D-14)
KEY_B = b"b" * 32
ARGS = {"symbol": "BTCUSDT"}
ANSWERS: InputResponses = {"confirm": ElicitResult(action="accept", content={"confirm": True})}

# Stands in for "who is calling": the product would read the verified bearer token / local user.
current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user", default=None)


def by_user(ctx: ServerRequestContext[Any, Any]) -> str | None:
    return current_user.get()


def build_server(name: str, security: RequestStateSecurity | None) -> MCPServer[Any]:
    mcp: MCPServer[Any] = MCPServer(name, request_state_security=security)

    @mcp.tool(name="place_order")
    async def place_order(symbol: str, ctx: Context) -> CallToolResult | InputRequiredResult:
        if ctx.request_state is None:  # round 2 is recognised by our sealed state (ADR-005, D-1)
            return InputRequiredResult(
                input_requests={
                    "confirm": ElicitRequest(
                        params=ElicitRequestFormParams(
                            message=f"Buy {symbol}?",
                            requested_schema={
                                "type": "object",
                                "properties": {"confirm": {"type": "boolean"}},
                            },
                        )
                    )
                },
                request_state=json.dumps({"pending_id": "p-1"}),
            )
        return CallToolResult(content=[], structured_content={"state": ctx.request_state})

    return mcp


async def mint(server: MCPServer[Any]) -> str:
    async with Client(server) as client:
        first = await client.session.call_tool("place_order", ARGS, allow_input_required=True)
    assert isinstance(first, InputRequiredResult) and first.request_state is not None
    return first.request_state


async def redeem(server: MCPServer[Any], state: str) -> CallToolResult:
    async with Client(server) as client:
        result = await client.session.call_tool(
            "place_order", ARGS, input_responses=ANSWERS, request_state=state, allow_input_required=True
        )
    assert isinstance(result, CallToolResult)
    return result


async def assert_rejected(server: MCPServer[Any], state: str) -> None:
    async with Client(server) as client:
        with pytest.raises(MCPError) as info:
            await client.session.call_tool("place_order", ARGS, input_responses=ANSWERS, request_state=state)
    assert (info.value.error.code, info.value.error.message) == (-32602, "Invalid or expired requestState")


async def test_default_ephemeral_key_does_not_survive_restart() -> None:
    state = await mint(build_server("gx", None))
    await assert_rejected(build_server("gx", None), state)


async def test_configured_key_survives_restart() -> None:
    state = await mint(build_server("gx", RequestStateSecurity(keys=[KEY_A])))
    result = await redeem(build_server("gx", RequestStateSecurity(keys=[KEY_A])), state)
    assert result.structured_content == {"state": '{"pending_id": "p-1"}'}


async def test_key_rotation_ring_unseals_with_old_key() -> None:
    state = await mint(build_server("gx", RequestStateSecurity(keys=[KEY_A])))
    await redeem(build_server("gx", RequestStateSecurity(keys=[KEY_B, KEY_A])), state)
    await assert_rejected(build_server("gx", RequestStateSecurity(keys=[KEY_B])), state)


async def test_audience_defaults_to_server_name_and_separates_services() -> None:
    state = await mint(build_server("gx", RequestStateSecurity(keys=[KEY_A])))
    await assert_rejected(build_server("other-service", RequestStateSecurity(keys=[KEY_A])), state)
    explicit = RequestStateSecurity(keys=[KEY_A], audience="guarded-exchange")
    state2 = await mint(build_server("gx", explicit))
    await redeem(
        build_server("renamed", RequestStateSecurity(keys=[KEY_A], audience="guarded-exchange")), state2
    )


def test_short_keys_and_unnamed_server_with_policy_fail_fast() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        RequestStateSecurity(keys=[b"short"])
    with pytest.raises(ValueError, match="no name"):
        MCPServer(request_state_security=RequestStateSecurity(keys=[KEY_A]))


async def test_principal_binding_rejects_another_user() -> None:
    security = RequestStateSecurity(keys=[KEY_A], bind_principal=by_user)
    server = build_server("gx", security)
    token = current_user.set("alice")
    try:
        state = await mint(server)
        await redeem(server, state)
    finally:
        current_user.reset(token)
    token = current_user.set("bob")
    try:
        await assert_rejected(server, state)
    finally:
        current_user.reset(token)


async def test_ttl_expires_state() -> None:
    server = build_server("gx", RequestStateSecurity(keys=[KEY_A], ttl=0.05))
    state = await mint(server)
    time.sleep(0.1)  # noqa: ASYNC251 - the clock is all we need here
    await assert_rejected(server, state)
