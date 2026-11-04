"""``set_trading_stop``, ``close_position``, ``set_leverage`` (FR-6 - FR-8, ticket 014). Thin: parse, call
the use case, shape the result; risk, limits, the position read and the journal are the use case's."""

from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from guarded_exchange.application.position_actions import (
    ClosePositionInput,
    SetLeverageInput,
    SetTradingStopInput,
    SubmitClosePosition,
    SubmitSetLeverage,
    SubmitSetTradingStop,
)
from guarded_exchange.interface.mcp.access import PrincipalProvider
from guarded_exchange.interface.mcp.results import run_tool
from guarded_exchange.interface.mcp.schemas import PlaceOrderOut
from guarded_exchange.interface.mcp.schemas_position_actions import SetLeverageOut, SetTradingStopOut
from guarded_exchange.interface.mcp.tools.market import CategoryArg, SymbolArg
from guarded_exchange.interface.mcp.tools.order_changes import KeyArg
from guarded_exchange.interface.mcp.tools.trading import AmountStr, client_description

_PERPS = "Perpetuals only (category linear): on spot the call is refused with invalid_params. "
_KEY_NOTE = (
    "idempotency_key (optional, e.g. a UUID): repeating the call with the same key returns the first "
    "answer and sends nothing; a new attempt needs a new key."
)

SET_TRADING_STOP_DESCRIPTION = (
    "Set or move the stop-loss and/or take-profit of the open position on a perpetual, on the test "
    "environment. "
    + _PERPS
    + "Prices are USDT trigger prices, multiples of the tick size; pass at least one. "
    "It protects the whole position, reduces risk and works under the kill switch and for positions larger "
    "than the order limit. The exchange applies it asynchronously: status requested means accepted, not yet "
    "visible in get_positions. On outcome_unknown check get_positions; repeating the call is safe. "
    + _KEY_NOTE
)
CLOSE_POSITION_DESCRIPTION = (
    "Close the open position on a perpetual, fully or partly (qty), on the test environment, with a "
    "reduce-only market order (executes immediately within the policy's slippage of the best price; what "
    "cannot execute is cancelled, so check filled_qty). " + _PERPS + "qty is in the base asset and is cut to "
    "the position's size; omit it to close everything. It reduces risk: works under the kill switch and "
    "for positions larger than the order limit. Answers like place_order. " + _KEY_NOTE
)
SET_LEVERAGE_DESCRIPTION = (
    "Set the leverage of a perpetual, on the test environment. " + _PERPS + "A leverage higher than the "
    "current one increases risk: the kill switch blocks it and your policy's maximum leverage (see "
    "get_my_policy) applies; the same or a lower one always works. Setting the leverage it already has is "
    "a success. On outcome_unknown check get_positions; repeating the call is safe. " + _KEY_NOTE
)

TakeProfitArg = Annotated[
    AmountStr | None,
    Field(description='Take-profit trigger price in USDT as a decimal string, e.g. "70000.5"; omit to keep.'),
]
StopLossArg = Annotated[
    AmountStr | None,
    Field(description='Stop-loss trigger price in USDT as a decimal string, e.g. "62000"; omit to keep.'),
]
CloseQtyArg = Annotated[
    AmountStr | None,
    Field(description='Quantity to close in the base asset, e.g. "0.005"; omit to close the whole position.'),
]
LeverageArg = Annotated[AmountStr, Field(description='Leverage as a decimal string, e.g. "5".')]

_CHANGES = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


@dataclass(frozen=True, slots=True)
class PositionActionUseCases:
    set_trading_stop: SubmitSetTradingStop
    close_position: SubmitClosePosition
    set_leverage: SubmitSetLeverage


def register(mcp: MCPServer[Any], *, use_cases: PositionActionUseCases, principal: PrincipalProvider) -> None:
    @mcp.tool(
        name="set_trading_stop",
        title="Set stop-loss / take-profit",
        description=SET_TRADING_STOP_DESCRIPTION,
        annotations=_CHANGES,
    )
    async def set_trading_stop_tool(  # noqa: PLR0917 - the SDK builds inputSchema from this signature
        symbol: SymbolArg,
        category: CategoryArg,
        ctx: Context,
        take_profit: TakeProfitArg = None,
        stop_loss: StopLossArg = None,
        idempotency_key: KeyArg = None,
    ) -> Annotated[CallToolResult, SetTradingStopOut]:
        async def call() -> SetTradingStopOut:
            request = SetTradingStopInput(symbol, category, take_profit, stop_loss, idempotency_key)
            result = await use_cases.set_trading_stop(principal(), request, client_description(ctx))
            return SetTradingStopOut.from_domain(result)

        return await run_tool("set_trading_stop", ctx.request_id, call())

    @mcp.tool(
        name="close_position",
        title="Close position",
        description=CLOSE_POSITION_DESCRIPTION,
        annotations=_CHANGES,
    )
    async def close_position_tool(
        symbol: SymbolArg,
        category: CategoryArg,
        ctx: Context,
        qty: CloseQtyArg = None,
        idempotency_key: KeyArg = None,
    ) -> Annotated[CallToolResult, PlaceOrderOut]:
        async def call() -> PlaceOrderOut:
            request = ClosePositionInput(symbol, category, qty, idempotency_key)
            result = await use_cases.close_position(principal(), request, client_description(ctx))
            return PlaceOrderOut.from_domain(result)

        return await run_tool("close_position", ctx.request_id, call())

    @mcp.tool(
        name="set_leverage", title="Set leverage", description=SET_LEVERAGE_DESCRIPTION, annotations=_CHANGES
    )
    async def set_leverage_tool(
        symbol: SymbolArg,
        category: CategoryArg,
        leverage: LeverageArg,
        ctx: Context,
        idempotency_key: KeyArg = None,
    ) -> Annotated[CallToolResult, SetLeverageOut]:
        async def call() -> SetLeverageOut:
            request = SetLeverageInput(symbol, category, leverage, idempotency_key)
            result = await use_cases.set_leverage(principal(), request, client_description(ctx))
            return SetLeverageOut.from_domain(result)

        return await run_tool("set_leverage", ctx.request_id, call())
