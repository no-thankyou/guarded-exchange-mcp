"""Market data value objects. Money and quantities are ``Decimal``, time is UTC-aware."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from guarded_exchange.domain.instruments import Category


@dataclass(frozen=True, slots=True)
class Ticker:
    """A 24h market summary for one instrument.

    Prices are in the quote currency of the symbol (USDT for ``BTCUSDT``). Percentages are
    already multiplied by 100 (``Decimal("0.7")`` means 0.7 %). ``None`` means the exchange
    did not report the value (e.g. no bid on an illiquid book, funding on spot).
    """

    symbol: str
    category: Category
    last_price: Decimal
    bid_price: Decimal | None
    ask_price: Decimal | None
    high_24h: Decimal | None
    low_24h: Decimal | None
    change_24h_pct: Decimal | None
    volume_24h_base: Decimal | None
    turnover_24h_quote: Decimal | None
    observed_at: datetime
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    funding_rate_pct: Decimal | None = None
    next_funding_at: datetime | None = None
