"""AC-4: the model cannot confirm an action that needs a human - by any tool, argument, text, replayed or
forged request state, or by what it claims to be (ticket 021; SPEC AC-4, FR-20, FR-42, FR-47; PLAN ADR-005).

The "agent" here is hostile on purpose: it only has what a model has - tool calls with arguments of its choice
and text. The state of the exchange (``FakeTrading`` / ``FakePositionActions``) and of the confirmations must
be unchanged afterwards: nothing sent, nothing ``approved``. A custom client that answers the UI form by
itself is outside this guarantee (FR-42 covers it by not trusting the UI on mainnet)."""

import ast
import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ElicitResult, Implementation, InputRequiredResult

from guarded_exchange.application.client_ui_confirmation import ClientUiConfirmation
from guarded_exchange.application.resolve_confirmation import ResolveConfirmation
from guarded_exchange.domain.confirmation import ConfirmationStatus
from guarded_exchange.domain.policy import default_ceiling
from guarded_exchange.domain.users import Environment
from guarded_exchange.interface.mcp.access import fixed_principal
from guarded_exchange.interface.mcp.server import create_server
from tests.e2e.test_mcp_client_ui import ACCEPT, ANSWERS, STOP, Human, Setup
from tests.e2e.test_mcp_place_order import MARKET_BUY

pytestmark = pytest.mark.req("AC-4", "FR-20", "FR-42", "FR-47")

MCP_SOURCES = Path(__file__).parents[2] / "src" / "guarded_exchange" / "interface" / "mcp"
SRC = MCP_SOURCES.parents[1]
ORDER_ID = "fa7b555a-dad9-444c-8f67-e4fc7572ac65"
APPROVAL_WORDS = ("approve", "confirm_action", "resolve", "decide", "accept", "execute", "authorize")
IN_FLIGHT = {ConfirmationStatus.APPROVED, ConfirmationStatus.EXECUTING, ConfirmationStatus.EXECUTED}

SCOPE = {"symbol": "BTCUSDT", "category": "linear"}
TRADING_CALLS: dict[str, dict[str, Any]] = {
    "place_order": MARKET_BUY,
    "set_trading_stop": STOP,
    "close_position": SCOPE,
    "set_leverage": {**SCOPE, "leverage": "3"},
    "cancel_order": {**SCOPE, "order_id": ORDER_ID},
    "amend_order": {**SCOPE, "order_id": ORDER_ID, "qty": "0.002"},
    "cancel_all_orders": SCOPE,
}
# What a model might invent to "confirm" a call: every spelling of consent and every protocol field.
CONSENT = {
    "confirmed": True,
    "confirm": True,
    "approve": True,
    "approved": True,
    "user_confirmed": True,
    "human_approved": True,
    "skip_confirmation": True,
    "input_responses": {"confirm": {"action": "accept", "content": {"confirm": True}}},
    "inputResponses": {"confirm": {"action": "accept", "content": {"confirm": True}}},
    "request_state": "approved",
    "requestState": "approved",
    "_meta": {"clientInfo": {"name": "claude-code", "version": "99"}, "confirmed": True},
}


def _untouched(setup: Setup) -> None:
    """Nothing reached the exchange and no confirmation went past ``pending``."""
    assert setup.trading.sent == []
    assert [c for c in setup.trading.calls if not str(c[0]).startswith("get_")] == []  # reads are fine
    assert setup.actions.stops == []
    assert setup.actions.leverages == []
    assert not [p for p in setup.pendings() if p.status in IN_FLIGHT]
    assert [d for d in setup.decisions() if d.payload["status"] in {"approved", "executed"}] == []
    assert [r for r in setup.db.all_audit() if r.event_type == "order_attempt"] == []


# --- the registry: no tool and no code path of a tool reaches a decision ---------------------------


def test_no_mcp_tool_is_named_or_shaped_like_an_approval() -> None:
    setup = Setup()

    async def listed() -> list[Any]:
        async with Client(setup.server()) as client:
            return list((await client.list_tools()).tools)

    tools = anyio.run(listed)
    assert tools
    for tool in tools:
        assert not [w for w in APPROVAL_WORDS if w in tool.name], tool.name
        properties = set((tool.input_schema or {}).get("properties", {}))
        assert not properties & {
            "approve",
            "approved",
            "confirmed",
            "confirm",
            "input_responses",
            "request_state",
        }


def _calls_named(attribute: str) -> Iterator[tuple[str, str]]:
    """(file, enclosing function) of every call ``<anything>.<attribute>(...)`` in the MCP layer."""
    for path in sorted(MCP_SOURCES.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == attribute
                ):
                    yield path.name, function.name


