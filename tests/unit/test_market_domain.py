"""Domain value objects of market data and instrument rules."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from guarded_exchange.domain.errors import InvalidParams
from guarded_exchange.domain.instruments import (
    Category,
    Instrument,
    InstrumentStatus,
    require_supported_quote,
)
from guarded_exchange.domain.market import (
    MAX_CANDLES,
    MAX_ORDERBOOK_DEPTH,
    Candle,
    CandleInterval,
    Candles,
    OrderbookLevel,
    candles_limit,
    orderbook_depth,
    parse_interval,
)
from tests.fakes import make_candles, make_orderbook, make_rules

pytestmark = pytest.mark.req("FR-1")


# --- USDT only (FR-4) ------------------------------------------------------------------------


@pytest.mark.req("FR-4")
def test_usdt_quoted_instrument_is_supported() -> None:
    require_supported_quote(Instrument("BTCUSDT", Category.LINEAR, base="BTC", quote="USDT"))


@pytest.mark.req("FR-4", "FR-28")
@pytest.mark.parametrize(("symbol", "base", "quote"), [("ETHBTC", "ETH", "BTC"), ("BTCPERP", "BTC", "USDC")])
def test_instrument_not_quoted_in_usdt_is_rejected_with_a_hint(symbol: str, base: str, quote: str) -> None:
    with pytest.raises(InvalidParams) as err:
        require_supported_quote(Instrument(symbol, Category.SPOT, base=base, quote=quote))

    assert err.value.code == "invalid_params"
    assert err.value.details == {"field": "symbol", "reason": "quote_not_supported", "quote": quote}
    assert f"quoted in {quote}" in err.value.message
    assert err.value.next_step is not None
    assert f"{base}USDT" in err.value.next_step


# --- instrument rules invariants -------------------------------------------------------------


def test_only_trading_status_accepts_orders() -> None:
    assert make_rules().is_trading
    for status in set(InstrumentStatus) - {InstrumentStatus.TRADING}:
        assert not make_rules(status=status).is_trading


@pytest.mark.parametrize(
    "change",
    [
        {"tick_size": Decimal(0)},
        {"tick_size": Decimal("NaN")},
        {"qty_step": Decimal("-0.001")},
        {"min_qty": Decimal(-1)},
        {"min_notional": Decimal("-5")},
        {"max_leverage": Decimal("0.5")},
        {"max_leverage": Decimal("Infinity")},
    ],
)
def test_rules_reject_impossible_values(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="must be"):
        replace(make_rules(), **change)


# --- order book ------------------------------------------------------------------------------


def test_orderbook_top_cuts_each_side_to_depth() -> None:
    book = make_orderbook(levels=7)

    top = book.top(3)

    assert len(top.bids) == len(top.asks) == 3
    assert top.bids == book.bids[:3]
    assert top.asks == book.asks[:3]
    assert book.top(7) is book


@pytest.mark.parametrize(
    ("bids", "asks"),
    [
        ((OrderbookLevel(Decimal(1), Decimal(1)), OrderbookLevel(Decimal(2), Decimal(1))), ()),
        ((), (OrderbookLevel(Decimal(2), Decimal(1)), OrderbookLevel(Decimal(1), Decimal(1)))),
        ((), (OrderbookLevel(Decimal(2), Decimal(1)), OrderbookLevel(Decimal(2), Decimal(3)))),
        ((OrderbookLevel(Decimal(0), Decimal(1)),), ()),
        ((), (OrderbookLevel(Decimal(1), Decimal(-1)),)),
    ],
    ids=["bids-ascending", "asks-descending", "duplicate-price", "zero-price", "negative-qty"],
)
def test_orderbook_rejects_inconsistent_levels(
    bids: tuple[OrderbookLevel, ...], asks: tuple[OrderbookLevel, ...]
) -> None:
    with pytest.raises(ValueError, match=r"bids|asks"):
        replace(make_orderbook(), bids=bids, asks=asks)


@pytest.mark.req("FR-3", "FR-28")
@pytest.mark.parametrize("depth", [0, -1, MAX_ORDERBOOK_DEPTH + 1, 1000, True, 2.5, 10.0, Decimal(10), "10"])
def test_orderbook_depth_is_bounded(depth: object) -> None:
    with pytest.raises(InvalidParams) as err:
        orderbook_depth(depth)

    assert err.value.details == {"field": "depth"}


@pytest.mark.req("FR-3")
@pytest.mark.parametrize("depth", [1, 10, MAX_ORDERBOOK_DEPTH])
def test_orderbook_depth_within_bounds_passes(depth: int) -> None:
    assert orderbook_depth(depth) == depth


# --- candles ---------------------------------------------------------------------------------


def test_candles_latest_keeps_the_newest_oldest_first() -> None:
    series = make_candles(count=5)

    latest = series.latest(2)

    assert latest.candles == series.candles[-2:]
    assert latest.candles[0].start_at < latest.candles[1].start_at
    assert series.latest(5) is series


def test_candles_must_be_strictly_oldest_first() -> None:
    series = make_candles(count=3)

    with pytest.raises(ValueError, match="oldest first"):
        replace(series, candles=tuple(reversed(series.candles)))
    with pytest.raises(ValueError, match="oldest first"):
        replace(series, candles=(series.candles[0], series.candles[0]))


@pytest.mark.parametrize(
    "change",
    [{"high": Decimal(79999)}, {"low": Decimal(80002)}, {"volume_base": Decimal(-1)}],
    ids=["high-below-open", "low-above-close", "negative-volume"],
)
def test_candle_rejects_inconsistent_ohlcv(change: dict[str, Any]) -> None:
    candle = Candle(
        start_at=datetime(2026, 10, 28, tzinfo=UTC),
        open=Decimal(80000),
        high=Decimal(80010),
        low=Decimal(79990),
        close=Decimal(80001),
        volume_base=Decimal(1),
        turnover_quote=Decimal(80000),
    )
    with pytest.raises(ValueError, match="candle"):
        replace(candle, **change)


@pytest.mark.req("FR-3", "FR-28")
@pytest.mark.parametrize("limit", [0, MAX_CANDLES + 1, 1000, False, 3.7, 50.0, Decimal(50), "50"])
def test_candles_limit_is_bounded(limit: object) -> None:
    with pytest.raises(InvalidParams) as err:
        candles_limit(limit)

    assert err.value.details == {"field": "limit"}


@pytest.mark.parametrize(("raw", "interval"), [("1h", CandleInterval.H1), (" 1D ", CandleInterval.D1)])
def test_interval_is_parsed_case_insensitively(raw: str, interval: CandleInterval) -> None:
    assert parse_interval(raw) is interval


@pytest.mark.req("FR-28")
@pytest.mark.parametrize("raw", ["60", "1y", "", "hour"])
def test_unknown_interval_is_invalid_params(raw: str) -> None:
    with pytest.raises(InvalidParams) as err:
        parse_interval(raw)

    assert err.value.details == {"field": "interval"}


def test_empty_series_is_valid() -> None:
    assert Candles("BTCUSDT", Category.LINEAR, CandleInterval.H1, ()).latest(5).candles == ()
