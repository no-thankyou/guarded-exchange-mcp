"""Read-only market data use cases (FR-1, FR-3).

Public market data: no credentials and no policy checks are needed (FR-1). Raw input is
validated here with domain value objects, so every entrypoint (MCP, CLI, later the executor)
gets the same ``InvalidParams`` errors; exchange failures surface as domain exceptions raised
by the ``ExchangeClient`` port.
"""

from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.ports import ExchangeClient
from guarded_exchange.domain.instruments import (
    InstrumentRules,
    normalize_symbol,
    parse_category,
    require_supported_quote,
)
from guarded_exchange.domain.market import (
    Candles,
    Orderbook,
    Ticker,
    candles_limit,
    orderbook_depth,
    parse_interval,
)


class GetTicker:
    """Return the current price and 24h summary for one instrument."""

    def __init__(self, exchange: ExchangeClient) -> None:
        self._exchange = exchange

    async def __call__(self, symbol: str, category: str) -> Ticker:
        return await self._exchange.get_ticker(normalize_symbol(symbol), parse_category(category))


class GetOrderbook:
    """Return the best ``depth`` bid and ask levels; never more than ``depth`` per side (FR-3)."""

    def __init__(self, exchange: ExchangeClient) -> None:
        self._exchange = exchange

    async def __call__(self, symbol: str, category: str, depth: int) -> Orderbook:
        sym, cat, levels = normalize_symbol(symbol), parse_category(category), orderbook_depth(depth)
        book = await self._exchange.get_orderbook(sym, cat, levels)
        return book.top(levels)


class GetCandles:
    """Return the most recent ``limit`` candles, oldest first; never more than ``limit`` (FR-3)."""

    def __init__(self, exchange: ExchangeClient) -> None:
        self._exchange = exchange

    async def __call__(self, symbol: str, category: str, interval: str, limit: int) -> Candles:
        sym, cat = normalize_symbol(symbol), parse_category(category)
        length, count = parse_interval(interval), candles_limit(limit)
        candles = await self._exchange.get_candles(sym, cat, length, count)
        return candles.latest(count)


class GetInstrumentRules:
    """Return the trading rules of a USDT-quoted instrument (FR-1, FR-4), served from the cache."""

    def __init__(self, rules: InstrumentRulesCache) -> None:
        self._rules = rules

    async def __call__(self, symbol: str, category: str) -> InstrumentRules:
        rules = await self._rules.get(normalize_symbol(symbol), parse_category(category))
        require_supported_quote(rules.instrument)
        return rules
