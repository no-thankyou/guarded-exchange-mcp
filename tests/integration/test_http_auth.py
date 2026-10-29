"""Remote mode over real Streamable HTTP with bearer tokens (ticket 026, PLAN ADR-003/004).

The whole ASGI stack of the SDK runs in-process (``httpx2.ASGITransport``, no sockets): Host/Origin
checks, the bearer middleware calling our ``BearerTokenVerifier`` on every request, the
2026-07-28 request ladder, ``GuardedMCPServer``. Storage is a real SQLite file; the exchange is a
fake. Auth does not exist on stdio and in-memory transports, so these tests must use HTTP.
"""

import io
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, Implementation
from pydantic import Field
from starlette.applications import Starlette

from guarded_exchange import bootstrap
from guarded_exchange.application.admin import CreateUser, IssueAccessToken, RevokeAccessToken
from guarded_exchange.application.auth import AuthenticateBearerToken
from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.domain.access_tokens import TOKEN_PREFIX, hash_token
from guarded_exchange.domain.policy import Permission, PolicyPatch, Profile
from guarded_exchange.infrastructure.clock import SystemClock, UuidGenerator
from guarded_exchange.infrastructure.db.uow import SqlUnitOfWorkFactory, create_sqlite_async_engine
from guarded_exchange.infrastructure.logging import configure_logging
from guarded_exchange.infrastructure.tokens import SecretTokenGenerator
from guarded_exchange.interface.mcp.access import TOOL_PERMISSIONS
from guarded_exchange.interface.mcp.guarded_server import GuardedMCPServer
from guarded_exchange.interface.mcp.http import (
    BearerTokenVerifier,
    auth_settings,
    create_http_server,
    http_app,
    token_principal,
)
from guarded_exchange.interface.mcp.server import create_server
from tests.e2e.support import use_cases
from tests.fakes import FakeCipher, FakeExchange, InMemoryDatabase, make_ticker
from tests.integration.conftest import BUSY_TIMEOUT_MS, make_settings

BASE = "http://127.0.0.1:8000"
URL = f"{BASE}/mcp"
LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*"]
LOCAL_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*"]
WITH_TRADING_TOOL = {**TOOL_PERMISSIONS, "place_order": Permission.TRADE}


class Tenants:
    """Two users on one server: alice may trade, bob is read-only (the default)."""

    def __init__(self, uow: SqlUnitOfWorkFactory) -> None:
        self.uow = uow
        self.secrets: dict[str, str] = {}
        self.token_ids: dict[str, str] = {}
        self.user_ids: dict[str, str] = {}

    async def setup(self) -> "Tenants":
        clock = SystemClock()
        for name in ("alice", "bob"):
            user = await CreateUser(self.uow, clock, UuidGenerator())(name)
            issued = await IssueAccessToken(self.uow, clock, UuidGenerator(), SecretTokenGenerator())(name)
            self.user_ids[name] = user.id
            self.secrets[name] = issued.secret
            self.token_ids[name] = issued.token_id
        await SetCeiling(self.uow, clock)("alice", PolicyPatch(profile=Profile.TRADE))
        return self

    async def revoke_token(self, name: str) -> None:
        await RevokeAccessToken(self.uow, SystemClock())(self.token_ids[name])


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Tests here point the root logger at their own stream; give later tests the original back."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


@pytest.fixture
async def tenants(uow: SqlUnitOfWorkFactory) -> Tenants:
    return await Tenants(uow).setup()


@pytest.fixture
def exchange() -> FakeExchange:
    return FakeExchange(tickers={("BTCUSDT", ticker.category): ticker for ticker in [make_ticker()]})


def production_server(uow: SqlUnitOfWorkFactory, exchange: FakeExchange) -> GuardedMCPServer:
    return create_http_server(use_cases(uow, exchange), AuthenticateBearerToken(uow), public_url=BASE)


