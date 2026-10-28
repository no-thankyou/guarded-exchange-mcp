"""Spike 002, Q6 (open question from review of 001): one place that turns SDK argument validation
errors into our tool error ``code=invalid_params`` (ADR-013).

Source facts (``mcp`` 2.3.0, 2026-10-27):

* ``Tool.run`` validates ``arguments`` with the pydantic model built from the signature; a
  ``ValidationError`` becomes ``ToolError(...) from exc`` (``mcp/server/mcpserver/tools/base.py``);
  crashes in the body become ``UnexpectedToolError`` (a ``ToolError`` subclass);
* ``MCPServer.call_tool(name, arguments, context)`` is public and documents
  "``except ToolError`` around ``MCPServer.call_tool()`` catches every tool failure"
  (``mcpserver/exceptions.py``); ``_handle_call_tool`` calls ``self.call_tool`` and only then turns
  a ``ToolError`` into a text-only ``CallToolResult`` - so an override of ``call_tool`` sees the
  typed exception, while an ``Extension.intercept_tool_call`` (wraps the handler) sees only text.

Result: overriding ``call_tool`` lets argument models carry the real constraints (``Literal``,
``pattern``, ``gt``), so ``inputSchema`` and runtime validation come from one declaration.
"""

from decimal import Decimal
from typing import Annotated, Any, Literal

from mcp import Client
from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolRequestParams, CallToolResult, InputRequiredResult, TextContent
from pydantic import AfterValidator, BaseModel, Field, ValidationError


def invalid_params_result(error: ValidationError) -> CallToolResult:
    """The shape ``interface/mcp/errors.py`` would build; never echoes the rejected values."""
    problems = [(".".join(str(p) for p in e["loc"]), e["type"], e["msg"]) for e in error.errors()]
    field, kind, _ = problems[0]
    # Built-in pydantic messages describe the rule, not the value. A custom validator's message
    # (type ``value_error``) is free text that may quote the input, so only loc + type are sent.
    parts = [f"{loc}: {t}" if t == "value_error" else f"{loc}: {msg}" for loc, t, msg in problems]
    payload = {
        "code": "invalid_params",
        "message": "; ".join(parts),
        "next_step": "Fix the arguments according to the tool's input schema and call again.",
        "details": {"field": field, "reason": kind},
    }
    return CallToolResult(
        content=[TextContent(type="text", text=str(payload))], structured_content=payload, is_error=True
    )


class ArgErrorMappingServer(MCPServer[Any]):
    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if isinstance(exc, UnexpectedToolError) or not isinstance(exc.__cause__, ValidationError):
                raise  # unknown tool, deliberate ToolError or crash: SDK handling stays as is
            return invalid_params_result(exc.__cause__)


class Seen(BaseModel):
    symbol: str
    category: str
    qty: str


def _no_secret(value: str) -> str:
    if "sk-" in value:
        raise ValueError(f"looks like an API key: {value}")  # a careless validator echoing the input
    return value


def register(mcp: MCPServer[Any]) -> None:
    @mcp.tool(name="place_order")
    async def place_order(
        symbol: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{2,30}$", description="e.g. BTCUSDT")],
        category: Literal["spot", "linear"],
        qty: Annotated[Decimal, Field(gt=0, description="base asset amount")],
    ) -> Seen:
        return Seen(symbol=symbol, category=category, qty=repr(qty))

    @mcp.tool(name="set_note")
    async def set_note(note: Annotated[str, AfterValidator(_no_secret)]) -> str:
        return note

    @mcp.tool(name="buggy")
    async def buggy() -> str:
        Seen.model_validate({})  # a pydantic error raised by our own code is a crash, not invalid_params
        return "unreachable"


def build(cls: type[MCPServer[Any]] = ArgErrorMappingServer, **kwargs: Any) -> MCPServer[Any]:
    mcp = cls("spike-002", **kwargs)
    register(mcp)
    return mcp


