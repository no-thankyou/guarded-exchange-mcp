"""Single mapping of domain exceptions to MCP tool execution errors (PLAN ADR-013).

Anticipated failures become ``CallToolResult(isError=True)`` with structured content
``{code, message, next_step, retry_after_s?, details?, untrusted_text?}`` so that the model
can self-correct and evals can grade on ``code``. Protocol errors (``MCPError``) are reserved
for protocol problems the client, not the model, must handle.
"""

from typing import Final

from mcp.types import CallToolResult, TextContent

from guarded_exchange.domain.errors import DomainError
from guarded_exchange.interface.mcp.schemas import ToolErrorOut

DEFAULT_NEXT_STEP: Final[dict[str, str]] = {
    "invalid_params": "Fix the arguments according to the tool's input schema and call again.",
    "not_found": "Check the identifiers; list the available objects first if unsure.",
    "permission_denied": "Do not retry or work around it. Tell the user; only they or the admin can "
    "change permissions, outside the chat.",
    "exchange_unavailable": "Retry in a few seconds; if it keeps failing, tell the user.",
    "exchange_rejected": "Do not retry unchanged; report the exchange reason to the user.",
}
_FALLBACK_NEXT_STEP: Final = "Report this error to the user."


def to_error_out(exc: DomainError) -> ToolErrorOut:
    return ToolErrorOut(
        code=exc.code,
        message=exc.message,
        next_step=exc.next_step or DEFAULT_NEXT_STEP.get(exc.code, _FALLBACK_NEXT_STEP),
        retry_after_s=exc.retry_after_s,
        details=exc.details or None,
        untrusted_text=exc.untrusted_text,
    )


def error_result(exc: DomainError) -> CallToolResult:
    payload = to_error_out(exc)
    return CallToolResult(
        content=[TextContent(type="text", text=payload.model_dump_json(exclude_none=True))],
        structured_content=payload.model_dump(mode="json", exclude_none=True),
        is_error=True,
    )


def internal_error_result(request_id: str) -> CallToolResult:
    """A bug or unanticipated failure: no exception text reaches the model, only a log reference."""
    return error_result(
        DomainError(
            "internal server error",
            next_step="Do not retry in a loop; tell the user, quoting the request_id for the server logs.",
            details={"request_id": request_id},
        )
    )
