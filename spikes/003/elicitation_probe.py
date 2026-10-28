"""Spike 003: a stdio MCP server that asks for a confirmation form and logs what the client did.

One tool, ``confirm_test_order``, follows the ADR-005 / D-1 template from spike 002
(``build_dual_era_server``) and nothing is ever traded:

* protocol 2026-07-28 -> MRTR: round 1 returns ``InputRequiredResult`` with a form, round 2 is
  recognised **only** by ``ctx.request_state``; answers without state -> ``invalid_params``;
* an earlier protocol (2025-11-25 and older) -> push ``elicitation/create`` inside the call;
* the client's ``elicitation`` capability is checked **before** a pending id is minted; without it
  the tool fails closed with ``confirmation_channel_unavailable``;
* confirmed = ``action == "accept"`` **and** ``content.confirm is True``; everything else refuses.

Every step is one JSON line on stderr (stdout carries JSON-RPC only) and, when
``SPIKE003_LOG_FILE`` is set, is appended to that file too: client log locations differ between
clients, the file does not. Logged: ``protocol_version``, ``client_info``,
``client_capabilities.elicitation`` (raw), the elicitation ``action`` and ``content``.

Run: ``uv run --directory <repo> python spikes/003/elicitation_probe.py`` (see README.md here).
"""

import itertools
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    ClientCapabilities,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    InputResponses,
    TextContent,
)
from mcp.types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel

LOG_FILE_ENV = "SPIKE003_LOG_FILE"
FORCE_ASK_ENV = "SPIKE003_FORCE_ASK"
CONFIRM_KEY = "confirm"
CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirm": {
            "type": "boolean",
            "title": "Confirm this test order",
            "description": "Spike 003: nothing is sent to any exchange.",
        }
    },
    "required": ["confirm"],
}

Channel = Literal["mrtr", "legacy_push"]
LogFn = Callable[..., None]


class ProbeOut(BaseModel):
    """What the tool reports back to the chat, so a screenshot shows the outcome."""

    pending_id: str
    protocol_version: str | None
    channel: Channel
    decision: str  # accept | decline | cancel | missing | error
    content: dict[str, Any] | None
    confirmed: bool


def stderr_logger(log_file: str | None = None) -> LogFn:
    """JSON lines on stderr (never stdout: it belongs to the protocol), optionally teed to a file."""

    def log(event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        print(line, file=sys.stderr, flush=True)
        if log_file:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    return log


def supports_form(capabilities: ClientCapabilities | None) -> bool:
    """Form elicitation is declared: ``{form: {}}`` or the bare ``{}`` (pre-modes shape = form only).

    Same rule as the SDK's own capability check (``mcp/server/mcpserver/resolve.py``); a url-only
    declaration does not count.
    """
    elicitation = capabilities.elicitation if capabilities is not None else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def is_confirmed(answer: object) -> bool:
    """Fail closed: only an explicit accept with ``confirm is True`` confirms."""
    return (
        isinstance(answer, ElicitResult)
        and answer.action == "accept"
        and (answer.content or {}).get(CONFIRM_KEY) is True
    )


def describe_client(ctx: Context) -> dict[str, Any]:
    params = ctx.session.client_params
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    return {
        "protocol_version": ctx.protocol_version,
        "client_info": (
            params.client_info.model_dump(mode="json", exclude_none=True) if params is not None else None
        ),
        # Raw shape matters: {} vs {"form": {}} vs {"form": {}, "url": {}} vs absent.
        "client_capabilities_elicitation": (
            elicitation.model_dump(mode="json", by_alias=True, exclude_none=True)
            if elicitation is not None
            else None
        ),
        "supports_form": supports_form(capabilities),
    }


def dump_responses(responses: InputResponses | None) -> dict[str, Any] | None:
    if responses is None:
        return None
    return {key: value.model_dump(mode="json", by_alias=True) for key, value in responses.items()}


def tool_error(code: str, message: str, log: LogFn, **fields: Any) -> CallToolResult:
    log("tool_refused", code=code, **fields)
    payload = {"code": code, "message": message}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
        is_error=True,
    )


