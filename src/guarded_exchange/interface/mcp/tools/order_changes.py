"""``cancel_order``, ``amend_order``, ``cancel_all_orders`` (FR-5, ticket 013). Thin: parse, call the use
case, shape the result; which orders may be touched, risk, limits and the journal are the use case's."""

from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from guarded_exchange.application.order_changes import (
    AmendOrderInput,
    CancelAllOrdersInput,
    CancelOrderInput,
    SubmitAmendOrder,
    SubmitCancelAllOrders,
    SubmitCancelOrder,
)
from guarded_exchange.domain.actions import ORDER_ID_PATTERN
from guarded_exchange.domain.idempotency import IDEMPOTENCY_KEY_PATTERN
from guarded_exchange.domain.order_changes import CANCEL_ALL_MAX
from guarded_exchange.interface.mcp.access import PrincipalProvider
from guarded_exchange.interface.mcp.results import run_tool
from guarded_exchange.interface.mcp.schemas_order_changes import AmendOrderOut, CancelAllOut, CancelOrderOut
from guarded_exchange.interface.mcp.tools.market import CategoryArg, SymbolArg
from guarded_exchange.interface.mcp.tools.trading import AmountStr, client_description

_KEY_NOTE = (
    "idempotency_key (optional, e.g. a UUID): repeating the call with the same key returns the first "
    "answer and sends nothing; a new attempt needs a new key."
)

CANCEL_ORDER_DESCRIPTION = (
    "Cancel one open order by its exchange order_id (from get_open_orders), on the test environment. Works "
    "on any plain order of the account, including external ones (placed by the user or another bot): the "
    "answer marks them; tell the user when you cancel an external order. Conditional orders and a position's "
    "stop-loss / take-profit are not cancelled here. Cancelling reduces risk, so it works under the kill "
    "switch. The exchange applies cancels asynchronously: status cancel_requested means accepted, not yet "
    "visible. On outcome_unknown, check get_open_orders; calling cancel_order again is safe. " + _KEY_NOTE
)
AMEND_ORDER_DESCRIPTION = (
    "Change the quantity and/or limit price of one open limit order (by exchange order_id), on the test "
    "environment; works on external orders too (the answer marks them). qty is the order's new TOTAL "
    "quantity in the base asset (rounded down to the step, above what is already filled); price must be on "
    "the tick. A larger qty or a worse price (higher for a buy, lower for a sell) increases risk: your "
    "policy's limits apply and the kill switch blocks it; otherwise it reduces risk. Applied asynchronously: "
    "status amend_requested means accepted, not yet visible. " + _KEY_NOTE
)
CANCEL_ALL_ORDERS_DESCRIPTION = (
    "Cancel the plain open orders on one instrument (ours and external), on the test environment: the "
    f"server lists them and cancels each by id, at most {CANCEL_ALL_MAX} per call (has_more: call again). "
    "Conditional orders and a position's stop-loss / take-profit are never touched. Works under the kill "
    "switch. Each order gets its own status; tell the user how many were external. " + _KEY_NOTE
)

OrderIdArg = Annotated[
    str,
    Field(
        pattern=ORDER_ID_PATTERN, description="Exchange order id from get_open_orders (not the client id)."
    ),
]
NewQtyArg = Annotated[
    AmountStr | None,
    Field(
        description='New total quantity in the base asset as a decimal string, e.g. "0.002"; omit to keep.'
    ),
]
NewPriceArg = Annotated[
    AmountStr | None,
    Field(description='New limit price in USDT as a decimal string, e.g. "65000.5"; omit to keep.'),
]
KeyArg = Annotated[
    str | None,
    Field(
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description="Optional: 1-64 letters, digits or _ . : - (e.g. a UUID), new for each new action.",
    ),
]

_CHANGES = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


@dataclass(frozen=True, slots=True)
class OrderChangeUseCases:
    cancel_order: SubmitCancelOrder
    amend_order: SubmitAmendOrder
    cancel_all_orders: SubmitCancelAllOrders


def register(mcp: MCPServer[Any], *, use_cases: OrderChangeUseCases, principal: PrincipalProvider) -> None:
    @mcp.tool(
        name="cancel_order", title="Cancel order", description=CANCEL_ORDER_DESCRIPTION, annotations=_CHANGES
    )
    async def cancel_order_tool(
        symbol: SymbolArg,
        category: CategoryArg,
        order_id: OrderIdArg,
        ctx: Context,
        idempotency_key: KeyArg = None,
    ) -> Annotated[CallToolResult, CancelOrderOut]:
        async def call() -> CancelOrderOut:
            request = CancelOrderInput(symbol, category, order_id, idempotency_key)
            result = await use_cases.cancel_order(principal(), request, client_description(ctx))
            return CancelOrderOut.from_domain(result)

        return await run_tool("cancel_order", ctx.request_id, call())

    @mcp.tool(
        name="amend_order", title="Amend order", description=AMEND_ORDER_DESCRIPTION, annotations=_CHANGES
    )
    async def amend_order_tool(  # noqa: PLR0917 - the SDK builds inputSchema from this signature
        symbol: SymbolArg,
        category: CategoryArg,
        order_id: OrderIdArg,
        ctx: Context,
        qty: NewQtyArg = None,
        price: NewPriceArg = None,
        idempotency_key: KeyArg = None,
    ) -> Annotated[CallToolResult, AmendOrderOut]:
        async def call() -> AmendOrderOut:
            request = AmendOrderInput(symbol, category, order_id, qty, price, idempotency_key)
            result = await use_cases.amend_order(principal(), request, client_description(ctx))
            return AmendOrderOut.from_domain(result)

        return await run_tool("amend_order", ctx.request_id, call())

    @mcp.tool(
        name="cancel_all_orders",
        title="Cancel all orders on an instrument",
        description=CANCEL_ALL_ORDERS_DESCRIPTION,
        annotations=_CHANGES,
    )
    async def cancel_all_orders_tool(
        symbol: SymbolArg,
        category: CategoryArg,
        ctx: Context,
        idempotency_key: KeyArg = None,
    ) -> Annotated[CallToolResult, CancelAllOut]:
        async def call() -> CancelAllOut:
            request = CancelAllOrdersInput(symbol, category, idempotency_key)
            result = await use_cases.cancel_all_orders(principal(), request, client_description(ctx))
            return CancelAllOut.from_domain(result)

        return await run_tool("cancel_all_orders", ctx.request_id, call())
