"""``GuardedMCPServer.call_tool``: only SDK argument validation becomes ``invalid_params``."""

from typing import Annotated, Any

import pytest
import structlog.testing
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field

from guarded_exchange.interface.mcp.guarded_server import GuardedMCPServer

pytestmark = pytest.mark.req("FR-28")


class _Strict(BaseModel):
    value: int


def _server() -> GuardedMCPServer:
    mcp = GuardedMCPServer("test")

    @mcp.tool(name="pydantic_crash")
    async def pydantic_crash() -> str:
        _Strict.model_validate({"value": "not a number"})  # our bug, not the caller's arguments
        return "unreachable"

    @mcp.tool(name="deliberate")
    async def deliberate() -> str:
        raise ToolError("deliberate failure")

    @mcp.tool(name="nested")
    async def nested(
        prices: Annotated[dict[str, Annotated[int, Field(ge=0)]], Field(description="name -> price")],
        steps: list[int],
    ) -> str:
        return "ok"

    return mcp


async def _call(name: str, args: dict[str, Any]) -> CallToolResult:
    async with Client(_server()) as client:
        return await client.call_tool(name, args)


async def test_validation_error_raised_inside_the_tool_body_is_not_invalid_params() -> None:
    result = await _call("pydantic_crash", {})

    assert result.is_error is True
    assert result.structured_content is None  # SDK crash handling: generic text, no details
    assert result.content == [TextContent(type="text", text="Error executing tool pydantic_crash")]


async def test_tool_error_raised_inside_the_tool_body_keeps_the_sdk_result() -> None:
    result = await _call("deliberate", {})

    assert result.is_error is True
    assert result.structured_content is None
    assert result.content == [
        TextContent(type="text", text="Error executing tool deliberate: deliberate failure")
    ]


async def test_nested_locations_name_the_argument_and_hide_caller_supplied_keys() -> None:
    result = await _call("nested", {"prices": {"sk-SECRET-KEY": -1}, "steps": [1, "x"]})

    assert result.structured_content is not None
    assert result.structured_content["code"] == "invalid_params"
    assert result.structured_content["details"] == {"field": "prices", "reason": "greater_than_equal"}
    message = result.structured_content["message"]
    assert "prices.*: Input should be greater than or equal to 0" in message
    assert "steps.1: Input should be a valid integer" in message
    assert "SECRET" not in result.model_dump_json()


async def test_logged_fields_are_argument_names_only() -> None:
    with structlog.testing.capture_logs() as logs:
        await _call("nested", {"prices": {"sk-SECRET-KEY": -1}, "steps": [1, "x"]})

    (entry,) = [e for e in logs if e["event"] == "tool_invalid_arguments"]
    assert entry["tool"] == "nested"
    assert entry["fields"] == ["prices", "steps"]
    assert "SECRET" not in str(logs)
