"""Provider adapters on SDK-shaped responses (fixtures in ``tests/fixtures/llm``), no network (ADR-016)."""

from typing import Any

import pytest
from anthropic.types import Message as AnthropicMessage
from openai.types.responses import Response

from guarded_agent.anthropic_provider import AnthropicProvider, to_anthropic_messages
from guarded_agent.openai_provider import OpenAIProvider, to_openai_input
from guarded_agent.provider import AssistantTurn, Message, ToolCall, ToolResult, ToolSpec, Usage, UserMessage
from tests.conftest import load_fixture

pytestmark = pytest.mark.req("FR-39")

TOOL = ToolSpec(
    name="get_positions",
    description="Open positions.",
    input_schema={"type": "object", "properties": {"symbol": {"type": "string"}}},
    read_only=True,
)
CALL = ToolCall(id="c1", name="get_positions", arguments={"symbol": "BTCUSDT"})
HISTORY: list[Message] = [
    UserMessage("my positions?"),
    AssistantTurn(text="", tool_calls=(CALL,)),
    ToolResult(call_id="c1", content='{"code": "forbidden"}', is_error=True),
]


class _Recorder:
    """Stands in for ``client.responses`` / ``client.messages``: records kwargs, returns a fixture."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> object:
        self.kwargs = kwargs
        return self.response


class _Client:
    def __init__(self, recorder: _Recorder) -> None:
        self.responses = self.messages = recorder


async def test_openai_function_call_and_usage_are_parsed_and_the_request_is_built() -> None:
    recorder = _Recorder(Response.model_validate(load_fixture("llm/openai_function_call.json")))
    provider = OpenAIProvider(_Client(recorder))  # type: ignore[arg-type]

    turn = await provider.complete("sys", HISTORY, [TOOL])

    assert turn.text == "Checking your positions first."
    assert turn.tool_calls == (
        ToolCall(id="call_pos_1", name="get_positions", arguments={"symbol": "BTCUSDT"}),
    )
    assert turn.usage == Usage(input_tokens=1104, cached_input_tokens=4096, output_tokens=42)
    sent = recorder.kwargs
    assert (sent["model"], sent["reasoning"], sent["store"]) == ("gpt-6-luna", {"effort": "none"}, False)
    assert sent["tools"][0] == {
        "type": "function",
        "name": "get_positions",
        "description": "Open positions.",
        "parameters": TOOL.input_schema,
        "strict": False,
    }


def test_openai_input_carries_tool_calls_and_marks_errors() -> None:
    items = to_openai_input(HISTORY)

    assert items[0] == {"role": "user", "content": "my positions?"}
    assert items[1] == {
        "type": "function_call",
        "call_id": "c1",
        "name": "get_positions",
        "arguments": '{"symbol": "BTCUSDT"}',
    }
    assert items[2] == {
        "type": "function_call_output",
        "call_id": "c1",
        "output": 'ERROR: {"code": "forbidden"}',
    }


async def test_anthropic_tool_use_and_usage_are_parsed_and_caching_is_on() -> None:
    recorder = _Recorder(AnthropicMessage.model_validate(load_fixture("llm/anthropic_tool_use.json")))
    provider = AnthropicProvider(_Client(recorder))  # type: ignore[arg-type]

    turn = await provider.complete("sys", HISTORY, [TOOL])

    assert turn.text == "Checking your positions first."
    assert turn.tool_calls == (
        ToolCall(id="toolu_01", name="get_positions", arguments={"symbol": "BTCUSDT"}),
    )
    assert turn.usage == Usage(input_tokens=310, cached_input_tokens=5100, output_tokens=48)
    sent = recorder.kwargs
    assert (sent["model"], sent["output_config"]) == ("claude-haiku-5-5", {"effort": "low"})
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["tools"][0]["input_schema"] == TOOL.input_schema


def test_anthropic_messages_group_tool_results_into_one_user_turn() -> None:
    two_calls = AssistantTurn(
        text="both", tool_calls=(CALL, ToolCall(id="c2", name="get_balances", arguments={}))
    )
    history: list[Message] = [
        UserMessage("hi"),
        two_calls,
        ToolResult("c1", "ok", is_error=False),
        ToolResult("c2", "bad", is_error=True),
    ]

    messages = to_anthropic_messages(history)

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assistant: Any = messages[1]["content"]
    assert assistant[0] == {"type": "text", "text": "both"}
    results: Any = messages[2]["content"]
    assert [(r["tool_use_id"], r["is_error"]) for r in results] == [("c1", False), ("c2", True)]
