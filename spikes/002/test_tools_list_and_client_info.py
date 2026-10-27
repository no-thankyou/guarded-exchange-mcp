"""Spike 002, Q3 + Q4: per-principal ``tools/list`` and where ``clientInfo``/capabilities live.

Q3 source facts (``mcp`` 2.3.0, 2026-10-27): ``MCPServer._handle_list_tools(ctx, params)`` returns
``ListToolsResult(tools=await self.list_tools())``; ``list_tools()`` is public, takes no context and
is resolved by normal method lookup, so a subclass can override it. The principal is read from a
context variable (``get_access_token()`` on HTTP, the configured local user on stdio). Results of
cacheable methods default to ``ttlMs=0, cacheScope="private"`` (``mcp_types.CacheableResult``).

Q4 source facts: on 2026-07-28 each request builds a ``Connection.from_envelope`` from its ``_meta``
(``mcp/server/connection.py``); ``client_params.client_info`` is set when the request carried both
``clientInfo`` and ``clientCapabilities``; ``ctx.client_capabilities`` needs only the latter.
"""

import contextvars
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context, RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    Implementation,
    InputRequiredResult,
    InputResponses,
    Tool,
)

TRADING_TOOLS = frozenset({"place_order"})
current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user", default=None)
PROFILES = {"alice": "trader", "reader": "read_only"}


def principal() -> str | None:
    """Product shape: verified bearer token on HTTP, otherwise the configured local user."""
    token = get_access_token()
    return token.subject if token is not None else current_user.get()


class PrincipalScopedServer(MCPServer[Any]):
    """Hides tools the caller's profile can never use. Not a security boundary: tools/call checks."""

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        user = principal()
        if user is not None and PROFILES.get(user) == "trader":
            return tools
        return [tool for tool in tools if tool.name not in TRADING_TOOLS]


def register_tools(mcp: MCPServer[Any]) -> None:
    @mcp.tool(name="get_ticker")
    async def get_ticker(symbol: str) -> str:
        return symbol

    @mcp.tool(name="place_order")
    async def place_order(symbol: str, ctx: Context) -> CallToolResult | InputRequiredResult:
        if ctx.input_responses is None:
            return InputRequiredResult(
                input_requests={
                    "confirm": ElicitRequest(
                        params=ElicitRequestFormParams(
                            message=f"Buy {symbol}?",
                            requested_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
                        )
                    )
                },
                request_state="pending:p-1",
            )
        return CallToolResult(
            content=[], structured_content={"user": principal(), "state": ctx.request_state}
        )

    @mcp.tool(name="whoami")
    async def whoami(ctx: Context) -> dict[str, Any]:
        params = ctx.session.client_params
        caps = ctx.client_capabilities
        meta = ctx.request_context.meta
        return {
            "protocol_version": ctx.protocol_version,
            "client_info": params.client_info.model_dump(exclude_none=True) if params else None,
            "elicitation_form": bool(caps and caps.elicitation and caps.elicitation.form),
            "meta_keys": sorted((meta or {}).keys()),
            "principal": principal(),
        }


def build(cls: type[MCPServer[Any]] = PrincipalScopedServer, **kwargs: Any) -> MCPServer[Any]:
    mcp = cls("spike-002", **kwargs)
    register_tools(mcp)
    return mcp


async def names(client: Client) -> set[str]:
    return {tool.name for tool in (await client.list_tools()).tools}


@pytest.mark.parametrize(("user", "expected_hidden"), [("alice", set()), ("reader", {"place_order"})])
async def test_list_tools_override_filters_by_principal(user: str, expected_hidden: set[str]) -> None:
    token = current_user.set(user)
    try:
        async with Client(build()) as client:
            listed = await client.session.list_tools()
            visible = await names(client)
            # Hidden is not forbidden: a direct tools/call still reaches the tool (use case must refuse).
            called = await client.call_tool("get_ticker" if expected_hidden else "whoami", {"symbol": "X"})
    finally:
        current_user.reset(token)
    assert {"get_ticker", "place_order", "whoami"} - visible == expected_hidden
    assert (listed.ttl_ms, listed.cache_scope) == (0, "private")
    assert called.is_error is False