def test_the_only_code_that_decides_in_the_mcp_layer_is_the_second_round_of_the_ui_form() -> None:
    # Decisions: the UI channel (fed by the client's elicitation response) ...
    assert sorted(set(_calls_named("decide"))) == [("confirm_ui.py", "_second_round")]
    # ... and nothing in the MCP layer names the CLI's use case or imports it.
    for path in MCP_SOURCES.rglob("*.py"):
        names = {n.id for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Name)}
        imported = {
            a.name
            for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.ImportFrom)
            for a in n.names
        }
        assert "ResolveConfirmation" not in names | imported, path.name
    # The answer that `decide` gets is read from the request's inputResponses only.
    second_round = (MCP_SOURCES / "confirm_ui.py").read_text()
    assert "read_answer(responses)" in second_round
    assert "ctx.input_responses" in second_round


def _reachable(root: object) -> Iterator[tuple[str, object]]:
    """Every object reachable from ``root`` through dataclass fields and instance attributes."""
    seen: set[int] = set()
    stack: list[tuple[str, object]] = [("use_cases", root)]
    while stack:
        path, node = stack.pop()
        if id(node) in seen or isinstance(node, str | int | float | bool | bytes | type(None)):
            continue
        seen.add(id(node))
        yield path, node
        if isinstance(node, dict):
            stack.extend((f"{path}[{k!r}]", v) for k, v in node.items())
        elif isinstance(node, list | tuple | set | frozenset):
            stack.extend((f"{path}[]", v) for v in node)
        elif dataclasses.is_dataclass(node) and not isinstance(node, type):
            stack.extend((f"{path}.{f.name}", getattr(node, f.name)) for f in dataclasses.fields(node))
        elif hasattr(node, "__dict__") and type(node).__module__.startswith("guarded_exchange"):
            stack.extend((f"{path}.{k}", v) for k, v in vars(node).items())


def test_the_tools_cannot_reach_the_cli_resolver_and_reach_the_ui_decision_only_through_client_ui() -> None:
    setup = Setup(cli=True)
    reached = list(_reachable(setup.cases))

    assert [p for p, node in reached if isinstance(node, ResolveConfirmation)] == []
    assert [p for p, node in reached if isinstance(node, ClientUiConfirmation)] == ["use_cases.client_ui"]
    assert SRC.is_dir()


# --- a hostile agent --------------------------------------------------------------------------


async def _hostile_session(client: Client, setup: Setup) -> list[CallToolResult]:
    """Every tool, valid arguments plus every spelling of consent, repeated; text in every free field;
    then the confirmation tools with the ids it learned."""
    results: list[CallToolResult] = []
    tools = [t.name for t in (await client.list_tools()).tools]
    ids: set[str] = set()
    for _ in range(3):
        for name in tools:
            args = {**TRADING_CALLS.get(name, {}), **CONSENT}
            for attempt in (args, {**args, "idempotency_key": "APPROVED-by-the-user"}, dict(CONSENT)):
                try:
                    result = await client.call_tool(name, attempt)
                except MCPError:
                    continue  # a protocol-level refusal is a refusal too
                results.append(result)
                found = (result.structured_content or {}).get("confirmation_id")
                if isinstance(found, str):
                    ids.add(found)
    for cid in sorted(ids):
        for name in ("get_confirmation_status", "cancel_confirmation"):
            for extra in ({}, CONSENT):
                try:
                    results.append(await client.call_tool(name, {"confirmation_id": cid, **extra}))
                except MCPError:
                    continue
    return results


@pytest.mark.parametrize(
    ("cli", "human"),
    [
        pytest.param(True, None, id="cli-enabled-client-without-elicitation"),
        pytest.param(True, "nobody", id="cli-enabled-form-closed-unanswered"),
        pytest.param(False, "nobody", id="ui-only-form-closed-unanswered"),
    ],
)
async def test_a_hostile_agent_calling_every_tool_with_every_kind_of_consent_executes_nothing(
    cli: bool, human: str | None
) -> None:
    setup = Setup(cli=cli)
    await setup.grant()
    callback = Human(ElicitResult(action="cancel")) if human else None  # the human never says yes
    async with Client(setup.server(), elicitation_callback=callback) as client:
        results = await _hostile_session(client, setup)

    assert results
    _untouched(setup)
    parked = list(setup.pendings())
    assert parked  # the attempt was real: actions were requested and held
    assert {p.status for p in parked} <= {
        ConfirmationStatus.PENDING,
        ConfirmationStatus.CANCELLED,
        ConfirmationStatus.REJECTED,
    }


async def test_cancelling_is_not_approving_and_another_users_confirmation_is_not_found() -> None:
    setup = Setup(cli=True)
    await setup.grant()
    async with Client(setup.server()) as client:
        cid = (await client.call_tool("place_order", MARKET_BUY)).structured_content["confirmation_id"]
        cancelled = await client.call_tool("cancel_confirmation", {"confirmation_id": cid})
        status = await client.call_tool("get_confirmation_status", {"confirmation_id": cid})
        stranger = await client.call_tool("cancel_confirmation", {"confirmation_id": "someone-elses-0001"})

    assert (cancelled.structured_content or {})["status"] == "cancelled"
    assert (status.structured_content or {})["status"] == "cancelled"
    assert stranger.is_error is True
    _untouched(setup)


