"""Trading tools (FR-4). Handlers only parse, call the use case and shape the result; limits,
rules and the journal are the use case's business.

Money arguments are JSON **strings** with a pattern, not ``Decimal`` (whose default schema is
``anyOf: [number, string]``): a model that sends a float gets ``invalid_params`` instead of an
amount that went through binary rounding (D-4, spike 002). The caller's self-description
(``clientInfo``) is passed on for the journal only (FR-36); it never decides anything.
"""

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from guarded_exchange.application.submit_action import PlaceOrderInput, SubmitPlaceOrder
from guarded_exchange.domain.audit import ClientDescription
from guarded_exchange.domain.money import AMOUNT_PATTERN
from guarded_exchange.interface.mcp.access import PrincipalProvider
from guarded_exchange.interface.mcp.results import run_tool
from guarded_exchange.interface.mcp.schemas import PlaceOrderOut
from guarded_exchange.interface.mcp.tools.market import CategoryArg, SymbolArg

PLACE_ORDER_DESCRIPTION = (
    "Place a market or limit order on the test environment (spot, or linear USDT perpetuals). qty is in "
    "the base asset (BTC for BTCUSDT) and is rounded down to the instrument's step; a limit price must "
    "be a multiple of the tick size (see get_instrument_rules). A market order executes immediately "
    "no worse than the best price +/- the policy's slippage; what cannot execute within that bound is "
    "cancelled. reduce_only (perpetuals only) can only shrink an open position and is cut to its size. "
    "Your policy's limits apply (allowed instruments, max order value, leverage; see get_my_policy); "
    "if it requires confirming orders, the call is refused with confirmation_channel_unavailable: do "
    "not retry it. Each call places a new order: never repeat a call to 'make sure'."
)

AmountStr = Annotated[str, Field(pattern=AMOUNT_PATTERN)]
QtyArg = Annotated[
    AmountStr,
    Field(description='Quantity in the base asset as a decimal string, e.g. "0.01" for 0.01 BTC.'),
]
PriceArg = Annotated[
    AmountStr | None,
    Field(
        description='Limit orders only: price in USDT as a decimal string, e.g. "65000.5", a multiple of '
        "the tick size. Omit for market orders."
    ),
]
SideArg = Annotated[Literal["buy", "sell"], Field(description="buy or sell.")]
OrderTypeArg = Annotated[
    Literal["market", "limit"],
    Field(description="market: execute now within the slippage bound; limit: rest at price."),
]
ReduceOnlyArg = Annotated[
    bool,
    Field(description="Perpetuals only: true = may only reduce an open position (e.g. sell against a long)."),
]

_TRADING = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


def client_description(ctx: Context) -> ClientDescription | None:
    """What the client said about itself in this request, if anything (2026-07-28: per request ``_meta``)."""
    params = ctx.session.client_params
    if params is None:
        return None
    return ClientDescription.sanitized(params.client_info.name, params.client_info.version)


def register(mcp: MCPServer[Any], *, place_order: SubmitPlaceOrder, principal: PrincipalProvider) -> None:
    @mcp.tool(
        name="place_order", title="Place order", description=PLACE_ORDER_DESCRIPTION, annotations=_TRADING
    )
    async def place_order_tool(  # noqa: PLR0917 - the SDK builds inputSchema from this signature
        symbol: SymbolArg,
        category: CategoryArg,
        side: SideArg,
        order_type: OrderTypeArg,
        qty: QtyArg,
        ctx: Context,
        price: PriceArg = None,
        reduce_only: ReduceOnlyArg = False,
    ) -> Annotated[CallToolResult, PlaceOrderOut]:
        async def call() -> PlaceOrderOut:
            request = PlaceOrderInput(
                symbol=symbol,
                category=category,
                side=side,
                order_type=order_type,
                qty=qty,
                price=price,
                reduce_only=reduce_only,
            )
            return PlaceOrderOut.from_domain(await place_order(principal(), request, client_description(ctx)))

        return await run_tool("place_order", ctx.request_id, call())
