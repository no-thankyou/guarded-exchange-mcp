"""Helpers that keep tool handlers thin: request-scoped logging context and result shaping."""

import time
import uuid
from collections.abc import Awaitable

import structlog
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

from guarded_exchange.domain.errors import DomainError
from guarded_exchange.interface.mcp.errors import error_result

log = structlog.get_logger(__name__)


def success_result(output: BaseModel) -> CallToolResult:
    """Structured content plus the same JSON as text for clients that only show ``content``."""
    return CallToolResult(
        content=[TextContent(type="text", text=output.model_dump_json(exclude_none=True))],
        structured_content=output.model_dump(mode="json", exclude_none=True),
    )


async def run_tool(tool: str, mcp_request_id: str | None, call: Awaitable[BaseModel]) -> CallToolResult:
    """Await ``call`` with a fresh ``request_id`` bound to every log line, map domain errors.

    Unexpected exceptions propagate: the SDK logs the traceback and returns a generic
    ``isError`` result without leaking internals to the model.
    """
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    with structlog.contextvars.bound_contextvars(
        request_id=request_id, tool=tool, mcp_request_id=mcp_request_id
    ):
        try:
            output = await call
        except DomainError as exc:
            log.info("tool_error", code=exc.code, duration_ms=_ms(started))
            return error_result(exc)
        log.info("tool_ok", duration_ms=_ms(started))
        return success_result(output)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
