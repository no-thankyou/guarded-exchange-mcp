"""The console elicitation form (FR-39, AC-5): the human's typed answers become the ``ElicitResult``."""

from collections.abc import Callable
from typing import Any

import pytest
from mcp.types import ElicitRequestFormParams, ElicitRequestURLParams, ElicitResult

from guarded_agent.console import ConsoleElicitation

pytestmark = pytest.mark.req("FR-39", "AC-5")

CONFIRM_FORM = ElicitRequestFormParams(
    message="Place order: buy 0.001 BTCUSDT market, ~81.75 USDT?",
    requested_schema={
        "type": "object",
        "properties": {
            "confirm": {"type": "boolean", "title": "Confirm"},
            "note": {"type": "string"},
            "size": {"type": "integer", "enum": [1, 2]},
        },
        "required": ["confirm"],
    },
)


def _answers(*lines: str) -> Callable[[str], str]:
    queue = list(lines)

    def ask(_: str) -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    return ask


async def _elicit(params: Any, *lines: str) -> tuple[ElicitResult, list[str]]:
    said: list[str] = []
    result = await ConsoleElicitation(ask=_answers(*lines), say=said.append)(None, params)  # type: ignore[arg-type]
    return result, said


async def test_accept_fills_the_fields_with_typed_values() -> None:
    result, said = await _elicit(CONFIRM_FORM, "a", "maybe", "y", "", "3", "2")

    assert result == ElicitResult(action="accept", content={"confirm": True, "size": 2})
    assert "81.75 USDT" in said[0]
    assert any("y or n" in s for s in said)  # "maybe" was asked again
    assert any("not a valid value" in s for s in said)  # 3 is not in the enum


@pytest.mark.parametrize(("choice", "action"), [("d", "decline"), ("c", "cancel"), ("whatever", "cancel")])
async def test_decline_and_cancel(choice: str, action: str) -> None:
    result, _ = await _elicit(CONFIRM_FORM, choice)

    assert result == ElicitResult(action=action)


async def test_end_of_input_cancels() -> None:
    result, _ = await _elicit(CONFIRM_FORM, "a")

    assert result.action == "cancel"


async def test_url_mode_only_prints_the_link() -> None:
    params = ElicitRequestURLParams(message="Confirm in the browser", url="https://example.test/c/1")

    result, said = await _elicit(params, "n")

    assert result.action == "decline"
    assert any("https://example.test/c/1" in s for s in said)
