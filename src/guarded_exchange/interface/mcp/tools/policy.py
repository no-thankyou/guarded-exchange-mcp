"""Policy tools. Read only: through MCP the agent can never change its policy (FR-44)."""

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations

from guarded_exchange.application.policy import ReadPolicy
from guarded_exchange.interface.mcp.access import PrincipalProvider
from guarded_exchange.interface.mcp.results import run_tool
from guarded_exchange.interface.mcp.schemas import PolicyOut

GET_MY_POLICY_DESCRIPTION = (
    "Get your effective policy: profile (read_only or trade), environment, allowed instruments, "
    "order size and leverage limits, which actions need the user's confirmation and how long a "
    "confirmation request lives. Read-only. Use it before proposing a trade to stay within limits. "
    "There is no tool to change the policy: only the user and the admin can change it, outside "
    "the chat. If an action is refused by the policy, tell the user instead of working around it."
)


def register(mcp: MCPServer[Any], read_policy: ReadPolicy, principal: PrincipalProvider) -> None:
    @mcp.tool(
        name="get_my_policy",
        title="Get my policy",
        description=GET_MY_POLICY_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    )
    async def get_my_policy_tool(ctx: Context) -> Annotated[CallToolResult, PolicyOut]:
        async def call() -> PolicyOut:
            view = await read_policy.for_principal(principal())
            return PolicyOut.from_domain(view.effective)

        return await run_tool("get_my_policy", ctx.request_id, call())
