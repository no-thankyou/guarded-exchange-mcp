"""Use cases GetOrderbook, GetCandles, GetInstrumentRules and the rules cache, on FakeExchange."""

from datetime import timedelta

import pytest

from guarded_exchange.application.instrument_rules import DEFAULT_RULES_TTL, InstrumentRulesCache
from guarded_exchange.application.ports import ExchangeClient
from guarded_exchange.application.read_market import GetCandles, GetInstrumentRules, GetOrderbook
from guarded_exchange.domain.errors import ExchangeUnavailable, InvalidParams, NotFound
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.market import MAX_CANDLES, MAX_ORDERBOOK_DEPTH, CandleInterval
from tests.fakes import FakeClock, FakeExchange, make_candles, make_orderbook, make_rules

pytestmark = pytest.mark.req("FR-1")

BTC = ("BTCUSDT", Category.LINEAR)
ETH = ("ETHUSDT", Category.SPOT)


def _port(exchange: FakeExchange) -> ExchangeClient:
    return exchange  # FakeExchange must satisfy the port structurally (mypy checks this line)


# --- order book ------------------------------------------------------------------------------


async def test_orderbook_inputs_are_normalised_before_the_port() -> None:
    exchange = FakeExchange(orderbooks={ETH: make_orderbook(*ETH)})

    book = await GetOrderbook(_port(exchange))(" ethusdt ", "SPOT", 5)

    assert book.symbol == "ETHUSDT"
    assert exchange.calls == [("get_orderbook", "ETHUSDT", Category.SPOT, 5)]


@pytest.mark.req("FR-3")
async def test_orderbook_depth_is_respected_even_if_the_exchange_returns_more() -> None:
    exchange = FakeExchange(orderbooks={BTC: make_orderbook(*BTC, levels=20)})

    book = await GetOrderbook(_port(exchange))("BTCUSDT", "linear", 3)

    assert len(book.bids) == 3
    assert len(book.asks) == 3


@pytest.mark.req("FR-3", "FR-28")
@pytest.mark.parametrize("depth", [0, MAX_ORDERBOOK_DEPTH + 1])
async def test_orderbook_depth_out_of_range_is_rejected_without_calling_the_port(depth: int) -> None:
    exchange = FakeExchange(orderbooks={BTC: make_orderbook(*BTC)})

    with pytest.raises(InvalidParams) as err:
        await GetOrderbook(_port(exchange))("BTCUSDT", "linear", depth)

    assert err.value.details == {"field": "depth"}
    assert exchange.calls == []


@pytest.mark.req("FR-28")
@pytest.mark.parametrize(
    ("symbol", "category", "field"), [("BTC/USDT", "linear", "symbol"), ("BTCUSDT", "inverse", "category")]
)
async def test_orderbook_bad_symbol_or_category_is_rejected(symbol: str, category: str, field: str) -> None:
    exchange = FakeExchange()

    with pytest.raises(InvalidParams) as err:
        await GetOrderbook(_port(exchange))(symbol, category, 5)

    assert err.value.details == {"field": field}
    assert exchange.calls == []


# --- candles ---------------------------------------------------------------------------------


async def test_candles_inputs_are_normalised_before_the_port() -> None:
    exchange = FakeExchange(candles={BTC: make_candles(*BTC)})

    series = await GetCandles(_port(exchange))("btcusdt", "linear", "1H", 3)

    assert series.interval is CandleInterval.H1
    assert exchange.calls == [("get_candles", "BTCUSDT", Category.LINEAR, CandleInterval.H1, 3)]


@pytest.mark.req("FR-3")
async def test_candles_limit_is_respected_even_if_the_exchange_returns_more() -> None:
    full = make_candles(*BTC, count=10)
    exchange = FakeExchange(candles={BTC: full})

    series = await GetCandles(_port(exchange))("BTCUSDT", "linear", "1h", 4)

    assert series.candles == full.candles[-4:]  # the newest four, still oldest first


@pytest.mark.req("FR-3", "FR-28")
@pytest.mark.parametrize(
    ("interval", "limit", "field"),
    [("1h", 0, "limit"), ("1h", MAX_CANDLES + 1, "limit"), ("7m", 10, "interval")],
)
async def test_candles_bad_limit_or_interval_is_rejected_without_calling_the_port(
    interval: str, limit: int, field: str
) -> None:
    exchange = FakeExchange(candles={BTC: make_candles(*BTC)})

    with pytest.raises(InvalidParams) as err:
        await GetCandles(_port(exchange))("BTCUSDT", "linear", interval, limit)

    assert err.value.details == {"field": field}
    assert exchange.calls == []


# --- instrument rules ------------------------------------------------------------------------


def _rules_use_case(exchange: FakeExchange, clock: FakeClock | None = None) -> GetInstrumentRules:
    return GetInstrumentRules(InstrumentRulesCache(_port(exchange), clock or FakeClock()))


async def test_rules_of_a_usdt_instrument_are_returned() -> None:
    rules = make_rules(*ETH, base="ETH")
    exchange = FakeExchange(rules={ETH: rules})

    assert await _rules_use_case(exchange)(" ethusdt", "spot") is rules
    assert exchange.calls == [("get_instrument_rules", "ETHUSDT", Category.SPOT)]


