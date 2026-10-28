"""``GuardedMCPServer``: the SDK's ``MCPServer`` with our protocol-boundary policies (spike 002).

Overrides of public ``MCPServer`` methods only:

- ``call_tool`` (ADR-013): arguments that fail the tool's input schema become our structured
  tool error ``code=invalid_params`` instead of the SDK's free-text message. Tools therefore
  declare real argument types (``Literal``, ``Field(pattern=..., ge=..., le=...)``) and one
  annotation gives both ``inputSchema`` and the runtime check. This is an extra layer: use cases
  still normalise and validate through domain value objects, because they have other entrypoints.
- ``list_tools`` (ADR-012, ticket 006) will filter the tool list by the caller's profile.

Both rely on the implicit SDK contract "``_handle_call_tool``/``_handle_list_tools`` call
``self.call_tool``/``self.list_tools``"; ``mcp`` is pinned with ``==`` and e2e tests catch a change.
"""

from typing import Any

import structlog
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult
from pydantic import ValidationError

from guarded_exchange.interface.mcp.errors import invalid_arguments_result

log = structlog.get_logger(__name__)


class GuardedMCPServer(MCPServer[Any]):
    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            # Unknown tool, a deliberate ToolError or a crash in the body (UnexpectedToolError,
            # even if caused by a pydantic error inside our code) keep the SDK handling.
            if isinstance(exc, UnexpectedToolError) or not isinstance(exc.__cause__, ValidationError):
                raise
            error = exc.__cause__
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in error.errors(include_input=False)})
            log.info("tool_invalid_arguments", tool=name, fields=fields)  # names only, never values
            return invalid_arguments_result(error)