def build_server(log: LogFn, *, force_ask: bool = False) -> MCPServer[Any]:
    """The probe server. ``log`` is injected so the smoke test can capture events in memory.

    ``force_ask`` is a diagnostic switch for the manual run only: ask even when the client did not
    declare form elicitation, to see what such a client sends back (research: Claude Desktop may
    answer a fake ``decline``). The product never does this (ADR-005: fail closed).
    """
    mcp: MCPServer[Any] = MCPServer("spike-003-elicitation")
    pending_ids = (f"p-{n}" for n in itertools.count(1))

    def ask(message: str, pending_id: str) -> InputRequiredResult:
        log("input_required_sent", pending_id=pending_id)
        return InputRequiredResult(
            input_requests={
                CONFIRM_KEY: ElicitRequest(
                    params=ElicitRequestFormParams(message=message, requested_schema=CONFIRM_SCHEMA)
                )
            },
            request_state=pending_id,
        )

    def report(ctx: Context, pending_id: str, channel: Channel, answer: object) -> CallToolResult:
        """Log the raw answer and turn it into the tool result (``answer`` is ``None`` on error)."""
        result = answer if isinstance(answer, ElicitResult) else None
        log(
            "elicitation_result",
            channel=channel,
            pending_id=pending_id,
            action=result.action if result is not None else None,
            content=result.content if result is not None else None,
        )
        out = ProbeOut(
            pending_id=pending_id,
            protocol_version=ctx.protocol_version,
            channel=channel,
            decision=result.action if result is not None else ("missing" if channel == "mrtr" else "error"),
            content=result.content if result is not None else None,
            confirmed=is_confirmed(result),
        )
        log("decision", **out.model_dump(mode="json"))
        return CallToolResult(
            content=[TextContent(type="text", text=out.model_dump_json())],
            structured_content=out.model_dump(mode="json"),
        )

    async def legacy_push(ctx: Context, message: str, pending_id: str) -> CallToolResult:
        """Earlier protocols: ``elicitation/create`` inside this call; raw result, no schema coercion."""
        log("legacy_elicit_sent", pending_id=pending_id)
        try:
            raw = await ctx.session.elicit_form(
                message=message, requested_schema=CONFIRM_SCHEMA, related_request_id=ctx.request_id
            )
        except MCPError as exc:
            log("elicitation_error", pending_id=pending_id, code=exc.error.code, message=exc.error.message)
            return report(ctx, pending_id, "legacy_push", None)
        return report(ctx, pending_id, "legacy_push", raw)

    @mcp.tool(
        name="confirm_test_order",
        description=(
            "Spike 003 probe. Asks the human to confirm a fake order through the client's form UI "
            "and reports what came back. Nothing is traded. Call it when the user asks to test confirmation."
        ),
    )
    async def confirm_test_order(
        ctx: Context, symbol: str = "BTCUSDT", qty: str = "0.001"
    ) -> Annotated[CallToolResult, ProbeOut] | InputRequiredResult:
        modern = ctx.protocol_version in MODERN_PROTOCOL_VERSIONS
        log(
            "tools_call",
            request_id=ctx.request_id,
            symbol=symbol,
            qty=qty,
            has_request_state=ctx.request_state is not None,
            input_responses=dump_responses(ctx.input_responses),
            **describe_client(ctx),
        )
        message = f"[spike 003] Buy {qty} {symbol} at market? Test only: nothing is sent to an exchange."

        if modern and ctx.request_state is not None:
            # Round 2 (D-1): recognised only by our state, unsealed and verified by the SDK.
            pending_id = ctx.request_state
            if ctx.input_responses is None:
                log("state_only_retry", pending_id=pending_id)
                return ask(message, pending_id)  # same pending, never a new one
            return report(ctx, pending_id, "mrtr", ctx.input_responses.get(CONFIRM_KEY))

        if modern and ctx.input_responses is not None:
            return tool_error("invalid_params", "inputResponses without requestState are not accepted.", log)

        # Round 1 (or a legacy call): check the channel BEFORE creating anything.
        if not supports_form(ctx.client_capabilities) and not force_ask:
            return tool_error(
                "confirmation_channel_unavailable",
                "This client did not declare form elicitation; no confirmation was requested.",
                log,
                protocol_version=ctx.protocol_version,
            )
        pending_id = next(pending_ids)  # the real server inserts the pending row here
        log("pending_created", pending_id=pending_id, forced=not supports_form(ctx.client_capabilities))
        if modern:
            return ask(message, pending_id)
        return await legacy_push(ctx, message, pending_id)

    return mcp


def main() -> None:
    log = stderr_logger(os.environ.get(LOG_FILE_ENV))
    force_ask = os.environ.get(FORCE_ASK_ENV) == "1"
    log(
        "server_start",
        pid=os.getpid(),
        mcp_version=version("mcp"),
        log_file=os.environ.get(LOG_FILE_ENV),
        force_ask=force_ask,
    )
    build_server(log, force_ask=force_ask).run("stdio")


if __name__ == "__main__":
    main()