@pytest.mark.req("FR-4", "FR-28")
async def test_rules_of_an_instrument_not_quoted_in_usdt_are_refused() -> None:
    exchange = FakeExchange(
        rules={("ETHBTC", Category.SPOT): make_rules("ETHBTC", Category.SPOT, base="ETH", quote="BTC")}
    )

    with pytest.raises(InvalidParams) as err:
        await _rules_use_case(exchange)("ETHBTC", "spot")

    assert err.value.details["reason"] == "quote_not_supported"


@pytest.mark.req("FR-28")
async def test_rules_bad_symbol_is_rejected_without_calling_the_port() -> None:
    exchange = FakeExchange()

    with pytest.raises(InvalidParams):
        await _rules_use_case(exchange)("BTC-USDT", "linear")

    assert exchange.calls == []


async def test_rules_are_cached_for_the_ttl_then_refreshed() -> None:
    clock = FakeClock()
    exchange = FakeExchange(rules={BTC: make_rules(*BTC)})
    use_case = _rules_use_case(exchange, clock)

    await use_case("BTCUSDT", "linear")
    clock.advance(DEFAULT_RULES_TTL - timedelta(milliseconds=1))
    await use_case("btcusdt", "linear")  # normalised to the same cache key
    assert len(exchange.calls) == 1

    clock.advance(timedelta(milliseconds=1))
    await use_case("BTCUSDT", "linear")
    assert len(exchange.calls) == 2


async def test_cache_keys_include_the_category() -> None:
    exchange = FakeExchange(
        rules={BTC: make_rules(*BTC), ("BTCUSDT", Category.SPOT): make_rules("BTCUSDT", Category.SPOT)}
    )
    use_case = _rules_use_case(exchange)

    linear = await use_case("BTCUSDT", "linear")
    spot = await use_case("BTCUSDT", "spot")

    assert linear.instrument.category is Category.LINEAR
    assert spot.instrument.category is Category.SPOT
    assert len(exchange.calls) == 2


@pytest.mark.req("FR-4")
async def test_non_usdt_rules_are_refused_on_every_call_even_when_cached() -> None:
    exchange = FakeExchange(
        rules={("ETHBTC", Category.SPOT): make_rules("ETHBTC", Category.SPOT, base="ETH", quote="BTC")}
    )
    use_case = _rules_use_case(exchange)

    for _ in range(2):
        with pytest.raises(InvalidParams):
            await use_case("ETHBTC", "spot")
    assert len(exchange.calls) == 1


@pytest.mark.req("FR-28")
@pytest.mark.parametrize("error", [ExchangeUnavailable("down"), NotFound("missing")])
async def test_failures_are_not_cached(error: Exception) -> None:
    exchange = FakeExchange(rules={BTC: make_rules(*BTC)}, fail_with=error)
    use_case = _rules_use_case(exchange)

    with pytest.raises(type(error)):
        await use_case("BTCUSDT", "linear")
    exchange.fail_with = None
    await use_case("BTCUSDT", "linear")

    assert len(exchange.calls) == 2


async def test_cache_size_is_bounded_oldest_entry_goes_first() -> None:
    symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    exchange = FakeExchange(rules={(s, Category.SPOT): make_rules(s, Category.SPOT) for s in symbols})
    cache = InstrumentRulesCache(_port(exchange), FakeClock(), max_entries=2)

    for symbol in symbols:
        await cache.get(symbol, Category.SPOT)
    await cache.get("CCCUSDT", Category.SPOT)  # still cached
    await cache.get("AAAUSDT", Category.SPOT)  # evicted, fetched again

    assert [c[1] for c in exchange.calls] == [*symbols, "AAAUSDT"]


async def test_cache_eviction_is_least_recently_used() -> None:
    symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    exchange = FakeExchange(rules={(s, Category.SPOT): make_rules(s, Category.SPOT) for s in symbols})
    cache = InstrumentRulesCache(_port(exchange), FakeClock(), max_entries=2)

    await cache.get("AAAUSDT", Category.SPOT)
    await cache.get("BBBUSDT", Category.SPOT)
    await cache.get("AAAUSDT", Category.SPOT)  # hit: AAA becomes the most recently used
    await cache.get("CCCUSDT", Category.SPOT)  # evicts BBB, not AAA
    await cache.get("AAAUSDT", Category.SPOT)  # still cached
    await cache.get("BBBUSDT", Category.SPOT)  # evicted, fetched again

    assert [c[1] for c in exchange.calls] == ["AAAUSDT", "BBBUSDT", "CCCUSDT", "BBBUSDT"]


async def test_expired_entry_is_dropped_on_read_even_if_the_refresh_fails() -> None:
    clock = FakeClock()
    exchange = FakeExchange(rules={BTC: make_rules(*BTC)})
    cache = InstrumentRulesCache(_port(exchange), clock)
    await cache.get(*BTC)
    assert len(cache) == 1

    clock.advance(DEFAULT_RULES_TTL)
    exchange.fail_with = ExchangeUnavailable("down")
    with pytest.raises(ExchangeUnavailable):
        await cache.get(*BTC)

    assert len(cache) == 0  # a stale entry is never served and does not linger


@pytest.mark.parametrize(("ttl", "size"), [(timedelta(0), 10), (timedelta(seconds=1), 0)])
def test_cache_rejects_a_useless_configuration(ttl: timedelta, size: int) -> None:
    with pytest.raises(ValueError, match="ttl"):
        InstrumentRulesCache(_port(FakeExchange()), FakeClock(), ttl=ttl, max_entries=size)
