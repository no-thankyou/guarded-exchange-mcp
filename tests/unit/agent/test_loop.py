"""The agent loop on a scripted provider and a fake tool session (FR-39, ADR-016)."""

from typing import Any

import pytest

from guarded_agent.loop import Agent
from guarded_agent.provider import AssistantTurn, ScriptedProvider, ToolCall, ToolResult, ToolSpec

pytestmark = pytest.mark.req("FR-39")


class FakeSession:
    def __init__(self, results: dict[str, ToolResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name=n, description="", input_schema={"type": "object"}) for n in self.results]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((name, arguments))
        return self.results[name]


def _call(name: str, call_id: str, **args: Any) -> AssistantTurn:
    return AssistantTurn(text="", tool_calls=(ToolCall(id=call_id, name=name, arguments=args),))


async def test_the_loop_calls_tools_and_feeds_an_is_error_result_back_to_the_model() -> None:
    session = FakeSession(
        {
            "get_positions": ToolResult("", '{"items": []}', is_error=False),
            "place_order": ToolResult("", '{"code": "limit_exceeded"}', is_error=True),
        }
    )
    provider = ScriptedProvider(
        [
            _call("get_positions", "c1"),
            _call("place_order", "c2", symbol="BTCUSDT", qty="5"),
            AssistantTurn(text="The order exceeds your limit."),
        ]
    )
    seen: list[ToolCall] = []

    run = await Agent(provider, session, on_tool_call=seen.append).ask("buy 5 BTC")

    assert session.calls == [("get_positions", {}), ("place_order", {"symbol": "BTCUSDT", "qty": "5"})]
    assert [s.result for s in run.steps] == [
        ToolResult("c1", '{"items": []}', is_error=False),
        ToolResult("c2", '{"code": "limit_exceeded"}', is_error=True),
    ]
    assert [c.id for c in seen] == ["c1", "c2"]
    assert run.text == "The order exceeds your limit."
    assert not run.hit_step_limit
    last_request = provider.requests[-1][1]
    assert last_request[-1] == ToolResult("c2", '{"code": "limit_exceeded"}', is_error=True)


async def test_the_loop_stops_at_the_step_limit() -> None:
    session = FakeSession({"get_ticker": ToolResult("", "{}", is_error=False)})
    provider = ScriptedProvider([_call("get_ticker", f"c{i}") for i in range(10)])

    run = await Agent(provider, session, max_steps=3).ask("loop forever")

    assert run.hit_step_limit
    assert len(session.calls) == 3


async def test_the_conversation_continues_across_requests() -> None:
    provider = ScriptedProvider([AssistantTurn(text="hi"), AssistantTurn(text="again")])
    agent = Agent(provider, FakeSession({}))

    await agent.ask("one")
    await agent.ask("two")

    assert len(provider.requests[-1][1]) == 3  # user, assistant, user
