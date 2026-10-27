"""In-memory fakes of application ports (no mocks of everything, ENGINEERING.md)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from guarded_exchange.domain.errors import NotFound
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.market import Ticker


def make_ticker(symbol: str = "BTCUSDT", category: Category = Category.LINEAR) -> Ticker:
    linear = category is Category.LINEAR
    return Ticker(
        symbol=symbol,
        category=category,
        last_price=Decimal("80571.10"),
        bid_price=Decimal("80571.10"),
        ask_price=Decimal("80571.20"),
        high_24h=Decimal("80800.00"),
        low_24h=Decimal("79458.90"),
        change_24h_pct=Decimal("0.6848"),
        volume_24h_base=Decimal("43556.0370"),
        turnover_24h_quote=Decimal("3486773303.0305"),
        observed_at=datetime(2026, 10, 27, 18, 53, 43, 449000, tzinfo=UTC),
        mark_price=Decimal("80571.10") if linear else None,
        index_price=Decimal("80603.97") if linear else None,
        funding_rate_pct=Decimal("0.01") if linear else None,
        next_funding_at=datetime(2026, 10, 28, 0, 0, tzinfo=UTC) if linear else None,
    )


@dataclass
class FakeExchange:
    """``ExchangeClient`` with a configurable set of tickers and an optional forced failure."""

    tickers: dict[tuple[str, Category], Ticker] = field(default_factory=dict)
    fail_with: Exception | None = None
    calls: list[tuple[str, Category]] = field(default_factory=list)

    async def get_ticker(self, symbol: str, category: Category) -> Ticker:
        self.calls.append((symbol, category))
        if self.fail_with is not None:
            raise self.fail_with
        try:
            return self.tickers[(symbol, category)]
        except KeyError:
            raise NotFound(f"no {category.value} instrument {symbol}") from None
