"""A parked action over MCP, and AC-4: the model cannot confirm anything (ticket 016; SPEC FR-20, FR-47,
AC-4; PLAN ADR-005, D-6)."""

from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult

from guarded_exchange.application.confirmation import ConfirmationSupport
from guarded_exchange.application.credentials import OpenCredentialKeys
from guarded_exchange.application.policy import SetCeiling
from guarded_exchange.application.resolve_confirmation import ResolveConfirmation
from guarded_exchange.domain.actions import ALL_ACTION_KINDS
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import InstrumentRef, PolicyPatch, Profile
from tests.e2e.support import server_for
from tests.e2e.test_mcp_place_order import KEYS, MARKET_BUY, TRADING_KEY
from tests.fakes import (
    FakeCipher,
    FakeClock,
    FakeExchange,
    FakeTrading,
    InMemoryDatabase,
    make_rules,
    make_ticker,
)

pytestmark = pytest.mark.req("FR-20", "FR-47", "AC-4")

APPROVAL_WORDS = ("approve", "confirm_action", "reject", "resolve", "decide", "accept", "execute")


class Setup:
    def __init__(self) -> None:
        self.db, self.clock, cipher = InMemoryDatabase(), FakeClock(), FakeCipher()
        self.user = self.db.add_user()
        self.db.add_credential(self.user, cipher, KEYS, permissions=TRADING_KEY)
        self.trading = FakeTrading(db=self.db)
        exchange = FakeExchange(
            tickers={("BTCUSDT", Category.LINEAR): make_ticker()},
            rules={("BTCUSDT", Category.LINEAR): make_rules()},
        )
        support = ConfirmationSupport(cli_enabled=True, open_keys=OpenCredentialKeys(self.db, cipher))
        self.server = server_for(
            self.db,
            self.user.id,
            exchange=exchange,
            cipher=cipher,
            trading=self.trading,
            clock=self.clock,
            confirmations=support,
        )

    async def grant(self) -> None:
        patch = PolicyPatch(
            profile=Profile.TRADE,
            allowlist=frozenset({InstrumentRef(Category.LINEAR, "BTCUSDT")}),
            confirm_actions=ALL_ACTION_KINDS,
        )
        await SetCeiling(self.db, self.clock)(self.user.name, patch)


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None, result.content
    out: dict[str, Any] = result.structured_content
    return out


async def test_a_call_needing_confirmation_returns_a_pending_answer_not_an_error() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server) as client:
        result = await client.call_tool("place_order", MARKET_BUY)
        out = _structured(result)
        asked = {"confirmation_id": out["confirmation_id"]}
        status = _structured(await client.call_tool("get_confirmation_status", asked))

    assert result.is_error is False
    assert (out["status"], out["tool"], out["risk"]) == ("pending", "place_order", "increases")
    assert out["shown_to_user"]["order"]["worst_price"]  # FR-21: what the human will be shown
    assert "cannot approve" in out["next_step"]
    assert status["status"] == "pending"
    assert setup.trading.sent == []


async def test_no_tool_can_approve_and_an_agent_that_tries_gets_nothing_executed() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        pending = _structured(await client.call_tool("place_order", MARKET_BUY))
        cid = pending["confirmation_id"]
        attempts = [
            await client.call_tool(name, {"confirmation_id": cid, "approved": True})
            for name in (
                "approve_confirmation",
                "confirm_action",
                "resolve_confirmation",
                "reject_confirmation",
            )
        ]
        again = await client.call_tool("place_order", {**MARKET_BUY, "confirmed": True})  # an invented flag
        cancelled = _structured(await client.call_tool("cancel_confirmation", {"confirmation_id": cid}))

    assert not [n for n in names if any(w in n for w in APPROVAL_WORDS)]
    assert all(a.is_error for a in attempts)
    assert again.is_error is False  # the call is merely parked again (the invented flag is ignored)...
    assert (
        _structured(again)["confirmation_id"] == cid
    )  # ...as the same pending one, not as a confirmed order
    assert cancelled["status"] == "cancelled"
    assert setup.trading.sent == []  # nothing happened without a human


async def test_the_human_approves_outside_mcp_and_the_agent_sees_the_status() -> None:
    setup = Setup()
    await setup.grant()
    async with Client(setup.server) as client:
        pending = _structured(await client.call_tool("place_order", MARKET_BUY))
        cid = pending["confirmation_id"]
        await ResolveConfirmation(setup.db, setup.clock, cli_enabled=True).approve(cid)
        status = _structured(await client.call_tool("get_confirmation_status", {"confirmation_id": cid}))

    assert status["status"] == "approved"  # the executor (not running here) executes it
