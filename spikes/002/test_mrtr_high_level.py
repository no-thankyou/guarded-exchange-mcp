"""Spike 002, Q1 + Q5: MRTR (``InputRequiredResult``) from a high-level ``@mcp.tool``.

Facts checked against the installed ``mcp`` 2.3.0 (protocol 2026-07-28), 2026-10-27:

* a tool declared ``-> Annotated[CallToolResult, Out] | InputRequiredResult`` registers, keeps
  ``outputSchema`` from ``Out`` and may return ``InputRequiredResult`` from its body
  (``mcp/server/mcpserver/utilities/func_metadata.py`` strips the arm; ``convert_result``
  passes it through);
* on the retry the tool reads ``ctx.input_responses`` / ``ctx.request_state`` (plaintext);
* the in-process ``Client`` resolves MRTR transparently through ``elicitation_callback`` and
  only declares ``elicitation`` when a callback is given;
* on a legacy (2025-11-25) connection the same return value is a protocol error (-32603), but
  the tool can branch on ``ctx.protocol_version`` and use push ``ctx.elicit`` there instead.

Run: ``uv run pytest spikes/002 -v``.
"""

import json
from typing import Annotated, Any

import pytest
from mcp import Client
from mcp.client import ClientRequestContext
from mcp.server import MCPServer
from mcp.server.elicitation import AcceptedElicitation
from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestParams,
    ElicitResult,
    ErrorData,
    InputRequiredResult,
    InputResponses,
    TextContent,
)
from pydantic import BaseModel

CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"confirm": {"type": "boolean", "title": "Place this order?"}},
    "required": ["confirm"],
}


class OrderOut(BaseModel):
    status: str
    pending_id: str
    decision: str


class Observed:
    """What the server saw on each round, for assertions."""

    def __init__(self) -> None:
        self.rounds: list[dict[str, Any]] = []
        self.pending_created = 0


def is_confirmed(responses: InputResponses | None) -> bool:
    """Fail closed: only an explicit accept with ``confirm is True`` confirms; anything else refuses."""
    answer = (responses or {}).get("confirm")
    return (
        isinstance(answer, ElicitResult)
        and answer.action == "accept"
        and (answer.content or {}).get("confirm") is True
    )


def invalid_params(message: str) -> CallToolResult:
    payload = {"code": "invalid_params", "message": message, "next_step": "Call the tool again without them."}
    return CallToolResult(
        content=[TextContent(type="text", text=message)], structured_content=payload, is_error=True
    )


def ask(symbol: str, qty: str, pending_id: str) -> InputRequiredResult:
    return InputRequiredResult(
        input_requests={
            "confirm": ElicitRequest(
                params=ElicitRequestFormParams(
                    message=f"Buy {qty} {symbol} at market?", requested_schema=CONFIRM_SCHEMA
                )
            )
        },
        request_state=json.dumps({"pending_id": pending_id}),
    )


def build_server(observed: Observed) -> MCPServer[Any]:
    """The template for ADR-005 / D-1: round 2 is recognised by ``request_state``, never by answers."""
    mcp: MCPServer[Any] = MCPServer("spike-002")

    @mcp.tool(name="place_order", description="Spike: confirm through client UI, then execute.")
    async def place_order(
        symbol: str, qty: str, ctx: Context
    ) -> Annotated[CallToolResult, OrderOut] | InputRequiredResult:
        observed.rounds.append(
            {
                "protocol_version": ctx.protocol_version,
                "client_elicitation": (
                    ctx.client_capabilities.elicitation if ctx.client_capabilities else None
                ),
                "input_responses": ctx.input_responses,
                "request_state": ctx.request_state,
            }
        )
        if ctx.request_state is None:
            if ctx.input_responses is not None:
                # Answers we never asked for (no sealed state to tie them to): fail closed.
                return invalid_params("inputResponses without requestState are not accepted.")
            # Round 1: the real server creates the pending row here (ADR-005, step 1).
            observed.pending_created += 1
            return ask(symbol, qty, f"p-{observed.pending_created}")
        # Round 2: request_state is the plaintext we minted, verified by the SDK.
        pending_id = json.loads(ctx.request_state)["pending_id"]
        if ctx.input_responses is None:
            # State-only retry (D-1): re-ask for the SAME pending, never create a new one.
            return ask(symbol, qty, pending_id)
        answer = ctx.input_responses.get("confirm")
        out = OrderOut(
            status="executed" if is_confirmed(ctx.input_responses) else "rejected",
            pending_id=pending_id,
            decision=answer.action if isinstance(answer, ElicitResult) else "missing",
        )
        return CallToolResult(
            content=[TextContent(type="text", text=out.model_dump_json())],
            structured_content=out.model_dump(mode="json"),
        )

    return mcp


