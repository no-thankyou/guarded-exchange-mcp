"""``LLMProvider`` on the OpenAI Responses API (``openai`` 3.x, function tools).

``gpt-6-luna`` with reasoning effort ``none``: no reasoning items have to be carried between
calls, so the conversation is rebuilt from our neutral messages each time (``store=False``).
"""

import json
from collections.abc import Sequence
from typing import Any

from openai import AsyncOpenAI
from openai.types.responses import FunctionToolParam, Response, ResponseInputItemParam
from openai.types.shared import ReasoningEffort
from openai.types.shared_params import Reasoning

from guarded_agent.provider import (
    AssistantTurn,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    UserMessage,
)

DEFAULT_OPENAI_MODEL = "gpt-6-luna"


class OpenAIProvider:
    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str = DEFAULT_OPENAI_MODEL,
        effort: ReasoningEffort = "none",
        max_output_tokens: int = 1024,
        prompt_cache_key: str | None = "guarded-agent",
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_output_tokens = max_output_tokens
        self._cache_key = prompt_cache_key

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AssistantTurn:
        extra: dict[str, Any] = {} if self._cache_key is None else {"prompt_cache_key": self._cache_key}
        response = await self._client.responses.create(
            model=self._model,
            instructions=system,
            input=to_openai_input(messages),
            tools=[to_openai_tool(t) for t in tools],
            reasoning=Reasoning(effort=self._effort),
            max_output_tokens=self._max_output_tokens,
            store=False,
            **extra,
        )
        return from_openai_response(response)


def to_openai_tool(tool: ToolSpec) -> FunctionToolParam:
    # Not strict: strict mode demands every property in ``required``, which MCP schemas do not do.
    return FunctionToolParam(
        type="function",
        name=tool.name,
        description=tool.description,
        parameters=dict(tool.input_schema),
        strict=False,
    )


def to_openai_input(messages: Sequence[Message]) -> list[ResponseInputItemParam]:
    items: list[ResponseInputItemParam] = []
    for message in messages:
        match message:
            case UserMessage(text=text):
                items.append({"role": "user", "content": text})
            case AssistantTurn(text=text, tool_calls=calls):
                if text:
                    items.append({"role": "assistant", "content": text})
                items.extend(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    }
                    for call in calls
                )
            case ToolResult(call_id=call_id, content=content, is_error=is_error):
                # Responses has no error flag for function output; the prefix carries it.
                output = f"ERROR: {content}" if is_error else content
                items.append({"type": "function_call_output", "call_id": call_id, "output": output})
    return items


def from_openai_response(response: Response) -> AssistantTurn:
    calls = tuple(
        ToolCall(id=item.call_id, name=item.name, arguments=_parse_arguments(item.arguments))
        for item in response.output
        if item.type == "function_call"
    )
    usage = Usage()
    if response.usage is not None:
        details = response.usage.input_tokens_details
        cached, written = details.cached_tokens, details.cache_write_tokens
        usage = Usage(
            input_tokens=response.usage.input_tokens - cached - written,
            cached_input_tokens=cached,
            cache_write_tokens=written,
            output_tokens=response.usage.output_tokens,
        )
    return AssistantTurn(text=response.output_text, tool_calls=calls, usage=usage)


def _parse_arguments(raw: str) -> dict[str, Any]:
    """Malformed arguments become ``{}``: the server then refuses the call with ``invalid_params``."""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
