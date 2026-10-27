"""Read-only market data use cases (FR-1)."""

from guarded_exchange.application.ports import ExchangeClient
from guarded_exchange.domain.instruments import normalize_symbol, parse_category
from guarded_exchange.domain.market import Ticker


class GetTicker:
    """Return the current price and 24h summary for one instrument.

    Public market data: no credentials and no policy checks are needed (FR-1). Raw input is
    validated here, so every entrypoint gets the same ``InvalidParams`` errors; exchange
    failures surface as domain exceptions raised by the ``ExchangeClient`` port.
    """

    def __init__(self, exchange: ExchangeClient) -> None:
        self._exchange = exchange

    async def __call__(self, symbol: str, category: str) -> Ticker:
        return await self._exchange.get_ticker(normalize_symbol(symbol), parse_category(category))