async def call(args: dict[str, Any], tool: str = "place_order") -> CallToolResult:
    async with Client(build()) as client:
        return await client.call_tool(tool, args)


async def test_input_schema_comes_from_the_same_annotations() -> None:
    async with Client(build()) as client:
        schema = (await client.list_tools()).tools[0].input_schema
    props = schema["properties"]
    assert schema["required"] == ["symbol", "category", "qty"]
    assert props["category"]["enum"] == ["spot", "linear"]
    assert props["symbol"]["pattern"] == "^[A-Za-z0-9]{2,30}$"
    # Decimal is published as number|string with a decimal-string pattern; ``gt`` becomes exclusiveMinimum.
    assert {"type": "number", "exclusiveMinimum": 0} in props["qty"]["anyOf"]


async def test_missing_argument_is_invalid_params() -> None:
    result = await call({"symbol": "BTCUSDT", "qty": "1"})
    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["code"] == "invalid_params"
    assert result.structured_content["details"] == {"field": "category", "reason": "missing"}


async def test_wrong_type_enum_pattern_and_range_are_invalid_params() -> None:
    cases = {
        "qty": ({"symbol": "BTCUSDT", "category": "spot", "qty": "abc"}, "decimal_parsing"),
        "category": ({"symbol": "BTCUSDT", "category": "inverse", "qty": "1"}, "literal_error"),
        "symbol": ({"symbol": "BTC/USDT", "category": "spot", "qty": "1"}, "string_pattern_mismatch"),
    }
    for field, (args, reason) in cases.items():
        result = await call(args)
        assert result.structured_content is not None
        assert result.structured_content["details"] == {"field": field, "reason": reason}
    range_error = await call({"symbol": "BTCUSDT", "category": "spot", "qty": "0"})
    assert range_error.structured_content is not None
    assert range_error.structured_content["details"] == {"field": "qty", "reason": "greater_than"}


async def test_json_number_becomes_exact_decimal_and_extra_arguments_are_ignored() -> None:
    result = await call({"symbol": "BTCUSDT", "category": "spot", "qty": 0.1, "confirmed": True})
    assert result.is_error is False
    # pydantic converts float via str(): 0.1 -> Decimal('0.1'). D-4 still wants strings on the wire.
    assert result.structured_content == {"symbol": "BTCUSDT", "category": "spot", "qty": "Decimal('0.1')"}


async def test_crash_and_unknown_tool_keep_sdk_handling() -> None:
    crash = await call({}, tool="buggy")
    assert crash.is_error is True and crash.structured_content is None
    assert crash.content == [TextContent(type="text", text="Error executing tool buggy")]
    unknown = await call({}, tool="nope")
    assert unknown.is_error is True and unknown.structured_content is None


async def test_extension_interceptor_only_sees_text_for_the_same_failure() -> None:
    seen: list[Any] = []

    class Spy(Extension):
        identifier = "dev.spike/spy"

        async def intercept_tool_call(
            self, params: CallToolRequestParams, ctx: ServerRequestContext[Any, Any], call_next: CallNext
        ) -> HandlerResult:
            result = await call_next(ctx)
            seen.append(result)
            return result

    async with Client(build(MCPServer, extensions=[Spy()])) as client:
        await client.call_tool("place_order", {"symbol": "BTCUSDT", "qty": "1"})
        advertised = client.server_capabilities.extensions
    (result,) = seen
    assert isinstance(result, CallToolResult) and result.structured_content is None
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text.startswith("Error executing tool place_order: 1 validation error")
    # Side effect: an Extension is also advertised to every client as a protocol extension.
    assert advertised is not None and "dev.spike/spy" in advertised


async def test_custom_validator_message_is_not_echoed() -> None:
    result = await call({"note": "my key sk-SECRET"}, tool="set_note")
    assert result.structured_content is not None
    assert result.structured_content["details"] == {"field": "note", "reason": "value_error"}
    assert result.structured_content["message"] == "note: value_error"
    assert "SECRET" not in str(result.structured_content)
