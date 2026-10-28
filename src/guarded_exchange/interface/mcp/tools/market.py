"""Market data tools (FR-1, FR-3). Handlers only call a use case and shape the result.

Arguments are declared with real types: the same annotation produces the ``inputSchema`` the
model sees and the SDK check whose failures ``GuardedMCPServer`` turns into ``invalid_params``
(ADR-013, spike 002). Use cases still normalise them through domain value objects.
"""

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from guarded_exchange.application.read_market import GetCandles, GetInstrumentRules, GetOrderbook, GetTicker
from guarded_exchange.domain.market import (
    DEFAULT_CANDLES,
    DEFAULT_ORDERBOOK_DEPTH,
    MAX_CANDLES,
    MAX_ORDERBOOK_DEPTH,
)
from guarded_exchange.interface.mcp.results import run_tool
from guarded_exchange.interface.mcp.schemas import CandlesOut, InstrumentRulesOut, OrderbookOut, TickerOut

GET_TICKER_DESCRIPTION = (
    "Get the current price and 24h summary (bid/ask, high/low, change %, volume) of one "
    "instrument. Read-only, public market data. For linear perpetuals also returns mark price "
    "and funding rate. Use this before proposing an order to know the current price."
)
GET_ORDERBOOK_DESCRIPTION = (
    "Get the order book of one instrument: the best bid and ask price levels with quantities. "
    "Read-only, public market data. Use it to judge liquidity and spread before a large order; "
    "for just the current price use get_ticker."
)
GET_CANDLES_DESCRIPTION = (
    "Get recent OHLCV candles of one instrument, oldest first, as compact rows. Read-only, public "
    "market data. Use it for price history and trends; the last candle may still be open."
)
GET_INSTRUMENT_RULES_DESCRIPTION = (
    "Get the trading rules of one USDT-quoted instrument: price tick size, quantity step, minimum "
    "quantity and order value, maximum leverage (linear) and whether it is trading. Read-only. "
    "Use it before placing an order so price and quantity fit the rules."
)

SymbolArg = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9]{2,30}$", description="Exchange symbol without separators, e.g. BTCUSDT."),
]
CategoryArg = Annotated[
    Literal["spot", "linear"], Field(description="spot, or linear for USDT perpetual futures.")
]
DepthArg = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_ORDERBOOK_DEPTH,
        description=f"Price levels per side, 1-{MAX_ORDERBOOK_DEPTH}.",
    ),
]
# Keep in sync with domain.market.CandleInterval (a unit test checks it).
IntervalLiteral = Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w", "1mo"]
IntervalArg = Annotated[
    IntervalLiteral, Field(description="Candle length: minutes (m), hours (h), day, week or month (1mo).")
]
CandlesLimitArg = Annotated[
    int, Field(ge=1, le=MAX_CANDLES, description=f"Number of most recent candles, 1-{MAX_CANDLES}.")
]

_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


def register(
    mcp: MCPServer[Any],
    *,
    get_ticker: GetTicker,
    get_orderbook: GetOrderbook,
    get_candles: GetCandles,
    get_instrument_rules: GetInstrumentRules,
) -> None:
    @mcp.tool(
        name="get_ticker", title="Get ticker", description=GET_TICKER_DESCRIPTION, annotations=_READ_ONLY
    )
    async def get_ticker_tool(
        symbol: SymbolArg, category: CategoryArg, ctx: Context
    ) -> Annotated[CallToolResult, TickerOut]:
        async def call() -> TickerOut:
            return TickerOut.from_domain(await get_ticker(symbol, category))

        return await run_tool("get_ticker", ctx.request_id, call())

    @mcp.tool(
        name="get_orderbook",
        title="Get order book",
        description=GET_ORDERBOOK_DESCRIPTION,
        annotations=_READ_ONLY,
    )
    async def get_orderbook_tool(
        symbol: SymbolArg, category: CategoryArg, ctx: Context, depth: DepthArg = DEFAULT_ORDERBOOK_DEPTH
    ) -> Annotated[CallToolResult, OrderbookOut]:
        async def call() -> OrderbookOut:
            return OrderbookOut.from_domain(await get_orderbook(symbol, category, depth))

        return await run_tool("get_orderbook", ctx.request_id, call())

    @mcp.tool(
        name="get_candles", title="Get candles", description=GET_CANDLES_DESCRIPTION, annotations=_READ_ONLY
    )
    async def get_candles_tool(
        symbol: SymbolArg,
        category: CategoryArg,
        interval: IntervalArg,
        ctx: Context,
        limit: CandlesLimitArg = DEFAULT_CANDLES,
    ) -> Annotated[CallToolResult, CandlesOut]:
        async def call() -> CandlesOut:
            return CandlesOut.from_domain(await get_candles(symbol, category, interval, limit))

        return await run_tool("get_candles", ctx.request_id, call())

    @mcp.tool(
        name="get_instrument_rules",
        title="Get instrument rules",
        description=GET_INSTRUMENT_RULES_DESCRIPTION,
        annotations=_READ_ONLY,
    )
    async def get_instrument_rules_tool(
        symbol: SymbolArg, category: CategoryArg, ctx: Context
    ) -> Annotated[CallToolResult, InstrumentRulesOut]:
        async def call() -> InstrumentRulesOut:
            return InstrumentRulesOut.from_domain(await get_instrument_rules(symbol, category))

        return await run_tool("get_instrument_rules", ctx.request_id, call())
