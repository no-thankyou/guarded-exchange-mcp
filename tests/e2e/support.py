"""Building the real MCP server on in-memory fakes for in-process end-to-end tests."""

from collections.abc import Mapping
from typing import Any

from mcp.server import MCPServer

from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.policy import AuthorizeAccess, ReadPolicy
from guarded_exchange.application.read_market import GetCandles, GetInstrumentRules, GetOrderbook, GetTicker
from guarded_exchange.domain.policy import Permission
from guarded_exchange.interface.mcp.access import TOOL_PERMISSIONS, fixed_principal
from guarded_exchange.interface.mcp.server import UseCases, create_server
from tests.fakes import FakeClock, FakeExchange, InMemoryDatabase


def use_cases(
    db: InMemoryDatabase, exchange: FakeExchange | None = None, clock: FakeClock | None = None
) -> UseCases:
    """The MCP use cases wired to fakes, as the composition root wires the real adapters."""
    exchange = exchange or FakeExchange()
    return UseCases(
        get_ticker=GetTicker(exchange),
        get_orderbook=GetOrderbook(exchange),
        get_candles=GetCandles(exchange),
        get_instrument_rules=GetInstrumentRules(InstrumentRulesCache(exchange, clock or FakeClock())),
        read_policy=ReadPolicy(db),
        authorize=AuthorizeAccess(db),
    )


def server_for(
    db: InMemoryDatabase,
    user_id: str | None,
    *,
    exchange: FakeExchange | None = None,
    tool_permissions: Mapping[str, Permission] = TOOL_PERMISSIONS,
) -> MCPServer[Any]:
    """The production server for principal ``user_id`` (``None``: nobody resolved)."""
    principal = fixed_principal(user_id) if user_id is not None else (lambda: None)
    return create_server(use_cases(db, exchange), principal=principal, tool_permissions=tool_permissions)
