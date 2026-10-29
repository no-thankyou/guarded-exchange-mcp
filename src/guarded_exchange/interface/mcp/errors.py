"""Single mapping of domain exceptions to MCP tool execution errors (PLAN ADR-013).

Anticipated failures become ``CallToolResult(isError=True)`` with structured content
``{code, message, next_step, retry_after_s?, details?, untrusted_text?}`` so that the model
can self-correct and evals can grade on ``code``. Protocol errors (``MCPError``) are reserved
for protocol problems the client, not the model, must handle.
"""

from typing import Final

from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from guarded_exchange.domain.errors import DomainError, InvalidParams
from guarded_exchange.interface.mcp.schemas import ToolErrorOut

DEFAULT_NEXT_STEP: Final[dict[str, str]] = {
    "invalid_params": "Fix the arguments according to the tool's input schema and call again.",
    "not_found": "Check the identifiers; list the available objects first if unsure.",
    "permission_denied": "Do not retry or work around it. Tell the user; only they or the admin can "
    "change permissions, outside the chat.",
    "exchange_unavailable": "Retry in a few seconds; if it keeps failing, tell the user.",
    "exchange_rejected": "Do not retry unchanged; report the exchange reason to the user.",
    "exchange_credentials_invalid": "Do not retry. Tell the user to check or replace the exchange key "
    "outside the chat; never ask for keys in the chat.",
    "rate_limited": "Wait retry_after_s seconds before calling again.",
    "instrument_rule_violation": "Fix qty or price according to get_instrument_rules and the details, "
    "then call again.",
    "limit_exceeded": "Do not retry or work around it; tell the user which limit was hit.",
    "confirmation_channel_unavailable": "Do not retry; tell the user the action needs their confirmation.",
    "position_not_found": "Check get_positions before a reduce-only order.",
    "outcome_unknown": "Do not repeat the action; check the open orders first.",
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


_ARGUMENTS_NEXT_STEP: Final = (
    "Fix the named arguments according to the tool's input schema (required fields, types, allowed "
    "values) and call again."
)

# pydantic error types whose built-in message describes the rule, never the rejected value
# ("Field required", "Input should be 'spot' or 'linear'", "String should match pattern ...").
# Any other type - notably `value_error`/`assertion_error` from a custom validator, whose text
# may quote the input - is reported by location and type only (fail closed against echoing a
# value, e.g. a pasted secret, back into the model context and logs).
_RULE_ONLY_MESSAGES: Final = frozenset(
    {
        "missing",
        "literal_error",
        "enum",
        "string_type",
        "string_pattern_mismatch",
        "string_too_short",
        "string_too_long",
        "int_type",
        "int_parsing",
        "int_from_float",
        "bool_type",
        "bool_parsing",
        "decimal_type",
        "decimal_parsing",
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
    }
)


def invalid_arguments_result(error: ValidationError) -> CallToolResult:
    """Arguments rejected by the tool's input schema -> ``invalid_params`` (ADR-013, spike 002).

    ``details`` name the first failing argument (``field``) and the pydantic error type
    (``reason``); the message lists every problem. Rejected values are never included.
    """
    errors = error.errors(include_input=False)
    problems = [(_safe_path(e["loc"]), e["type"], e["msg"]) for e in errors]
    message = "; ".join(
        f"{loc}: {msg}" if kind in _RULE_ONLY_MESSAGES else f"{loc}: {kind}" for loc, kind, msg in problems
    )
    return error_result(
        InvalidParams(
            f"invalid arguments: {message}",
            next_step=_ARGUMENTS_NEXT_STEP,
            details={"field": argument_name(errors[0]["loc"]), "reason": errors[0]["type"]},
        )
    )


def argument_name(loc: tuple[int | str, ...]) -> str:
    """The top-level argument of a pydantic error location: a name declared by the tool signature."""
    return str(loc[0]) if loc else "arguments"


def _safe_path(loc: tuple[int | str, ...]) -> str:
    """``argument_name`` plus list indexes; nested string parts (e.g. keys of a dict the caller
    sent) are caller data and become ``*``."""
    rest = [str(part) if isinstance(part, int) else "*" for part in loc[1:]]
    return ".".join([argument_name(loc), *rest])


def internal_error_result(request_id: str) -> CallToolResult:
    """A bug or unanticipated failure: no exception text reaches the model, only a log reference."""
    return error_result(
        DomainError(
            "internal server error",
            next_step="Do not retry in a loop; tell the user, quoting the request_id for the server logs.",
            details={"request_id": request_id},
        )
    )