def make_callback(action: str, seen: list[ElicitRequestParams]) -> Any:
    async def callback(
        context: ClientRequestContext, params: ElicitRequestParams
    ) -> ElicitResult | ErrorData:
        seen.append(params)
        content = {"confirm": True} if action == "accept" else None
        return ElicitResult(action=action, content=content)

    return callback


async def test_output_schema_survives_input_required_union() -> None:
    mcp = build_server(Observed())
    async with Client(mcp) as client:
        tool = (await client.list_tools()).tools[0]
    assert tool.output_schema is not None
    assert set(tool.output_schema["properties"]) == {"status", "pending_id", "decision"}


async def test_client_resolves_mrtr_with_elicitation_callback() -> None:
    observed, seen = Observed(), list[ElicitRequestParams]()
    mcp = build_server(observed)
    async with Client(mcp, elicitation_callback=make_callback("accept", seen)) as client:
        result = await client.call_tool("place_order", {"symbol": "BTCUSDT", "qty": "0.01"})

    assert result.is_error is False
    assert result.structured_content == {"status": "executed", "pending_id": "p-1", "decision": "accept"}
    # The callback got the form exactly as the server built it.
    assert len(seen) == 1
    assert isinstance(seen[0], ElicitRequestFormParams)
    assert seen[0].message == "Buy 0.01 BTCUSDT at market?"
    # Two separate tools/call rounds reached the tool body.
    first, second = observed.rounds
    assert first["protocol_version"] == "2026-07-28"
    assert first["client_elicitation"] is not None and first["client_elicitation"].form is not None
    assert first["input_responses"] is None and first["request_state"] is None
    assert second["request_state"] == '{"pending_id": "p-1"}'  # plaintext: the SDK unsealed it
    assert second["input_responses"]["confirm"].content == {"confirm": True}


async def test_decline_reaches_the_tool_as_action() -> None:
    observed: Observed = Observed()
    async with Client(build_server(observed), elicitation_callback=make_callback("decline", [])) as client:
        result = await client.call_tool("place_order", {"symbol": "BTCUSDT", "qty": "0.01"})
    assert result.structured_content == {"status": "rejected", "pending_id": "p-1", "decision": "decline"}


async def test_client_without_callback_declares_no_elicitation_and_fails_on_input_required() -> None:
    observed = Observed()
    async with Client(build_server(observed)) as client:
        with pytest.raises(MCPError) as info:
            await client.call_tool("place_order", {"symbol": "BTCUSDT", "qty": "0.01"})
    # The server could have seen this and picked the external channel (ADR-005 step 2).
    assert observed.rounds[0]["client_elicitation"] is None
    assert info.value.error.code == -32600
    assert info.value.error.message == "Elicitation not supported"