def server_with_trading_stub(
    uow: SqlUnitOfWorkFactory, exchange: FakeExchange, calls: list[str], *, header_annotated: bool = False
) -> GuardedMCPServer:
    """The production wiring plus a stub ``place_order`` (TRADE) until ticket 011 brings the real one."""
    mcp = create_server(
        use_cases(uow, exchange),
        principal=token_principal,
        tool_permissions=WITH_TRADING_TOOL,
        token_verifier=BearerTokenVerifier(AuthenticateBearerToken(uow)),
        auth=auth_settings(BASE),
    )
    # `x-mcp-header`: the SDK checks the matching `Mcp-Param-Symbol` header BEFORE dispatch.
    extra: dict[str, Any] = {"x-mcp-header": "Symbol"} if header_annotated else {}

    @mcp.tool(name="place_order", description="Stub trading tool.")
    async def place_order(
        symbol: Annotated[str, Field(pattern=r"^[A-Z]{3,20}$", json_schema_extra=extra)],
        qty: Annotated[int, Field(gt=0)],
    ) -> str:
        calls.append(symbol)
        return "placed"

    return mcp


class Http:
    """Clients of one running ASGI app: MCP clients with a bearer token, or raw HTTP."""

    def __init__(self, app: Starlette) -> None:
        self._app = app

    def raw(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self._app), base_url=BASE)

    def connect(
        self,
        secret: str | None,
        *,
        client_info: Implementation | None = None,
        statuses: list[int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Client:
        """``statuses`` collects the HTTP status of every response the client receives."""
        headers = dict(headers or {})
        if secret is not None:
            headers["Authorization"] = f"Bearer {secret}"

        async def record(response: httpx2.Response) -> None:
            if statuses is not None:
                statuses.append(response.status_code)

        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self._app),
            base_url=BASE,
            headers=headers,
            event_hooks={"response": [record]},
        )
        transport = streamable_http_client(URL, http_client=http)
        return Client(transport, client_info=client_info) if client_info else Client(transport)


@asynccontextmanager
async def serving(mcp: GuardedMCPServer) -> AsyncIterator[Http]:
    """The production ASGI app; the SDK session manager runs as uvicorn's lifespan would run it."""
    app = http_app(mcp, allowed_hosts=LOCAL_HOSTS, allowed_origins=LOCAL_ORIGINS)
    async with mcp.session_manager.run():
        yield Http(app)


LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CALL_TICKER = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "get_ticker", "arguments": {"symbol": "BTCUSDT", "category": "linear"}},
}


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None, result
    return dict(result.structured_content)


# --- AC 1: 401 without a valid token ----------------------------------------------------


