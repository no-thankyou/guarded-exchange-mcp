import pytest

from guarded_exchange.application.ports import ExchangeClient
from guarded_exchange.application.read_market import GetTicker
from guarded_exchange.domain.errors import ExchangeUnavailable, InvalidParams, NotFound
from guarded_exchange.domain.instruments import Category
from tests.fakes import FakeExchange, make_ticker

pytestmark = pytest.mark.req("FR-1")


def _use_case(exchange: FakeExchange) -> GetTicker:
    port: ExchangeClient = exchange  # FakeExchange must satisfy the port structurally
    return GetTicker(port)


async def test_returns_domain_ticker_from_port() -> None:
    ticker = make_ticker("BTCUSDT", Category.LINEAR)
    exchange = FakeExchange(tickers={("BTCUSDT", Category.LINEAR): ticker})

    result = await _use_case(exchange)("BTCUSDT", "linear")

    assert result is ticker


async def test_normalizes_symbol_and_category_before_calling_port() -> None:
    ticker = make_ticker("ETHUSDT", Category.SPOT)
    exchange = FakeExchange(tickers={("ETHUSDT", Category.SPOT): ticker})

    await _use_case(exchange)("  ethusdt ", "SPOT")

    assert exchange.calls == [("ETHUSDT", Category.SPOT)]


@pytest.mark.req("FR-28")
@pytest.mark.parametrize("symbol", ["", "B", "BTC-USDT", "BTC/USDT", "X" * 31, "btc usdt"])
async def test_invalid_symbol_is_rejected_without_calling_port(symbol: str) -> None:
    exchange = FakeExchange()

    with pytest.raises(InvalidParams) as err:
        await _use_case(exchange)(symbol, "linear")

    assert err.value.code == "invalid_params"
    assert err.value.details == {"field": "symbol"}
    assert exchange.calls == []


@pytest.mark.req("FR-28")
@pytest.mark.parametrize("category", ["inverse", "option", "", "futures"])
async def test_invalid_category_is_rejected_without_calling_port(category: str) -> None:
    exchange = FakeExchange()

    with pytest.raises(InvalidParams) as err:
        await _use_case(exchange)("BTCUSDT", category)

    assert err.value.details == {"field": "category"}
    assert exchange.calls == []


@pytest.mark.req("FR-28")
async def test_port_failure_surfaces_as_domain_exception() -> None:
    exchange = FakeExchange(fail_with=ExchangeUnavailable("the exchange is unavailable: timeout"))

    with pytest.raises(ExchangeUnavailable) as err:
        await _use_case(exchange)("BTCUSDT", "linear")

    assert err.value.code == "exchange_unavailable"


@pytest.mark.req("FR-28")
async def test_unknown_instrument_is_not_found() -> None:
    with pytest.raises(NotFound):
        await _use_case(FakeExchange())("DOGEUSDT", "spot")
