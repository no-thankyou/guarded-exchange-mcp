"""Kill switch tool (FR-16, FR-44). The agent can only turn its user's kill switch on: there is no
tool, argument or other way to turn it off through MCP. Not rate limited (PLAN D-16)."""

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations

from guarded_exchange.application.kill_switch import ActivateKillSwitch
from guarded_exchange.interface.mcp.access import PrincipalProvider
from guarded_exchange.interface.mcp.results import run_tool
from guarded_exchange.interface.mcp.schemas import KillSwitchOut
from guarded_exchange.interface.mcp.tools.trading import client_description

ACTIVATE_KILL_SWITCH_DESCRIPTION = (
    "Turn on your kill switch: from now on every risk-increasing action is refused with "
    "kill_switch_active (new orders that are not reduce-only, including any spot order; raising "
    "leverage; amending an order to a larger size or a worse price). Reading and risk-reducing actions "
    "keep working: cancelling orders, reduce-only orders, closing positions, stop-loss / take-profit. "
    "It cancels nothing on the exchange: cancel open orders separately if the user wants that. Use it "
    "when the user asks to stop trading, or when something looks wrong and trading must stop at once. "
    "There is no tool to turn it off: only the user (or the admin, if the admin turned it on) can, "
    "outside the chat. Repeating the call is harmless."
)


def register(
    mcp: MCPServer[Any], *, activate_kill_switch: ActivateKillSwitch, principal: PrincipalProvider
) -> None:
    @mcp.tool(
        name="activate_kill_switch",
        title="Activate kill switch",
        description=ACTIVATE_KILL_SWITCH_DESCRIPTION,
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
    async def activate_kill_switch_tool(ctx: Context) -> Annotated[CallToolResult, KillSwitchOut]:
        async def call() -> KillSwitchOut:
            return KillSwitchOut.from_domain(await activate_kill_switch(principal(), client_description(ctx)))

        return await run_tool("activate_kill_switch", ctx.request_id, call())
