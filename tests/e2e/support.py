"""Building the real MCP server on in-memory fakes for in-process end-to-end tests."""

from collections.abc import Mapping
from typing import Any

from mcp.server import MCPServer

from guarded_exchange.application.credentials import (
    ReadCredential,
    RecheckCredential,
    RequireTradingCredential,
)
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.kill_switch import ActivateKillSwitch
from guarded_exchange.application.order_changes import (
    SubmitAmendOrder,
    SubmitCancelAllOrders,
    SubmitCancelOrder,
)
from guarded_exchange.application.policy import AuthorizeAccess, ReadPolicy
from guarded_exchange.application.ports import UnitOfWorkFactory
from guarded_exchange.application.read_account import GetBalances, GetOpenOrders, GetPositions
from guarded_exchange.application.read_history import GetOrderHistory, GetTrades
from guarded_exchange.application.read_market import GetCandles, GetInstrumentRules, GetOrderbook, GetTicker
from guarded_exchange.application.resolve_confirmation import CancelConfirmation, GetConfirmationStatus
from guarded_exchange.application.submit_action import SubmitPlaceOrder
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT
from guarded_exchange.domain.policy import Permission
from guarded_exchange.domain.users import Environment
from guarded_exchange.interface.mcp.access import TOOL_PERMISSIONS, fixed_principal
from guarded_exchange.interface.mcp.server import UseCases, create_server
from guarded_exchange.interface.mcp.tools.confirmations import ConfirmationUseCases
from guarded_exchange.interface.mcp.tools.order_changes import OrderChangeUseCases
from tests.fakes import (
    FakeAccount,
    FakeCipher,
    FakeClock,
    FakeExchange,
    FakeKeyReader,
    FakeTrading,
    InMemoryDatabase,
    SequentialIds,
)
from tests.fakes_history import FakeHistory
from tests.fakes_trading import FakeOrderChanges


def use_cases(
    db: UnitOfWorkFactory,
    exchange: FakeExchange | None = None,
    clock: FakeClock | None = None,
    *,
    account: FakeAccount | None = None,
    cipher: FakeCipher | None = None,
    trading: FakeTrading | None = None,
    key_reader: FakeKeyReader | None = None,
    changes: FakeOrderChanges | None = None,
) -> UseCases:
    """The MCP use cases wired to fakes, as the composition root wires the real adapters."""
    exchange, clock, account = exchange or FakeExchange(), clock or FakeClock(), account or FakeAccount()
    cipher = cipher or FakeCipher()
    scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
    read_key = ReadCredential(db, cipher, clock, **scope)
    recheck = RecheckCredential(db, cipher, key_reader or FakeKeyReader(), clock)
    rules = InstrumentRulesCache(exchange, clock)
    return UseCases(
        get_ticker=GetTicker(exchange),
        get_orderbook=GetOrderbook(exchange),
        get_candles=GetCandles(exchange),
        get_instrument_rules=GetInstrumentRules(rules),
        get_balances=GetBalances(read_key, account),
        get_positions=GetPositions(read_key, account),
        get_order_history=GetOrderHistory(read_key, FakeHistory(), clock),
        get_trades=GetTrades(read_key, FakeHistory(), clock),
        get_open_orders=GetOpenOrders(read_key, account),
        read_policy=ReadPolicy(db),
        authorize=AuthorizeAccess(db),
        place_order=SubmitPlaceOrder(
            db,
            RequireTradingCredential(db, cipher, recheck, clock, **scope),
            rules,
            market=exchange,
            trading=trading or FakeTrading(),
            clock=clock,
            ids=SequentialIds("act"),
            **scope,
        ),
        activate_kill_switch=ActivateKillSwitch(db, clock),
        order_changes=order_change_use_cases(db, cipher, recheck, rules, clock, trading, changes),
        confirmations=ConfirmationUseCases(
            get_status=GetConfirmationStatus(db, clock), cancel=CancelConfirmation(db, clock)
        ),
    )


def order_change_use_cases(  # noqa: PLR0917 - the fakes the composition root would wire
    db: UnitOfWorkFactory,
    cipher: FakeCipher,
    recheck: RecheckCredential,
    rules: InstrumentRulesCache,
    clock: FakeClock,
    trading: FakeTrading | None,
    changes: FakeOrderChanges | None,
) -> OrderChangeUseCases:
    scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
    credentials = RequireTradingCredential(db, cipher, recheck, clock, **scope)
    common: dict[str, Any] = {
        "changes": changes or FakeOrderChanges(),
        "clock": clock,
        "ids": SequentialIds("chg"),
        "reconnaissance_delay_s": 0,
        **scope,
    }
    return OrderChangeUseCases(
        cancel_order=SubmitCancelOrder(db, credentials, **common),
        amend_order=SubmitAmendOrder(db, credentials, rules, trading=trading or FakeTrading(), **common),
        cancel_all_orders=SubmitCancelAllOrders(db, credentials, **common),
    )


def server_for(
    db: InMemoryDatabase,
    user_id: str | None,
    *,
    exchange: FakeExchange | None = None,
    account: FakeAccount | None = None,
    cipher: FakeCipher | None = None,
    trading: FakeTrading | None = None,
    clock: FakeClock | None = None,
    tool_permissions: Mapping[str, Permission] = TOOL_PERMISSIONS,
    changes: FakeOrderChanges | None = None,
) -> MCPServer[Any]:
    """The production server for principal ``user_id`` (``None``: nobody resolved)."""
    principal = fixed_principal(user_id) if user_id is not None else (lambda: None)
    return create_server(
        use_cases(db, exchange, clock, account=account, cipher=cipher, trading=trading, changes=changes),
        principal=principal,
        tool_permissions=tool_permissions,
    )