async def test_middleware_alternative_also_filters_but_is_provisional() -> None:
    async def hide_trading(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method == "tools/list" and isinstance(result, dict):
            tools = [t for t in result["tools"] if t["name"] not in TRADING_TOOLS]
            return {**result, "tools": tools}
        return result

    async with Client(build(MCPServer, middleware=[hide_trading])) as client:
        assert await names(client) == {"get_ticker", "whoami"}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_client_info_and_capabilities_reach_the_tool(mode: str) -> None:
    async def accept(context: Any, params: Any) -> ElicitResult:
        return ElicitResult(action="accept")

    client = Client(
        build(),
        mode=mode,
        client_info=Implementation(name="spike-agent", version="1.2.3"),
        elicitation_callback=accept,
    )
    async with client:
        result = await client.call_tool("whoami", {})
    out = result.structured_content
    assert out is not None
    assert out["client_info"] == {"name": "spike-agent", "version": "1.2.3"}
    assert out["elicitation_form"] is True
    if mode == "auto":
        assert out["protocol_version"] == "2026-07-28"
        assert {"io.modelcontextprotocol/clientCapabilities", "io.modelcontextprotocol/clientInfo"} <= set(
            out["meta_keys"]
        )
    else:
        assert out["protocol_version"] == "2025-11-25"


# --- Real Streamable HTTP + bearer auth (ASGI in-process, no sockets) ---------------------------


class StaticTokens:
    """Opaque bearer tokens -> user id in ``subject`` (ADR-004 shape, hashes omitted)."""

    TOKENS: ClassVar[dict[str, str]] = {"t-alice": "alice", "t-reader": "reader"}

    async def verify_token(self, token: str) -> AccessToken | None:
        user = self.TOKENS.get(token)
        if user is None:
            return None
        return AccessToken(token=token, client_id="guarded-exchange", scopes=[], subject=user)


def http_server() -> MCPServer[Any]:
    return build(
        token_verifier=StaticTokens(),
        auth=AuthSettings(
            issuer_url="https://auth.invalid",
            resource_server_url="http://127.0.0.1:8000/mcp",
            validate_token_resource=False,
        ),
        request_state_security=RequestStateSecurity(keys=[b"k" * 32]),
    )


@asynccontextmanager
async def http_client_factory(mcp: MCPServer[Any]) -> AsyncIterator[Callable[[str], Client]]:
    app = mcp.streamable_http_app()
    async with mcp.session_manager.run():

        def connect(token: str) -> Client:
            http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
                headers={"Authorization": f"Bearer {token}"},
            )
            return Client(streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http))

        yield connect


async def test_http_principal_from_token_in_list_tools_and_tool() -> None:
    async with http_client_factory(http_server()) as connect:
        async with connect("t-reader") as client:
            assert await names(client) == {"get_ticker", "whoami"}
            who = await client.call_tool("whoami", {})
        async with connect("t-alice") as client:
            assert "place_order" in await names(client)
    assert who.structured_content is not None
    assert who.structured_content["principal"] == "reader"
    assert who.structured_content["protocol_version"] == "2026-07-28"


async def test_http_request_state_is_bound_to_token_principal_by_default() -> None:
    answers: InputResponses = {"confirm": ElicitResult(action="accept", content={"ok": True})}
    async with http_client_factory(http_server()) as connect:
        async with connect("t-alice") as alice:
            first = await alice.session.call_tool("place_order", {"symbol": "BTC"}, allow_input_required=True)
            assert isinstance(first, InputRequiredResult) and first.request_state is not None
            done = await alice.session.call_tool(
                "place_order", {"symbol": "BTC"}, input_responses=answers, request_state=first.request_state
            )
            assert done.structured_content == {"user": "alice", "state": "pending:p-1"}
        async with connect("t-reader") as reader:
            with pytest.raises(MCPError) as info:
                await reader.session.call_tool(
                    "place_order",
                    {"symbol": "BTC"},
                    input_responses=answers,
                    request_state=first.request_state,
                )
    assert info.value.error.code == -32602
