"""The console agent against the real server in-process, on a scripted provider (FR-39, AC-5).

The confirmation demo goes through the client-UI channel (ticket 020, MRTR): the console asks the human,
the model never sees the form. Without that channel the server fails closed with
``confirmation_channel_unavailable`` and the agent passes that to the model, not crash.
"""

import pytest
from mcp import Client

from guarded_agent.console import ConsoleElicitation
from guarded_agent.loop import Agent, AgentRun, McpToolSession
from guarded_agent.provider import AssistantTurn, ScriptedProvider, ToolCall
from guarded_exchange.application.confirmation import ConfirmationSupport
from guarded_exchange.application.credentials import OpenCredentialKeys
from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.credentials import ApiKeyPair, KeyPermissions
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from tests.e2e.support import server_for
from tests.fakes import (
    FakeAccount,
    FakeCipher,
    FakeClock,
    FakeExchange,
    FakeTrading,
    InMemoryDatabase,
    make_position,
    make_rules,
    make_ticker,
)

pytestmark = pytest.mark.req("FR-39", "AC-5")

KEYS = ApiKeyPair(api_key="E2EAGENTKEY01234", api_secret="e2e-agent-secret-abcdefghijklmnopqr")  # noqa: S106
TRADING_KEY = KeyPermissions(
    read_only=False,
    permissions=frozenset({"ContractTrade.Order", "ContractTrade.Position"}),
    expires_at=None,
    ip_restricted=True,
)
BUY = {"symbol": "BTCUSDT", "category": "linear", "side": "buy", "order_type": "market", "qty": "0.001"}
SCRIPT = [
    AssistantTurn(text="", tool_calls=(ToolCall("c1", "get_positions", {}),)),
    AssistantTurn(text="", tool_calls=(ToolCall("c2", "place_order", BUY),)),
    AssistantTurn(text="Done."),
]


async def _demo(
    *, confirm: bool, answers: list[str], ui: bool = False
) -> tuple[AgentRun, FakeTrading, list[str]]:
    db, clock, cipher = InMemoryDatabase(), FakeClock(), FakeCipher()
    user = db.add_user()
    db.add_credential(user, cipher, KEYS, permissions=TRADING_KEY)
    trading = FakeTrading(db=db)
    exchange = FakeExchange(
        tickers={("BTCUSDT", Category.LINEAR): make_ticker()},
        rules={("BTCUSDT", Category.LINEAR): make_rules()},
    )
    server = server_for(
        db,
        user.id,
        exchange=exchange,
        account=FakeAccount(positions=(make_position(),)),
        cipher=cipher,
        trading=trading,
        clock=clock,
        confirmations=ConfirmationSupport(cli_enabled=False, open_keys=OpenCredentialKeys(db, cipher))
        if ui
        else None,
    )
    policy = PolicyPatch(
        profile=Profile.TRADE,
        allowlist=frozenset({InstrumentRef(Category.LINEAR, "BTCUSDT")}),
        confirm_actions=frozenset({ActionKind.PLACE_ORDER}) if confirm else frozenset(),
    )
    await SetCeiling(db, clock)(user.name, policy)
    said: list[str] = []
    console = ConsoleElicitation(ask=lambda _: answers.pop(0), say=said.append)
    async with Client(server, elicitation_callback=console) as client:
        run = await Agent(ScriptedProvider(list(SCRIPT)), McpToolSession(client)).ask("buy 0.001 BTC")
    return run, trading, said


def _sent(trading: FakeTrading) -> int:
    return len([c for c in trading.calls if c[0] == "place_order"])


async def test_the_agent_reads_positions_and_places_an_order_without_confirmation() -> None:
    run, trading, said = await _demo(confirm=False, answers=[])

    assert [(s.call.name, s.result.is_error) for s in run.steps] == [
        ("get_positions", False),
        ("place_order", False),
    ]
    assert '"status": "accepted"' in run.steps[1].result.content
    assert _sent(trading) == 1
    assert said == []
    assert run.text == "Done."


async def test_without_a_ui_channel_the_agent_gets_confirmation_channel_unavailable() -> None:
    run, trading, said = await _demo(confirm=True, answers=[])

    result = run.steps[1].result
    assert result.is_error
    assert "confirmation_channel_unavailable" in result.content
    assert _sent(trading) == 0
    assert said == []


async def test_ac5_the_human_confirms_in_the_console_and_the_order_is_sent() -> None:
    run, trading, said = await _demo(confirm=True, answers=["a", "y"], ui=True)

    assert said
    assert "BTCUSDT" in said[0]
    assert not run.steps[1].result.is_error
    assert _sent(trading) == 1


async def test_ac5_a_declined_confirmation_sends_nothing() -> None:
    run, trading, said = await _demo(confirm=True, answers=["d"], ui=True)

    assert said
    assert run.steps[1].result.is_error
    assert _sent(trading) == 0