async def test_manual_loop_sees_sealed_state_and_binding_to_arguments() -> None:
    mcp = build_server(Observed())
    args = {"symbol": "BTCUSDT", "qty": "0.01"}
    async with Client(mcp, elicitation_callback=make_callback("accept", [])) as client:
        first = await client.session.call_tool("place_order", args, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        sealed = first.request_state
        assert sealed is not None and sealed.startswith("v1.") and "p-1" not in sealed  # AES-GCM, opaque
        answers: InputResponses = {"confirm": ElicitResult(action="accept", content={"confirm": True})}

        # Replaying the state with different arguments is refused before the tool runs (-32602).
        with pytest.raises(MCPError) as tampered_args:
            await client.session.call_tool(
                "place_order", {**args, "qty": "5"}, input_responses=answers, request_state=sealed
            )
        assert tampered_args.value.error.code == -32602
        assert tampered_args.value.error.message == "Invalid or expired requestState"

        with pytest.raises(MCPError) as tampered_state:
            await client.session.call_tool(
                "place_order", args, input_responses=answers, request_state=sealed[:-2] + "AA"
            )
        assert tampered_state.value.error.code == -32602

        # Same state replayed twice is accepted twice: single use is the server's job (ADR-005 step 3).
        for _ in range(2):
            ok = await client.session.call_tool(
                "place_order", args, input_responses=answers, request_state=sealed
            )
            assert isinstance(ok, CallToolResult) and ok.is_error is False


async def test_legacy_connection_cannot_carry_input_required() -> None:
    observed = Observed()
    async with Client(
        build_server(observed), mode="legacy", elicitation_callback=make_callback("accept", [])
    ) as client:
        with pytest.raises(MCPError) as info:
            await client.call_tool("place_order", {"symbol": "BTCUSDT", "qty": "0.01"})
    assert observed.rounds[0]["protocol_version"] == "2025-11-25"
    assert info.value.error.code == -32603


class Confirm(BaseModel):
    confirm: bool


def build_dual_era_server() -> MCPServer[Any]:
    """One tool, both eras: MRTR on 2026-07-28, push ``ctx.elicit`` on a legacy connection."""
    mcp: MCPServer[Any] = MCPServer("spike-002")

    @mcp.tool(name="place_order")
    async def place_order(
        symbol: str, ctx: Context
    ) -> Annotated[CallToolResult, OrderOut] | InputRequiredResult:
        message = f"Buy {symbol}?"
        if ctx.protocol_version == "2025-11-25":
            answer = await ctx.elicit(message, Confirm)  # server->client request inside the call
            decision: str = answer.action
            confirmed = isinstance(answer, AcceptedElicitation) and answer.data.confirm is True
        elif ctx.request_state is None:
            if ctx.input_responses is not None:
                return invalid_params("inputResponses without requestState are not accepted.")
            return InputRequiredResult(
                input_requests={
                    "confirm": ElicitRequest(
                        params=ElicitRequestFormParams(
                            message=message, requested_schema=Confirm.model_json_schema()
                        )
                    )
                },
                request_state="p-1",
            )
        else:
            response = (ctx.input_responses or {}).get("confirm")
            decision = response.action if isinstance(response, ElicitResult) else "missing"
            confirmed = is_confirmed(ctx.input_responses)
        out = OrderOut(status="executed" if confirmed else "rejected", pending_id="p-1", decision=decision)
        return CallToolResult(content=[], structured_content=out.model_dump(mode="json"))

    return mcp


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_same_tool_serves_both_eras(mode: str) -> None:
    seen = list[ElicitRequestParams]()
    callback = make_callback("accept", seen)
    async with Client(build_dual_era_server(), mode=mode, elicitation_callback=callback) as client:
        result = await client.call_tool("place_order", {"symbol": "BTCUSDT"})
    assert result.structured_content == {"status": "executed", "pending_id": "p-1", "decision": "accept"}
    assert [p.message for p in seen] == ["Buy BTCUSDT?"]


async def test_push_elicit_is_refused_on_the_modern_protocol() -> None:
    mcp: MCPServer[Any] = MCPServer("spike-002")

    @mcp.tool(name="ask")
    async def ask(ctx: Context) -> str:
        await ctx.elicit("Sure?", Confirm)
        return "unreachable"

    async with Client(mcp, elicitation_callback=make_callback("accept", [])) as client:
        with pytest.raises(MCPError) as info:
            await client.call_tool("ask", {})
    # NoBackChannelError is an MCPError, so it surfaces as a protocol error, not a tool result.
    assert info.value.error.code == -32600
    assert "no back-channel" in info.value.error.message


ARGS = {"symbol": "BTCUSDT", "qty": "0.01"}
ACCEPT: InputResponses = {"confirm": ElicitResult(action="accept", content={"confirm": True})}


async def test_answers_without_state_reach_the_tool_and_the_template_refuses_them() -> None:
    observed = Observed()
    async with Client(build_server(observed)) as client:
        result = await client.session.call_tool("place_order", ARGS, input_responses=ACCEPT)
    # SDK fact: nothing ties inputResponses to a requestState; the body sees answers on "round 1".
    assert observed.rounds[0]["request_state"] is None
    assert observed.rounds[0]["input_responses"] is not None
    # Our template: fail closed, no pending created, nothing executed.
    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["code"] == "invalid_params"
    assert observed.pending_created == 0


async def test_state_only_retry_re_asks_for_the_same_pending() -> None:
    observed = Observed()
    async with Client(build_server(observed)) as client:
        first = await client.session.call_tool("place_order", ARGS, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        again = await client.session.call_tool(
            "place_order", ARGS, request_state=first.request_state, allow_input_required=True
        )
        assert isinstance(again, InputRequiredResult)
        done = await client.session.call_tool(
            "place_order", ARGS, input_responses=ACCEPT, request_state=again.request_state
        )
    assert observed.pending_created == 1
    assert done.structured_content == {"status": "executed", "pending_id": "p-1", "decision": "accept"}


@pytest.mark.parametrize(
    "answers",
    [
        {"confirm": ElicitResult(action="accept", content={"confirm": False})},
        {"confirm": ElicitResult(action="accept", content={})},
        {"confirm": ElicitResult(action="accept")},
        {"other": ElicitResult(action="accept", content={"confirm": True})},
        {"confirm": ElicitResult(action="cancel")},
    ],
    ids=["confirm-false", "no-confirm-field", "no-content", "wrong-key", "cancel"],
)
async def test_only_accept_with_confirm_true_confirms(answers: InputResponses) -> None:
    async with Client(build_server(Observed())) as client:
        first = await client.session.call_tool("place_order", ARGS, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        result = await client.session.call_tool(
            "place_order", ARGS, input_responses=answers, request_state=first.request_state
        )
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "rejected"
