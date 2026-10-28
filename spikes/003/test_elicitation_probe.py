"""Smoke tests for the spike 003 probe server: in-process ``Client`` plus one real stdio run.

These do not replace the manual check (the point of spike 003 is how *real* clients behave); they
prove the probe itself is correct before the user spends time on it. Run:
``uv run pytest spikes/003 -v``.
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from elicitation_probe import LOG_FILE_ENV, build_server, supports_form
from mcp import Client
from mcp.client import ClientRequestContext
from mcp.client.stdio import StdioServerParameters
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequestFormParams,
    ElicitRequestParams,
    ElicitResult,
    ErrorData,
    FormElicitationCapability,
    InputRequiredResult,
    InputResponses,
    UrlElicitationCapability,
)

PROBE = Path(__file__).with_name("elicitation_probe.py")
TOOL = "confirm_test_order"


class Events:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def __call__(self, event: str, **fields: Any) -> None:
        self.items.append({"event": event, **fields})

    def named(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.items if e["event"] == event]


def answering(action: str, content: dict[str, Any] | None, seen: list[ElicitRequestParams]) -> Any:
    async def callback(
        context: ClientRequestContext, params: ElicitRequestParams
    ) -> ElicitResult | ErrorData:
        seen.append(params)
        return ElicitResult(action=action, content=content)

    return callback


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_accept_confirms_and_logs_client_facts(mode: str) -> None:
    events, seen = Events(), list[ElicitRequestParams]()
    callback = answering("accept", {"confirm": True}, seen)
    async with Client(build_server(events), mode=mode, elicitation_callback=callback) as client:
        result = await client.call_tool(TOOL, {})

    expected_channel = "mrtr" if mode == "auto" else "legacy_push"
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["confirmed"] is True
    assert result.structured_content["channel"] == expected_channel
    assert result.structured_content["decision"] == "accept"
    assert len(seen) == 1 and isinstance(seen[0], ElicitRequestFormParams)

    first = events.named("tools_call")[0]
    assert first["protocol_version"] == ("2026-07-28" if mode == "auto" else "2025-11-25")
    assert first["client_info"]["name"]  # the Python client identifies itself
    assert first["client_capabilities_elicitation"] == {"form": {}, "url": {}}
    assert first["supports_form"] is True
    [answer] = events.named("elicitation_result")
    assert answer["action"] == "accept" and answer["content"] == {"confirm": True}
    assert [e["pending_id"] for e in events.named("pending_created")] == ["p-1"]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(
    ("action", "content"),
    [("decline", None), ("cancel", None), ("accept", {"confirm": False})],
    ids=["decline", "cancel", "accept-confirm-false"],
)
async def test_anything_but_accept_with_confirm_true_refuses(
    mode: str, action: str, content: dict[str, Any] | None
) -> None:
    events = Events()
    callback = answering(action, content, [])
    async with Client(build_server(events), mode=mode, elicitation_callback=callback) as client:
        result = await client.call_tool(TOOL, {"symbol": "ETHUSDT", "qty": "0.1"})

    assert result.structured_content is not None
    assert result.structured_content["confirmed"] is False
    assert result.structured_content["decision"] == action
    [answer] = events.named("elicitation_result")
    assert answer["action"] == action


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_client_without_elicitation_is_refused_before_any_pending(mode: str) -> None:
    events = Events()
    async with Client(build_server(events), mode=mode) as client:
        result = await client.call_tool(TOOL, {})

    # Not the SDK's -32600 "Elicitation not supported": the server never asked.
    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["code"] == "confirmation_channel_unavailable"
    assert events.named("tools_call")[0]["client_capabilities_elicitation"] is None
    assert events.named("pending_created") == []
    assert events.named("input_required_sent") == []


async def test_second_call_gets_a_new_pending() -> None:
    events = Events()
    callback = answering("accept", {"confirm": True}, [])
    async with Client(build_server(events), elicitation_callback=callback) as client:
        first = await client.call_tool(TOOL, {})
        second = await client.call_tool(TOOL, {})
    assert first.structured_content is not None and second.structured_content is not None
    assert [first.structured_content["pending_id"], second.structured_content["pending_id"]] == ["p-1", "p-2"]


async def test_answers_without_state_are_invalid_params_and_create_nothing() -> None:
    events = Events()
    accept: InputResponses = {"confirm": ElicitResult(action="accept", content={"confirm": True})}
    async with Client(build_server(events), elicitation_callback=answering("accept", None, [])) as client:
        result = await client.session.call_tool(TOOL, {}, input_responses=accept)
    assert result.structured_content is not None
    assert result.structured_content["code"] == "invalid_params"
    assert events.named("pending_created") == []


async def test_state_only_retry_reasks_for_the_same_pending() -> None:
    events = Events()
    async with Client(build_server(events), elicitation_callback=answering("accept", None, [])) as client:
        first = await client.session.call_tool(TOOL, {}, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        again = await client.session.call_tool(
            TOOL, {}, request_state=first.request_state, allow_input_required=True
        )
    assert isinstance(again, InputRequiredResult)
    assert [e["pending_id"] for e in events.named("pending_created")] == ["p-1"]
    assert [e["pending_id"] for e in events.named("input_required_sent")] == ["p-1", "p-1"]


@pytest.mark.parametrize(
    ("elicitation", "expected"),
    [
        (None, False),
        (ElicitationCapability(), True),  # bare {}: form only, the pre-modes shape
        (ElicitationCapability(form=FormElicitationCapability()), True),
        (ElicitationCapability(url=UrlElicitationCapability()), False),
    ],
    ids=["absent", "bare", "form", "url-only"],
)
def test_supports_form(elicitation: ElicitationCapability | None, expected: bool) -> None:
    assert supports_form(ClientCapabilities(elicitation=elicitation)) is expected


async def test_real_stdio_process_logs_to_the_file(tmp_path: Path) -> None:
    log_file = tmp_path / "probe.log"
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(PROBE)],
        env={LOG_FILE_ENV: str(log_file), "PYTHONPATH": ""},
        cwd=tmp_path,
    )
    callback = answering("accept", {"confirm": True}, [])
    async with Client(params, elicitation_callback=callback, read_timeout_seconds=20) as client:
        tools = await client.list_tools()
        result = await client.call_tool(TOOL, {})

    assert [t.name for t in tools.tools] == [TOOL]
    assert result.structured_content is not None
    assert result.structured_content["confirmed"] is True
    events = [json.loads(line) for line in log_file.read_text().splitlines()]
    names = [e["event"] for e in events]
    assert names[0] == "server_start"
    assert {"tools_call", "pending_created", "elicitation_result", "decision"} <= set(names)


async def test_force_ask_sends_the_form_without_capability_and_logs_the_outcome() -> None:
    # Diagnostic switch for the manual run (does a client that declares nothing still answer?).
    events = Events()
    async with Client(build_server(events, force_ask=True), mode="legacy") as client:
        result = await client.call_tool(TOOL, {})
    assert [e["forced"] for e in events.named("pending_created")] == [True]
    # The Python client without a callback answers the push request with an error.
    assert len(events.named("elicitation_error")) == 1
    assert result.structured_content is not None
    assert result.structured_content["decision"] == "error"
    assert result.structured_content["confirmed"] is False
