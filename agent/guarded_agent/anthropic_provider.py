"""``LLMProvider`` on the Anthropic Messages API (``anthropic`` 2.x, tool use).

``claude-haiku-5-5`` with effort ``low`` by default; automatic prompt caching through the
top-level ``cache_control`` (the tool definitions and system prompt are the stable prefix).
"""

from collections.abc import Sequence
from typing import Any, Literal

from anthropic import AsyncAnthropic
from anthropic.types import Message as AnthropicMessage
from anthropic.types import MessageParam, ToolParam

from guarded_agent.provider import (
    AssistantTurn,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    UserMessage,
)

DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class AnthropicProvider:
    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        effort: Effort = "low",
        max_tokens: int = 1024,
    ) -> None:
        self._client = client
        self._model = model
        self._effort: Effort = effort
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AssistantTurn:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=to_anthropic_messages(messages),
            tools=[to_anthropic_tool(t) for t in tools],
            output_config={"effort": self._effort},
            cache_control={"type": "ephemeral"},
        )
        return from_anthropic_message(response)


def to_anthropic_tool(tool: ToolSpec) -> ToolParam:
    return ToolParam(name=tool.name, description=tool.description, input_schema=dict(tool.input_schema))


def to_anthropic_messages(messages: Sequence[Message]) -> list[MessageParam]:
    """Consecutive tool results go into one user message, as the Messages API requires."""
    out: list[MessageParam] = []
    results: list[Any] = []
    for message in messages:
        if isinstance(message, ToolResult):
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.call_id,
                    "content": message.content,
                    "is_error": message.is_error,
                }
            )
            continue
        if results:
            out.append({"role": "user", "content": results})
            results = []
        match message:
            case UserMessage(text=text):
                out.append({"role": "user", "content": text})
            case AssistantTurn(text=text, tool_calls=calls):
                blocks: list[Any] = [{"type": "text", "text": text}] if text else []
                blocks += [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": dict(c.arguments)}
                    for c in calls
                ]
                out.append({"role": "assistant", "content": blocks})
    if results:
        out.append({"role": "user", "content": results})
    return out


def from_anthropic_message(response: AnthropicMessage) -> AssistantTurn:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in response.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))
    usage = response.usage
    return AssistantTurn(
        text="".join(texts),
        tool_calls=tuple(calls),
        usage=Usage(
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
            output_tokens=usage.output_tokens,
        ),
    )
