"""The ``LLMProvider`` port and the provider-neutral conversation it works on (ADR-016).

Adapters translate these types to and from their SDK; nothing SDK-specific leaves an adapter.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool as the model sees it: the MCP name, description and ``inputSchema``."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    read_only: bool = False
    """``annotations.readOnlyHint``; a tool without it is treated as changing something."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens of one model call; ``input_tokens`` excludes the cached and cache-write parts."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class UserMessage:
    text: str


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    """One model answer: text, tool calls or both."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool


Message = UserMessage | AssistantTurn | ToolResult


class LLMProvider(Protocol):
    """One model call over the whole conversation so far."""

    @property
    def model(self) -> str: ...

    async def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AssistantTurn: ...


class ScriptedProvider:
    """A provider replaying prepared turns: for tests and the offline evals smoke run.

    When the script runs out it answers with ``fallback`` text and no tool calls.
    """

    def __init__(
        self,
        turns: Sequence[AssistantTurn] = (),
        *,
        fallback: str = "I will not do that.",
        model: str = "fake",
    ) -> None:
        self._turns = list(turns)
        self._fallback = fallback
        self._model = model
        self.requests: list[tuple[str, tuple[Message, ...], tuple[ToolSpec, ...]]] = []

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AssistantTurn:
        self.requests.append((system, tuple(messages), tuple(tools)))
        if self._turns:
            return self._turns.pop(0)
        return AssistantTurn(text=self._fallback)