@pytest.mark.req("FR-29", "FR-33", "NFR-1")
@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic YWxpY2U6c2VjcmV0",
        "Bearer not-a-token",
        f"Bearer {TOKEN_PREFIX}{'A' * 43}",  # well-formed, never issued
        "Bearer {alice_active}x",  # an active token with one extra character
        "Bearer {alice_active_truncated}",  # ... or one character short
        "Bearer {alice_revoked}",
        "Bearer {bob_user_revoked}",
    ],
)
@pytest.mark.parametrize("body", [LIST_TOOLS, CALL_TICKER], ids=["tools_list", "tools_call"])
async def test_requests_without_a_valid_token_get_401_and_reach_nothing(
    uow: SqlUnitOfWorkFactory,
    db_path: Path,
    tenants: Tenants,
    exchange: FakeExchange,
    *,
    authorization: str | None,
    body: dict[str, Any],
) -> None:
    await tenants.revoke_token("alice")
    active = (
        await IssueAccessToken(uow, SystemClock(), UuidGenerator(), SecretTokenGenerator())("alice")
    ).secret
    assert await AuthenticateBearerToken(uow).authenticate(active) == tenants.user_ids["alice"]
    with sqlite3.connect(db_path) as conn:  # revoking a user is ticket 027; the state is what matters
        conn.execute("UPDATE users SET status = 'revoked' WHERE name = 'bob'")
    secrets = {
        "alice_active": active,
        "alice_active_truncated": active[:-1],
        "alice_revoked": tenants.secrets["alice"],
        "bob_user_revoked": tenants.secrets["bob"],
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization.format(**secrets)

    async with serving(production_server(uow, exchange)) as server, server.raw() as http:
        response = await http.post("/mcp", json=body, headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer ")
    assert response.json()["error"] == "invalid_token"
    assert len(response.headers["x-request-id"]) == 32  # correlates with the server log line
    assert exchange.calls == []  # no tool ran


@pytest.mark.req("FR-29")
async def test_revoking_a_token_applies_to_the_next_request_of_an_open_client(
    uow: SqlUnitOfWorkFactory, tenants: Tenants, exchange: FakeExchange
) -> None:
    statuses: list[int] = []
    async with (
        serving(production_server(uow, exchange)) as server,
        server.connect(tenants.secrets["alice"], statuses=statuses) as alice,
        server.connect(tenants.secrets["bob"]) as bob,
    ):
        before = await alice.call_tool("get_my_policy", {})
        await tenants.revoke_token("alice")
        statuses.clear()
        with pytest.raises(MCPError):  # the client reports the HTTP 401 as an error response
            await alice.call_tool("get_my_policy", {})
        other = await bob.call_tool("get_my_policy", {})  # other tokens are unaffected

    assert _structured(before)["profile"] == "trade"
    assert statuses == [401]
    assert _structured(other)["profile"] == "read_only"


@pytest.mark.req("FR-29")
async def test_a_user_revoked_after_connecting_is_refused_on_the_next_request(
    uow: SqlUnitOfWorkFactory, db_path: Path, tenants: Tenants, exchange: FakeExchange
) -> None:
    async with serving(production_server(uow, exchange)) as server, server.raw() as http:
        async with server.connect(tenants.secrets["bob"]) as bob:
            assert {t.name for t in (await bob.list_tools()).tools} >= {"get_ticker", "get_my_policy"}
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE users SET status = 'revoked' WHERE name = 'bob'")
        response = await http.post(
            "/mcp",
            json=LIST_TOOLS,
            headers={
                "Authorization": f"Bearer {tenants.secrets['bob']}",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code == 401


@pytest.mark.req("NFR-1")
@pytest.mark.usefixtures("restore_logging")
async def test_a_failing_token_check_refuses_with_401_and_logs_no_secret(
    uow: SqlUnitOfWorkFactory, tenants: Tenants, exchange: FakeExchange
) -> None:
    class BrokenAuthenticator:
        async def authenticate(self, credential: str) -> str | None:
            raise RuntimeError(f"database is gone while checking {credential}")

    logs = io.StringIO()
    configure_logging("INFO", stream=logs)
    mcp = create_http_server(use_cases(uow, exchange), BrokenAuthenticator(), public_url=BASE)
    async with serving(mcp) as server, server.raw() as http:
        response = await http.post(
            "/mcp", json=LIST_TOOLS, headers={"Authorization": f"Bearer {tenants.secrets['alice']}"}
        )

    assert response.status_code == 401
    events = [json.loads(line) for line in logs.getvalue().splitlines()]
    failed = next(e for e in events if e["event"] == "bearer_check_failed")
    assert failed["error_type"] == "RuntimeError"
    assert failed["http_request_id"] == response.headers["x-request-id"]
    assert tenants.secrets["alice"] not in logs.getvalue()


@pytest.mark.req("NFR-3")
@pytest.mark.usefixtures("restore_logging")
async def test_rejected_and_accepted_requests_are_logged_with_their_http_request_id(
    uow: SqlUnitOfWorkFactory, tenants: Tenants, exchange: FakeExchange
) -> None:
    logs = io.StringIO()
    configure_logging("INFO", stream=logs)
    async with serving(production_server(uow, exchange)) as server, server.raw() as http:
        rejected = await http.post(
            "/mcp", json=LIST_TOOLS, headers={"Authorization": f"Bearer {TOKEN_PREFIX}{'A' * 43}"}
        )
        async with server.connect(tenants.secrets["alice"]) as alice:
            await alice.call_tool("get_my_policy", {})

    events = [json.loads(line) for line in logs.getvalue().splitlines()]
    [denied] = [e for e in events if e["event"] == "bearer_rejected"]
    [tool] = [e for e in events if e["event"] == "tool_ok"]
    assert denied["http_request_id"] == rejected.headers["x-request-id"]
    assert len(tool["http_request_id"]) == 32
    assert tool["http_request_id"] != denied["http_request_id"]
    assert tool["request_id"] != tool["http_request_id"]  # the per-tool-call id stays separate


@pytest.mark.req("NFR-1")
async def test_unknown_host_header_is_refused_before_mcp(
    uow: SqlUnitOfWorkFactory, tenants: Tenants, exchange: FakeExchange
) -> None:
    """DNS rebinding protection (ADR-003): a Host outside ``allowed_hosts`` never reaches MCP."""
    async with serving(production_server(uow, exchange)) as server, server.raw() as http:
        response = await http.post(
            "/mcp",
            json=CALL_TICKER,
            headers={
                "Host": "evil.example",
                "Authorization": f"Bearer {tenants.secrets['alice']}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 421
    assert exchange.calls == []


async def test_auth_runs_before_the_host_check(uow: SqlUnitOfWorkFactory, exchange: FakeExchange) -> None:
    """Order of the SDK stack: bearer middleware (Starlette) wraps the transport, whose first
    step is the Host/Origin check. An unauthenticated request to a wrong Host gets 401, not 421."""
    async with serving(production_server(uow, exchange)) as server, server.raw() as http:
        response = await http.post("/mcp", json=CALL_TICKER, headers={"Host": "evil.example"})

    assert response.status_code == 401


# --- legacy protocol 2025-11-25 (sessions) ----------------------------------------------

LEGACY = "2025-11-25"
JSON_AND_SSE = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _jsonrpc(response: httpx2.Response) -> dict[str, Any]:
    """The JSON-RPC message of a plain JSON or a single-event SSE response."""
    if response.text.startswith("{"):
        return dict(response.json())
    data = next(line[5:] for line in response.text.splitlines() if line.startswith("data:"))
    return dict(json.loads(data))


@pytest.mark.req("FR-29", "FR-33")
async def test_legacy_session_is_bound_to_its_token_user_and_revocation_applies(
    uow: SqlUnitOfWorkFactory, tenants: Tenants, exchange: FakeExchange
) -> None:
    """Regression for the 2025-11-25 path (``Mcp-Session-Id``): the SDK binds a session to the
    principal that created it, so another user's token cannot ride it (404), and a revoked token
    is refused on the next request of its own session (401)."""

    def headers(user: str, session: str | None = None) -> dict[str, str]:
        out = {**JSON_AND_SSE, "Authorization": f"Bearer {tenants.secrets[user]}"}
        if session is not None:  # after initialize the legacy client sends the version and session
            out |= {"MCP-Protocol-Version": LEGACY, "Mcp-Session-Id": session}
        return out

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LEGACY,
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_my_policy", "arguments": {}},
    }
    async with serving(production_server(uow, exchange)) as server, server.raw() as http:
        # No MCP-Protocol-Version header on initialize: that is what routes to the legacy handshake.
        opened = await http.post("/mcp", json=initialize, headers=headers("alice"))
        session = opened.headers["mcp-session-id"]
        acked = await http.post("/mcp", json=initialized, headers=headers("alice", session))
        own = await http.post("/mcp", json=call, headers=headers("alice", session))
        hijack = await http.post("/mcp", json=call, headers=headers("bob", session))
        await tenants.revoke_token("alice")
        after_revoke = await http.post("/mcp", json=call, headers=headers("alice", session))

    assert (opened.status_code, acked.status_code, own.status_code) == (200, 202, 200)
    assert _jsonrpc(opened)["result"]["protocolVersion"] == LEGACY
    assert json.loads(_jsonrpc(own)["result"]["content"][0]["text"])["profile"] == "trade"
    assert hijack.status_code == 404  # "Session not found": nothing about alice leaks to bob
    assert "trade" not in hijack.text
    assert after_revoke.status_code == 401


# --- AC 1: the principal comes from the token only --------------------------------------


@pytest.mark.req("FR-33")
@pytest.mark.parametrize(("caller", "impersonated"), [("alice", "bob"), ("bob", "alice")])
async def test_principal_is_not_replaced_by_headers_client_info_or_arguments(
    uow: SqlUnitOfWorkFactory, tenants: Tenants, exchange: FakeExchange, caller: str, impersonated: str
) -> None:
    victim_id = tenants.user_ids[impersonated]
    spoofing_headers = {
        "X-User-Id": victim_id,
        "X-Forwarded-User": impersonated,
        "X-Remote-User": impersonated,
        "X-MCP-Principal": victim_id,
    }
    async with serving(production_server(uow, exchange)) as server:
        client = server.connect(
            tenants.secrets[caller],
            client_info=Implementation(name=impersonated, version=victim_id),
            headers=spoofing_headers,
        )
        async with client:
            policy = await client.call_tool(
                "get_my_policy", {"user_id": victim_id, "user": impersonated, "principal": victim_id}
            )

    expected = "trade" if caller == "alice" else "read_only"
    assert _structured(policy)["profile"] == expected


@pytest.mark.req("FR-33")
async def test_two_tokens_on_one_server_see_their_own_policy(
    uow: SqlUnitOfWorkFactory, tenants: Tenants, exchange: FakeExchange
) -> None:
    async with (
        serving(production_server(uow, exchange)) as server,
        server.connect(tenants.secrets["alice"]) as alice,
        server.connect(tenants.secrets["bob"]) as bob,
    ):
        alice_policy = await alice.call_tool("get_my_policy", {})
        bob_policy = await bob.call_tool("get_my_policy", {})
        ticker = await bob.call_tool("get_ticker", {"symbol": "BTCUSDT", "category": "linear"})

    assert (_structured(alice_policy)["profile"], _structured(bob_policy)["profile"]) == (
        "trade",
        "read_only",
    )
    assert not ticker.is_error


# --- review note from 006: SDK validation before the gate on Streamable HTTP ------------


@pytest.mark.req("FR-11", "FR-33")
async def test_forbidden_hidden_tool_over_http_is_refused_before_any_argument_check(
    uow: SqlUnitOfWorkFactory, tenants: Tenants, exchange: FakeExchange
) -> None:
    """A read-only user calling the hidden trading tool with bad arguments and stray
    ``Mcp-Param-*`` headers gets ``permission_denied``, not a validation error that would reveal
    the tool's input schema."""
    calls: list[str] = []
    mcp = server_with_trading_stub(uow, exchange, calls)
    async with serving(mcp) as server:
        async with server.connect(
            tenants.secrets["bob"], headers={"Mcp-Param-Symbol": "x", "Mcp-Param-Qty": "-1"}
        ) as bob:
            listed = {t.name for t in (await bob.list_tools()).tools}
            results = [
                await bob.call_tool("place_order", args)
                for args in ({}, {"symbol": "bad", "qty": -1}, {"symbol": "BTCUSDT", "qty": 1})
            ]
        async with server.connect(tenants.secrets["alice"]) as alice:
            invalid = await alice.call_tool("place_order", {"symbol": "bad", "qty": 1})

    assert "place_order" not in listed
    for result in results:
        assert result.is_error
        assert _structured(result)["code"] == "permission_denied"
        assert "symbol" not in json.dumps(_structured(result))
    assert calls == []
    assert _structured(invalid)["code"] == "invalid_params"  # alice may call it: schema errors are hers


def test_no_tool_declares_an_mcp_param_header(uow: SqlUnitOfWorkFactory, exchange: FakeExchange) -> None:
    """Guards the SDK behaviour shown below: our tools must never use ``x-mcp-header``."""
    mcp = production_server(uow, exchange)

    def walk(node: object) -> Iterator[object]:
        yield node
        if isinstance(node, dict):
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    for tool in mcp._tool_manager.list_tools():
        assert not any(isinstance(n, dict) and "x-mcp-header" in n for n in walk(tool.parameters)), tool.name


async def test_canary_sdk_checks_mcp_param_headers_of_hidden_tools_before_the_gate(
    uow: SqlUnitOfWorkFactory, tenants: Tenants, exchange: FakeExchange
) -> None:
    """Why the guard above exists (``mcp`` 2.3.0, ``_streamable_http_modern._mcp_param_rejection``):
    ``MCPServer._tool_input_schema`` finds a tool regardless of visibility, and the header check
    runs before dispatch. A forbidden caller would get a protocol error (HTTP 400, -32001) that
    names the annotated argument instead of ``permission_denied``. If this starts failing, the SDK
    changed; re-check before allowing ``x-mcp-header``."""
    calls: list[str] = []
    mcp = server_with_trading_stub(uow, exchange, calls, header_annotated=True)
    async with serving(mcp) as server, server.connect(tenants.secrets["bob"]) as bob:
        with pytest.raises(MCPError) as leaked:
            await bob.call_tool("place_order", {"symbol": "BTCUSDT", "qty": 1})

    assert "Mcp-Param-Symbol" in leaked.value.error.message
    assert calls == []


# --- AC 2: the token is stored only as a hash -------------------------------------------


def _all_bytes(db_path: Path) -> bytes:
    return b"".join(p.read_bytes() for p in db_path.parent.glob(db_path.name + "*"))


async def _use_over_http(db_path: Path, secret: str) -> tuple[CallToolResult, int]:
    """One accepted call with the token and one refused request with a corrupted copy of it."""
    engine = create_sqlite_async_engine(db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    uow = SqlUnitOfWorkFactory(engine)
    try:
        async with serving(production_server(uow, FakeExchange())) as server:
            async with server.connect(secret) as client:
                accepted = await client.call_tool("get_my_policy", {})
            async with server.raw() as http:
                refused = await http.post(
                    "/mcp", json=LIST_TOOLS, headers={"Authorization": f"Bearer {secret}x"}
                )
    finally:
        await engine.dispose()
    return accepted, refused.status_code


@pytest.mark.req("FR-29", "NFR-3")
@pytest.mark.usefixtures("restore_logging")
def test_token_is_stored_only_as_its_hash_and_never_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through the real CLI functions of the composition root, then used over HTTP, DEBUG logs on."""
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    env = tmp_path / "gx.env"
    env.write_text(
        f"GX_DB_PATH={settings.db_path}\nGX_MASTER_KEY_FILE={settings.master_key_file}\nGX_LOG_LEVEL=DEBUG\n"
    )

    bootstrap.create_user(env, "alice")
    issued = bootstrap.issue_token(env, "alice")
    accepted, refused = anyio.run(_use_over_http, settings.db_path, issued.secret)
    logs = capsys.readouterr().err

    secret = issued.secret
    with sqlite3.connect(settings.db_path) as conn:
        rows = conn.execute("SELECT id, token_hash FROM access_tokens").fetchall()
        dump = "\n".join(conn.iterdump())
    assert rows == [(issued.token_id, hash_token(secret))]
    assert secret[len(TOKEN_PREFIX) :] not in dump  # not even without the prefix
    assert secret.encode() not in _all_bytes(settings.db_path)  # incl. the WAL file
    assert issued.token_id in dump  # the journal references the token by its id
    assert (not accepted.is_error, refused) == (True, 401)
    assert "bearer_rejected" in logs  # the HTTP part did log, at DEBUG level
    assert secret[len(TOKEN_PREFIX) :] not in logs


# --- composition root: `serve --transport http` -----------------------------------------


@pytest.mark.req("FR-29", "FR-33")
@pytest.mark.usefixtures("restore_logging")
def test_serve_http_wires_storage_tokens_and_transport_security(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bootstrap.serve("http")`` up to the ASGI server; uvicorn is replaced by a driver that
    runs the app's lifespan (as uvicorn does) and talks to it in-process."""
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    env = tmp_path / "gx.env"
    env.write_text(
        f"GX_DB_PATH={settings.db_path}\nGX_MASTER_KEY_FILE={settings.master_key_file}\n"
        'GX_HTTP_PORT=9123\nGX_HTTP_ALLOWED_HOSTS=["mcp.internal:9123"]\n'
        'GX_HTTP_ALLOWED_ORIGINS=["https://mcp.internal"]\n'
    )
    bootstrap.create_user(env, "alice")
    secret = bootstrap.issue_token(env, "alice").secret
    seen: dict[str, Any] = {}

    async def drive(app: Starlette, *, host: str, port: int) -> None:
        seen["bind"] = (host, port)
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://mcp.internal:9123") as http:
                seen["no_token"] = (await http.post("/mcp", json=LIST_TOOLS)).status_code
                seen["loopback_host"] = (
                    await http.post(
                        "/mcp",
                        json=LIST_TOOLS,
                        headers={"Host": "127.0.0.1:9123", "Authorization": f"Bearer {secret}"},
                    )
                ).status_code
            client_http = httpx2.AsyncClient(
                transport=transport,
                base_url="http://mcp.internal:9123",
                headers={"Authorization": f"Bearer {secret}"},
            )
            async with Client(
                streamable_http_client("http://mcp.internal:9123/mcp", http_client=client_http)
            ) as c:
                seen["policy"] = _structured(await c.call_tool("get_my_policy", {}))["profile"]

    monkeypatch.setattr(bootstrap, "serve_http", drive)
    bootstrap.serve("http", env)

    assert seen == {
        "bind": ("127.0.0.1", 9123),
        "no_token": 401,
        "loopback_host": 421,  # only the configured Host is accepted
        "policy": "read_only",
    }


def test_app_server_needs_exactly_one_principal_source(tmp_path: Path) -> None:
    async def build(**kwargs: Any) -> None:
        async with bootstrap.app_server(
            make_settings(tmp_path), uow=InMemoryDatabase(), cipher=FakeCipher(), **kwargs
        ):
            pass  # pragma: no cover - never reached

    with pytest.raises(ValueError, match="exactly one"):
        anyio.run(build)
    with pytest.raises(ValueError, match="exactly one"):
        anyio.run(
            lambda: build(principal=lambda: "u", authenticator=AuthenticateBearerToken(InMemoryDatabase()))
        )


def _http_env(tmp_path: Path, extra: str) -> Path:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    env = tmp_path / "gx.env"
    env.write_text(f"GX_DB_PATH={settings.db_path}\nGX_MASTER_KEY_FILE={settings.master_key_file}\n{extra}\n")
    return env


@pytest.mark.req("NFR-1")
@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("host", ["http://0.0.0.0", "0.0.0.0:8000", "two words"])
def test_serve_http_with_a_bad_bind_host_exits_2_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    started: list[str] = []

    async def never(app: Starlette, *, host: str, port: int) -> None:  # pragma: no cover - must not run
        started.append(host)

    monkeypatch.setattr(bootstrap, "serve_http", never)
    env = _http_env(tmp_path, f"GX_HTTP_HOST={host}")

    with pytest.raises(SystemExit) as exit_info:
        bootstrap.serve("http", env)

    err = capsys.readouterr().err
    assert exit_info.value.code == 2
    assert "guarded-exchange: configuration error: http_host" in err
    assert "Traceback" not in err
    assert started == []


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(("host", "warned"), [("0.0.0.0", True), ("127.0.0.1", False)])  # noqa: S104
def test_binding_beyond_loopback_warns_about_tls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    warned: bool,
) -> None:
    async def stop_at_once(app: Starlette, *, host: str, port: int) -> None:
        return None

    monkeypatch.setattr(bootstrap, "serve_http", stop_at_once)

    bootstrap.serve("http", _http_env(tmp_path, f"GX_HTTP_HOST={host}"))

    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    warnings = [e for e in lines if e["event"] == "http_not_loopback"]
    assert bool(warnings) is warned
    if warned:
        assert warnings[0]["level"] == "warning"
        assert "TLS" in warnings[0]["hint"]
