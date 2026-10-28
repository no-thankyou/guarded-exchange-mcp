"""``GuardedMCPServer``: the SDK's ``MCPServer`` with our protocol-boundary policies (spike 002).

Overrides of public ``MCPServer`` methods only:

- ``call_tool`` (ADR-013): arguments that fail the tool's input schema become our structured
  tool error ``code=invalid_params`` instead of the SDK's free-text message. Tools therefore
  declare real argument types (``Literal``, ``Field(pattern=..., ge=..., le=...)``) and one
  annotation gives both ``inputSchema`` and the runtime check. This is an extra layer: use cases
  still normalise and validate through domain value objects, because they have other entrypoints.
- ``list_tools`` (ADR-012, ticket 006): only the tools the caller's profile allows are listed.
- ``call_tool`` also asks the same ``ToolGate`` first (FR-11): a hidden or forbidden tool called
  directly gets ``permission_denied`` before argument validation, so a refusal reveals nothing
  about the tool's schema. The decision itself is made by the application (``AuthorizeAccess``).
  A registered tool without a declared permission is hidden and refused (fail closed); a name
  that is not registered at all keeps the SDK's "unknown tool" result.

Both rely on the implicit SDK contract "``_handle_call_tool``/``_handle_list_tools`` call
``self.call_tool``/``self.list_tools``"; ``mcp`` is pinned with ``==`` and e2e tests catch a change.
"""

from typing import Any

import structlog
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult, Tool
from pydantic import ValidationError

from guarded_exchange.interface.mcp.access import ToolGate
from guarded_exchange.interface.mcp.errors import argument_name, invalid_arguments_result

log = structlog.get_logger(__name__)


class GuardedMCPServer(MCPServer[Any]):
    def __init__(
        self,
        name: str,
        *,
        gate: ToolGate,
        instructions: str,
        version: str,
        token_verifier: TokenVerifier | None = None,
        auth: AuthSettings | None = None,
    ) -> None:
        super().__init__(
            name, instructions=instructions, version=version, token_verifier=token_verifier, auth=auth
        )
        self._gate = gate

    async def list_tools(self) -> list[Tool]:
        return await self._gate.visible(await super().list_tools())

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        # Outside the try below: a refusal is our result, never mistaken for an SDK error. A name
        # that is not registered at all keeps the SDK's "unknown tool" handling.
        if any(tool.name == name for tool in await super().list_tools()):
            request_id = context.request_id if context is not None else None
            refusal = await self._gate.refusal(name, request_id)
            if refusal is not None:
                return refusal
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            # Unknown tool, a deliberate ToolError or a crash in the body (UnexpectedToolError,
            # even if caused by a pydantic error inside our code) keep the SDK handling.
            if isinstance(exc, UnexpectedToolError) or not isinstance(exc.__cause__, ValidationError):
                raise
            error = exc.__cause__
            fields = sorted({argument_name(e["loc"]) for e in error.errors(include_input=False)})
            log.info("tool_invalid_arguments", tool=name, fields=fields)  # names only, never values
            return invalid_arguments_result(error)
