"""``get_confirmation_status`` and ``cancel_confirmation`` (FR-47, D-6). Thin handlers.

There is deliberately **no tool that approves or rejects**: the model cannot confirm anything (FR-20,
AC-4). Approval is the human's, through the CLI, which calls ``ResolveConfirmation`` directly.
``cancel_confirmation`` only withdraws the agent's own waiting request: it reduces risk and never executes.
"""

from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from guarded_exchange.application.resolve_confirmation import CancelConfirmation, GetConfirmationStatus
from guarded_exchange.interface.mcp.access import PrincipalProvider
from guarded_exchange.interface.mcp.results import run_tool
from guarded_exchange.interface.mcp.schemas_confirmation import ConfirmationOut

GET_CONFIRMATION_STATUS_DESCRIPTION = (
    "Read the state of a confirmation that a trading call returned as pending (confirmation_id): pending "
    "(waits for the user), approved/executing, executed, failed, unknown, rejected, expired, cancelled. "
    "Once the user approved, the server executes the action itself: do not repeat the trading call."
)
CANCEL_CONFIRMATION_DESCRIPTION = (
    "Withdraw your own request that still waits for the user (pending, or approved but not yet started). "
    "It does NOT confirm or execute anything and frees the slot. You cannot approve a confirmation: only "
    "the user can, outside the chat."
)

ConfirmationIdArg = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", description="From the pending answer."),
]


@dataclass(frozen=True, slots=True)
class ConfirmationUseCases:
    get_status: GetConfirmationStatus
    cancel: CancelConfirmation


def register(mcp: MCPServer[Any], *, use_cases: ConfirmationUseCases, principal: PrincipalProvider) -> None:
    @mcp.tool(
        name="get_confirmation_status",
        title="Get confirmation status",
        description=GET_CONFIRMATION_STATUS_DESCRIPTION,
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    async def get_confirmation_status_tool(
        confirmation_id: ConfirmationIdArg, ctx: Context
    ) -> Annotated[CallToolResult, ConfirmationOut]:
        async def call() -> ConfirmationOut:
            return ConfirmationOut.from_domain(await use_cases.get_status(principal(), confirmation_id))

        return await run_tool("get_confirmation_status", ctx.request_id, call())

    @mcp.tool(
        name="cancel_confirmation",
        title="Cancel confirmation",
        description=CANCEL_CONFIRMATION_DESCRIPTION,
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    async def cancel_confirmation_tool(
        confirmation_id: ConfirmationIdArg, ctx: Context
    ) -> Annotated[CallToolResult, ConfirmationOut]:
        async def call() -> ConfirmationOut:
            return ConfirmationOut.from_domain(await use_cases.cancel(principal(), confirmation_id))

        return await run_tool("cancel_confirmation", ctx.request_id, call())