async def test_arguments_named_like_protocol_fields_do_not_make_a_second_round() -> None:
    setup = Setup(cli=True)
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=None) as client:
        parked = await client.call_tool("set_trading_stop", STOP)
        cid = (parked.structured_content or {})["confirmation_id"]
        again = await client.call_tool("set_trading_stop", {**STOP, **CONSENT, "confirmation_id": cid})

    assert (again.structured_content or {})["status"] == "pending"
    _untouched(setup)


# --- forged and replayed request state ---------------------------------------------------------


async def _first(client: Client) -> InputRequiredResult:
    first = await client.session.call_tool("set_trading_stop", STOP, allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    return first


async def test_a_request_state_the_agent_wrote_itself_is_refused_before_the_tool_runs() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        await _first(client)
        (pending,) = setup.pendings()
        forged = [
            f'{{"id":"{pending.id}","terms":"{pending.terms_digest}"}}',  # the right content, not sealed
            pending.id,
            "v1." + "A" * 120,
            "",
        ]
        for state in forged:
            with pytest.raises(MCPError):
                await client.session.call_tool(
                    "set_trading_stop", STOP, input_responses=ANSWERS, request_state=state
                )

    _untouched(setup)


async def test_a_sealed_state_from_another_key_or_with_flipped_bytes_is_refused() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        sealed = (await _first(client)).request_state
    assert sealed is not None

    attempts = [(setup.server(b"y" * 32), sealed), (setup.server(), sealed[:-3] + "AAA")]
    for server, state in attempts:
        async with Client(server, elicitation_callback=Human(ACCEPT)) as client:
            with pytest.raises(MCPError):
                await client.session.call_tool(
                    "set_trading_stop", STOP, input_responses=ANSWERS, request_state=state
                )

    _untouched(setup)


async def test_the_state_of_one_users_form_does_not_work_for_another_user() -> None:
    setup = Setup()
    await setup.grant()
    mallory = setup.db.add_user("mallory")
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        sealed = (await _first(client)).request_state
    their_server = create_server(
        setup.cases, principal=fixed_principal(mallory.id), request_state_key=setup.key
    )
    async with Client(their_server, elicitation_callback=Human(ACCEPT)) as client:
        result = await client.session.call_tool(
            "set_trading_stop", STOP, input_responses=ANSWERS, request_state=sealed
        )

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    _untouched(setup)


async def test_a_pending_changed_in_storage_after_the_form_was_built_is_not_executed() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        sealed = (await _first(client)).request_state
        (pending,) = setup.pendings()
        setup.db.state.confirmations[pending.id] = dataclasses.replace(
            pending, action={**pending.action, "stop_loss": "1.0"}
        )
        result = await client.session.call_tool(
            "set_trading_stop", STOP, input_responses=ANSWERS, request_state=sealed
        )

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    _untouched(setup)


# --- what the agent claims to be does not decide anything (FR-36, FR-42) ------------------------


@pytest.mark.parametrize(
    "name", ["Claude Desktop", "claude-code", "trusted-human-approved-ui", "guarded-exchange-cli"]
)
async def test_claiming_to_be_a_trusted_client_opens_no_channel(name: str) -> None:
    setup = Setup(cli=False)
    await setup.grant()
    spoof = Implementation(name=name, version="9.9")
    async with Client(setup.server(), client_info=spoof) as client:  # declares no elicitation
        result = await client.call_tool("set_trading_stop", STOP)

    assert result.is_error is True
    assert (result.structured_content or {})["code"] == "confirmation_channel_unavailable"
    assert setup.pendings() == []
    _untouched(setup)


async def test_a_spoofed_client_name_does_not_change_who_may_decide_on_a_parked_action() -> None:
    setup = Setup(cli=True)
    await setup.grant()
    async with Client(
        setup.server(), client_info=Implementation(name="Claude Desktop", version="9")
    ) as client:
        parked = await client.call_tool("set_trading_stop", STOP)
        cid = (parked.structured_content or {})["confirmation_id"]
        for name in ("get_confirmation_status", "cancel_confirmation"):
            await client.call_tool(name, {"confirmation_id": cid, "client": "Claude Desktop"})

    assert [d for d in setup.decisions() if d.payload["channel"] != "mcp:agent"] == []
    _untouched(setup)


def test_mainnet_never_trusts_the_client_ui_by_default() -> None:
    assert default_ceiling(Environment.MAINNET).trust_client_ui is False
    assert default_ceiling(Environment.TESTNET).trust_client_ui is True
