"""The client-UI confirmation channel (MRTR) through an in-process MCP ``Client`` (ticket 020; SPEC FR-20 -
FR-22, FR-42, FR-47 (a), AC-1; PLAN ADR-005 (13), D-1, D-2, D-4, D-5, D-6).

``elicitation_callback`` plays the human (or the client's UI); the manual loop (``client.session``) plays a
client that replays, alters or invents rounds."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from mcp import Client
from mcp.client import ClientRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    ElicitRequestFormParams,
    ElicitRequestParams,
    ElicitResult,
    InputRequiredResult,
    InputResponses,
)

from guarded_exchange.application.client_ui_confirmation import UiAnswer
from guarded_exchange.application.confirmation import ConfirmationSupport
from guarded_exchange.application.credentials import OpenCredentialKeys
from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.application.resolve_confirmation import ResolveConfirmation
from guarded_exchange.domain.account import SymbolPosition
from guarded_exchange.domain.actions import ALL_ACTION_KINDS, ActionKind
from guarded_exchange.domain.confirmation import ConfirmationStatus, DecisionChannel
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from guarded_exchange.infrastructure.crypto import KeyPurpose, Keyring
from guarded_exchange.interface.mcp.access import fixed_principal
from guarded_exchange.interface.mcp.server import UseCases, create_server
from tests.e2e.support import use_cases
from tests.e2e.test_mcp_place_order import KEYS, MARKET_BUY, TRADING_KEY
from tests.fakes import (
    FakeCipher,
    FakeClock,
    FakeExchange,
    FakeTrading,
    InMemoryDatabase,
    make_position,
    make_rules,
    make_ticker,
)
from tests.fakes_trading import FakePositionActions

pytestmark = pytest.mark.req("FR-20", "FR-21", "FR-22", "FR-42", "FR-47", "AC-1")

STOP = {"symbol": "BTCUSDT", "category": "linear", "stop_loss": "78510.2"}  # 3% below the entry 80938.4
NOT_TICKED = "form submitted without confirming"
ACCEPT = ElicitResult(action="accept", content={"confirm": True})
ANSWERS: InputResponses = {"confirm": ACCEPT}


class Setup:
    def __init__(self, *, cli: bool = False, key: bytes = b"k" * 32) -> None:
        self.db, self.clock, self.cipher = InMemoryDatabase(), FakeClock(), FakeCipher()
        self.user = self.db.add_user()
        self.db.add_credential(self.user, self.cipher, KEYS, permissions=TRADING_KEY)
        self.trading = FakeTrading(db=self.db)
        self.trading.positions["BTCUSDT"] = SymbolPosition("BTCUSDT", make_position(), Decimal(10))
        self.actions = FakePositionActions(self.trading)
        self.exchange = FakeExchange(
            tickers={("BTCUSDT", Category.LINEAR): make_ticker()},
            rules={("BTCUSDT", Category.LINEAR): make_rules()},
        )
        self.support = ConfirmationSupport(
            cli_enabled=cli, open_keys=OpenCredentialKeys(self.db, self.cipher)
        )
        self.cases = self.build()
        self.key = key

    def build(self) -> UseCases:
        return use_cases(
            self.db,
            self.exchange,
            self.clock,
            cipher=self.cipher,
            trading=self.trading,
            position_actions=self.actions,
            confirmations=self.support,
        )

    def server(self, key: bytes | None = None) -> Any:
        """A fresh server over the same storage and key: what a restarted process is."""
        return create_server(
            self.cases, principal=fixed_principal(self.user.id), request_state_key=key or self.key
        )

    async def grant(self, **extra: Any) -> None:
        patch = PolicyPatch(
            profile=Profile.TRADE,
            allowlist=frozenset({InstrumentRef(Category.LINEAR, "BTCUSDT")}),
            confirm_actions=ALL_ACTION_KINDS,
            **extra,
        )
        await SetCeiling(self.db, self.clock)(self.user.name, patch)

    def pendings(self) -> list[Any]:
        return list(self.db.state.confirmations.values())

    def decisions(self) -> list[Any]:
        return [r for r in self.db.all_audit() if r.event_type == "confirmation_decided"]


class Human:
    """An ``elicitation_callback``: records the forms and answers with ``result``."""

    def __init__(self, result: ElicitResult) -> None:
        self.result = result
        self.forms: list[ElicitRequestParams] = []

    async def __call__(self, context: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult:
        self.forms.append(params)
        return self.result


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None, result.content
    return dict(result.structured_content)


async def _first_round(
    client: Client, tool: str = "set_trading_stop", args: dict[str, Any] | None = None
) -> Any:
    first = await client.session.call_tool(tool, args or STOP, allow_input_required=True)
    assert isinstance(first, InputRequiredResult), first
    assert first.request_state is not None
    return first


# --- AC-1 ------------------------------------------------------------------------------------


async def test_ac1_positions_then_stop_loss_is_confirmed_in_the_client_ui_and_journaled() -> None:
    setup = Setup()
    await setup.grant()
    human = Human(ACCEPT)
    async with Client(setup.server(), elicitation_callback=human) as client:
        positions = _structured(await client.call_tool("get_positions", {}))
        assert positions
        result = await client.call_tool("set_trading_stop", STOP)

    out = _structured(result)
    assert result.is_error is False
    assert out["status"] == "executed"
    assert out["tool"] == "set_trading_stop"
    assert setup.actions.stops == [("BTCUSDT", None, Decimal("78510.2"))]
    # The human saw what will be executed, and the hint for the box.
    (form,) = human.forms
    assert isinstance(form, ElicitRequestFormParams)
    assert "BTCUSDT" in form.message
    assert "78510.2" in form.message
    assert "Press Space" in form.message
    # The journal records the channel of the decision (FR-24, FR-42).
    (decision,) = setup.decisions()
    assert (decision.payload["status"], decision.payload["channel"]) == ("approved", "client_ui")
    assert decision.actor == "client_ui:user"
    (pending,) = setup.pendings()
    assert pending.status is ConfirmationStatus.EXECUTED
    assert pending.channel is DecisionChannel.CLIENT_UI


async def test_place_order_goes_through_the_same_form() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        out = _structured(await client.call_tool("place_order", MARKET_BUY))

    assert out["status"] == "executed"
    assert len(setup.trading.sent) == 1


@pytest.mark.parametrize(
    ("answer", "status", "journaled"),
    [
        (ElicitResult(action="decline"), "rejected", "declined by client UI"),
        (ElicitResult(action="cancel"), "cancelled", "form closed by the user"),
        (
            ElicitResult(action="accept", content={"confirm": False}),
            "rejected",
            "form submitted without confirming",
        ),
        (ElicitResult(action="accept", content={}), "rejected", "form submitted without confirming"),
        (ElicitResult(action="accept"), "rejected", "form submitted without confirming"),
        (
            ElicitResult(action="accept", content={"confirm": "true"}),
            "rejected",
            "form submitted without confirming",
        ),
    ],
)
async def test_anything_but_accept_with_confirm_true_is_not_a_confirmation(
    answer: ElicitResult, status: str, journaled: str
) -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(answer)) as client:
        result = await client.call_tool("set_trading_stop", STOP)

    assert result.is_error is True
    assert _structured(result)["code"] == "confirmation_declined"
    assert setup.actions.stops == []
    (pending,) = setup.pendings()
    assert pending.status.value == status
    (decision,) = setup.decisions()
    assert decision.payload["note"] == journaled
    assert decision.payload["channel"] == "client_ui"


# --- the second round is bound to the pending (FR-22) ------------------------------------------


async def test_a_second_round_with_other_arguments_is_refused_and_nothing_is_executed() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        first = await _first_round(client)
        with pytest.raises(MCPError) as altered:
            await client.session.call_tool(
                "set_trading_stop",
                {**STOP, "stop_loss": "1.5"},
                input_responses=ANSWERS,
                request_state=first.request_state,
            )

    assert altered.value.error.code == -32602  # the sealed state binds the raw arguments
    assert setup.actions.stops == []
    (pending,) = setup.pendings()
    assert pending.status is ConfirmationStatus.PENDING


async def test_a_state_minted_for_one_tool_cannot_confirm_another() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        first = await _first_round(client)
        with pytest.raises(MCPError) as other_tool:
            await client.session.call_tool(
                "close_position",
                {"symbol": "BTCUSDT", "category": "linear"},
                input_responses=ANSWERS,
                request_state=first.request_state,
            )

    assert other_tool.value.error.code == -32602
    assert setup.actions.stops == []
    assert setup.trading.sent == []


async def test_input_responses_without_request_state_are_refused_and_create_nothing() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        result = await client.session.call_tool("set_trading_stop", STOP, input_responses=ANSWERS)
        assert isinstance(result, CallToolResult)

    assert result.is_error is True
    assert _structured(result)["code"] == "invalid_params"
    assert setup.pendings() == []
    assert setup.actions.stops == []


async def test_a_state_only_retry_asks_again_about_the_same_pending() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        first = await _first_round(client)
        again = await client.session.call_tool(
            "set_trading_stop", STOP, request_state=first.request_state, allow_input_required=True
        )
        repeat = await _first_round(client)  # a fresh first round for the same call: same pending (D-20)

    assert isinstance(again, InputRequiredResult)
    assert again.request_state  # sealed afresh each time (random nonce), same pending behind it
    assert repeat.request_state
    assert len(setup.pendings()) == 1
    assert setup.actions.stops == []


async def test_repeating_the_second_round_after_execution_returns_the_result_and_executes_once() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        first = await _first_round(client)
        results = [
            await client.session.call_tool(
                "set_trading_stop", STOP, input_responses=ANSWERS, request_state=first.request_state
            )
            for _ in range(3)
        ]

    outs = [_structured(r) for r in results if isinstance(r, CallToolResult)]
    assert [o["status"] for o in outs] == ["executed"] * 3
    assert outs[0]["result"] == outs[1]["result"] == outs[2]["result"]
    assert len(setup.actions.stops) == 1
    assert len(setup.decisions()) == 1


async def test_a_declined_confirmation_cannot_be_revived_by_replaying_an_accept() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        first = await _first_round(client)
        no = {"confirm": ElicitResult(action="decline")}
        await client.session.call_tool(
            "set_trading_stop", STOP, input_responses=no, request_state=first.request_state
        )
        replay = await client.session.call_tool(
            "set_trading_stop", STOP, input_responses=ANSWERS, request_state=first.request_state
        )

    assert isinstance(replay, CallToolResult)

    assert replay.is_error is True
    assert setup.actions.stops == []


async def test_a_restart_between_the_rounds_keeps_the_form_valid_with_the_same_key() -> None:
    key = Keyring.single(b"m" * 32).subkey(KeyPurpose.REQUEST_STATE)
    setup = Setup(key=key)
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as before:
        first = await _first_round(before)
    # A new server object over the same storage and a key derived again from the same master key.
    restarted = setup.server()
    async with Client(restarted, elicitation_callback=Human(ACCEPT)) as after:
        result = await after.session.call_tool(
            "set_trading_stop", STOP, input_responses=ANSWERS, request_state=first.request_state
        )
    assert isinstance(result, CallToolResult)
    assert _structured(result)["status"] == "executed"

    other = setup.server(b"x" * 32)
    async with Client(other, elicitation_callback=Human(ACCEPT)) as stranger:
        with pytest.raises(MCPError):
            await stranger.session.call_tool(
                "set_trading_stop", STOP, input_responses=ANSWERS, request_state=first.request_state
            )


# --- which channel (FR-42, D-6) ----------------------------------------------------------------


@pytest.mark.parametrize("extra", [{"trust_client_ui": False}, {"prefer_external_channel": True}])
async def test_a_policy_that_does_not_trust_the_ui_never_shows_the_form(extra: dict[str, Any]) -> None:
    setup = Setup(cli=False)
    await setup.grant(**extra)
    human = Human(ACCEPT)
    async with Client(setup.server(), elicitation_callback=human) as client:
        result = await client.call_tool("set_trading_stop", STOP)

    assert result.is_error is True
    assert _structured(result)["code"] == "confirmation_channel_unavailable"
    assert human.forms == []
    assert setup.pendings() == []


@pytest.mark.parametrize("extra", [{"trust_client_ui": False}, {"prefer_external_channel": True}])
async def test_with_the_cli_enabled_such_a_policy_parks_the_action_for_the_cli(extra: dict[str, Any]) -> None:
    setup = Setup(cli=True)
    await setup.grant(**extra)
    human = Human(ACCEPT)
    async with Client(setup.server(), elicitation_callback=human) as client:
        out = _structured(await client.call_tool("set_trading_stop", STOP))

    assert out["status"] == "pending"
    assert human.forms == []


async def test_a_client_without_elicitation_gets_the_pending_answer_and_the_cli_can_approve() -> None:
    setup = Setup(cli=True)
    await setup.grant()
    async with Client(setup.server()) as client:
        out = _structured(await client.call_tool("set_trading_stop", STOP))
    assert out["status"] == "pending"

    decided = await ResolveConfirmation(setup.db, setup.clock, cli_enabled=True).approve(
        out["confirmation_id"]
    )
    assert decided.channel is DecisionChannel.CLI


async def test_a_client_without_elicitation_and_no_cli_fails_closed() -> None:
    setup = Setup(cli=False)
    await setup.grant()
    async with Client(setup.server()) as client:
        result = await client.call_tool("set_trading_stop", STOP)

    assert _structured(result)["code"] == "confirmation_channel_unavailable"
    assert setup.pendings() == []


async def test_a_policy_change_between_the_rounds_makes_the_form_answer_void() -> None:
    setup = Setup(cli=True)
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        first = await _first_round(client)
        await setup.grant(prefer_external_channel=True)
        result = await client.session.call_tool(
            "set_trading_stop", STOP, input_responses=ANSWERS, request_state=first.request_state
        )

    assert isinstance(result, CallToolResult)

    assert result.is_error is True
    assert _structured(result)["code"] == "confirmation_channel_unavailable"
    (pending,) = setup.pendings()
    assert pending.status is ConfirmationStatus.PENDING  # still there for the CLI
    assert setup.actions.stops == []


async def test_the_form_answer_after_the_expiry_executes_nothing() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        first = await _first_round(client)
        setup.clock.advance(timedelta(hours=1))
        result = await client.session.call_tool(
            "set_trading_stop", STOP, input_responses=ANSWERS, request_state=first.request_state
        )

    assert isinstance(result, CallToolResult)

    assert _structured(result)["code"] == "confirmation_expired"
    assert setup.actions.stops == []


# --- one execution path: the UI and the CLI race (D-2) -----------------------------------------


async def test_a_form_accept_and_a_cli_approve_at_the_same_time_execute_once() -> None:
    setup = Setup(cli=True)
    await setup.grant()
    async with Client(setup.server()) as client:
        out = _structured(await client.call_tool("set_trading_stop", STOP))
    (pending,) = setup.pendings()
    ui = setup.cases.client_ui
    assert ui is not None

    outcomes = await asyncio.gather(
        ui.decide(
            setup.user.id,
            ActionKind.SET_TRADING_STOP,
            pending.id,
            pending.terms_digest,
            UiAnswer.CONFIRM,
            note="n",
        ),
        ResolveConfirmation(setup.db, setup.clock, cli_enabled=True).approve(out["confirmation_id"]),
        return_exceptions=True,
    )
    await ui.execute((setup.pendings())[0])  # what the background executor does for a CLI approval

    (decision,) = setup.decisions()
    winner = decision.payload["channel"]
    assert winner in {"client_ui", "cli"}
    assert len(setup.actions.stops) == 1
    assert (setup.pendings())[0].status is ConfirmationStatus.EXECUTED
    assert (setup.pendings())[0].channel.value == winner  # type: ignore[union-attr]
    assert sum(isinstance(o, Exception) for o in outcomes) <= 1  # the loser is told, or just reads the state


async def test_a_cli_approval_first_leaves_the_form_answer_with_the_state_it_found() -> None:
    setup = Setup(cli=True)
    await setup.grant()
    async with Client(setup.server(), elicitation_callback=Human(ACCEPT)) as client:
        first = await _first_round(client)
        (pending,) = setup.pendings()
        await ResolveConfirmation(setup.db, setup.clock, cli_enabled=True).approve(pending.id)
        result = await client.session.call_tool(
            "set_trading_stop", STOP, input_responses=ANSWERS, request_state=first.request_state
        )

    assert isinstance(result, CallToolResult)

    assert _structured(result)["status"] == "approved"
    assert [d.payload["channel"] for d in setup.decisions()] == ["cli"]
    assert setup.actions.stops == []  # the executor, not the form, executes a CLI approval
