"""Ports the application layer depends on. Adapters live in ``infrastructure``.

The exchange port speaks domain language only (NFR-5, PLAN ADR-002/D-26): no Bybit field
names, codes or URLs. Implementations must raise ``guarded_exchange.domain.errors``
exceptions, never transport-specific ones.
"""

from typing import Protocol

from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.market import Ticker


class ExchangeClient(Protocol):
    """Read and (later) trading access to one exchange account or public market data."""

    async def get_ticker(self, symbol: str, category: Category) -> Ticker:
        """Return the current ticker.

        Raises:
            NotFound: the instrument does not exist in this category.
            InvalidParams: the exchange rejected the parameters.
            ExchangeUnavailable: the exchange could not be reached or failed transiently.
            ExchangeRejected: any other refusal by the exchange.
        """
        ...
