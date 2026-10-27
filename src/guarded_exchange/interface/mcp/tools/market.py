"""Market data tools. Handlers only call a use case and shape the result."""

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field, WithJsonSchema

from guarded_exchange.application.read_market import GetTicker
from guarded_exchange.interface.mcp.results import run_tool
from guarded_exchange.interface.mcp.schemas import TickerOut

GET_TICKER_DESCRIPTION = (
    "Get the current price and 24h summary (bid/ask, high/low, change %, volume) of one "
    "instrument. Read-only, public market data. For linear perpetuals also returns mark price "
    "and funding rate. Use this before proposing an order to know the current price."
)

# The schemas below are what the model sees. Runtime validation is done by the use case, so a
# bad value comes back as a structured `invalid_params` error (ADR-013) instead of the SDK's
# free-text pydantic message.
SymbolArg = Annotated[
    str,
    WithJsonSchema(
        {
            "type": "string",
            "pattern": "^[A-Za-z0-9]{2,30}$",
            "description": "Exchange symbol without separators, e.g. BTCUSDT.",
        }
    ),
    Field(description="Exchange symbol without separators, e.g. BTCUSDT."),
]
CategoryArg = Annotated[
    str,
    WithJsonSchema(
        {
            "type": "string",
            "enum": ["spot", "linear"],
            "description": "spot, or linear for USDT perpetual futures.",
        }
    ),
    Field(description="spot, or linear for USDT perpetual futures."),
]


def register(mcp: MCPServer[Any], get_ticker: GetTicker) -> None:
    @mcp.tool(
        name="get_ticker",
        title="Get ticker",
        description=GET_TICKER_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True),
    )
    async def get_ticker_tool(
        symbol: SymbolArg, category: CategoryArg, ctx: Context
    ) -> Annotated[CallToolResult, TickerOut]:
        async def call() -> TickerOut:
            return TickerOut.from_domain(await get_ticker(symbol, category))

        return await run_tool("get_ticker", ctx.request_id, call())
