"""Order planning in the domain: rounding, tick, price band, worst price, limits, reduce-only clamp
(ticket 011: FR-10, FR-13, FR-14, FR-15, FR-19, FR-43, ADR-010, GP-1, GP-2)."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from guarded_exchange.domain.account import OrderSide, Position, PositionSide
from guarded_exchange.domain.actions import ActionKind, OrderKind, PlaceOrder, parse_place_order
from guarded_exchange.domain.errors import (
    ExchangeUnavailable,
    InstrumentRuleViolation,
    InvalidParams,
    LimitExceeded,
    PermissionDenied,
    PositionNotFound,
)
from guarded_exchange.domain.instruments import Category, InstrumentStatus
from guarded_exchange.domain.limits import (
    check_limits,
    require_trading_environment,
    requires_confirmation,
)
from guarded_exchange.domain.money import neighbours, parse_amount, round_down, round_up
from guarded_exchange.domain.orders import (
    BestPrices,
    OrderTimeInForce,
    PriceBand,
    QtyAdjustment,
    plan_order,
    worst_price,
)
from guarded_exchange.domain.policy import NO_OVERRIDES as NONE
from guarded_exchange.domain.policy import EffectivePolicy, InstrumentRef, Profile, default_ceiling, strictest
from guarded_exchange.domain.risk import RiskDirection, classify
from guarded_exchange.domain.users import Environment
from tests.fakes import make_rules

BTC_PERP = InstrumentRef(Category.LINEAR, "BTCUSDT")
WIDE = PriceBand(buy_max=Decimal(1_000_000), sell_min=Decimal("0.01"))
BOOK = BestPrices(bid=Decimal("80000.0"), ask=Decimal("80000.1"))
RULES = make_rules()  # linear BTCUSDT: tick 0.10, step 0.001, min qty 0.001, min notional 5
SPOT_RULES = make_rules(category=Category.SPOT)  # tick 0.01, step 0.00001, min notional 5


def policy(**changes: Any) -> EffectivePolicy:
    base = strictest(default_ceiling(Environment.TESTNET), NONE, Environment.TESTNET)
    trading = replace(
        base, profile=Profile.TRADE, allowlist=frozenset({BTC_PERP}), confirm_actions=frozenset()
    )
    return replace(trading, **changes)


def order(
    *,
    side: OrderSide = OrderSide.BUY,
    kind: OrderKind = OrderKind.LIMIT,
    qty: str = "0.01",
    price: str | None = "80000.0",
    reduce_only: bool = False,
    category: Category = Category.LINEAR,
) -> PlaceOrder:
    return PlaceOrder(
        symbol="BTCUSDT",
        category=category,
        side=side,
        order_type=kind,
        qty=Decimal(qty),
        price=None if price is None else Decimal(price),
        reduce_only=reduce_only,
    )


def long_position(size: str = "0.010") -> Position:
    return Position(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        size=Decimal(size),
        entry_price=Decimal("80938.4"),
        mark_price=None,
        liquidation_price=None,
        unrealised_pnl=None,
        position_value=None,
        leverage=Decimal(10),
        take_profit=None,
        stop_loss=None,
        updated_at=datetime(2026, 10, 28, tzinfo=UTC),
    )


# --- money ------------------------------------------------------------------------------------


def test_rounding_keeps_the_step_precision_and_never_uses_float() -> None:
    assert round_down(Decimal("0.0129"), Decimal("0.001")) == Decimal("0.012")
    assert str(round_down(Decimal("81100.43"), Decimal("0.10"))) == "81100.40"
    assert round_up(Decimal("81100.41"), Decimal("0.10")) == Decimal("81100.5")
    assert neighbours(Decimal("80000.05"), Decimal("0.1")) == (Decimal("80000.0"), Decimal("80000.1"))


@pytest.mark.req("FR-10")
@pytest.mark.parametrize("raw", ["0", "-1", "1e3", "0.1.2", "", " ", "NaN", "Infinity", "1,5"])
def test_amounts_are_positive_plain_decimal_strings(raw: str) -> None:
    with pytest.raises(InvalidParams) as error:
        parse_amount("qty", raw)
    assert error.value.details["field"] == "qty"
    assert raw.strip() == "" or raw not in error.value.message  # never echoed


def test_amount_text_keeps_its_exact_value() -> None:
    assert parse_amount("qty", " 0.0100 ") == Decimal("0.0100")


# --- the action -------------------------------------------------------------------------------


@pytest.mark.req("FR-4")
@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"order_type": "limit", "price": None}, "price"),
        ({"order_type": "market", "price": "80000"}, "price"),
        ({"category": "spot", "reduce_only": True}, "reduce_only"),
        ({"side": "long"}, "side"),
        ({"order_type": "stop"}, "order_type"),
    ],
)
def test_malformed_orders_are_invalid_params(kwargs: dict[str, object], field: str) -> None:
    args: dict[str, object] = {
        "symbol": "btcusdt",
        "category": "linear",
        "side": "buy",
        "order_type": "limit",
        "qty": "0.01",
        "price": "80000",
        "reduce_only": False,
        **kwargs,
    }
    with pytest.raises(InvalidParams) as error:
        parse_place_order(**args)  # type: ignore[arg-type]
    assert error.value.details["field"] == field


@pytest.mark.req("FR-43")
def test_only_reduce_only_orders_reduce_risk() -> None:
    assert classify(order(reduce_only=True)) is RiskDirection.REDUCES
    assert classify(order()) is RiskDirection.INCREASES
    # GP-5: a spot sell is not modelled as closing anything; strict.
    assert classify(order(side=OrderSide.SELL, category=Category.SPOT)) is RiskDirection.INCREASES


# --- rounding and limits (FR-10, FR-13) --------------------------------------------------------


@pytest.mark.req("FR-10", "FR-13")
def test_edge_limit_fails_before_rounding_but_passes_after() -> None:
    """0.01259 x 80000 = 1007.2 > 1000; rounded down to 0.012 -> 960, within the limit."""
    plan = plan_order(order(qty="0.01259"), RULES, band=WIDE, slippage_pct=Decimal(2))

    assert plan.qty == Decimal("0.012")
    assert plan.qty_adjustment is QtyAdjustment.ROUNDED_DOWN
    assert plan.notional == Decimal("960.0")
    check_limits(plan, policy(), leverage=Decimal(10))  # passes: the limit sees the rounded qty


@pytest.mark.req("FR-10", "FR-13")
def test_edge_min_order_value_passes_before_rounding_but_fails_after() -> None:
    """Spot 0.0000626 x 80000 = 5.008 >= 5; rounded down to 0.00006 -> 4.8 < 5: refused."""
    spot = order(category=Category.SPOT, qty="0.0000626", price="80000.00")

    with pytest.raises(InstrumentRuleViolation) as error:
        plan_order(spot, SPOT_RULES, band=WIDE, slippage_pct=Decimal(2))

    assert error.value.details["rule"] == "min_notional"
    assert error.value.details["notional"] == "4.8"


@pytest.mark.req("FR-10", "FR-13")
def test_rounding_down_can_make_the_qty_too_small() -> None:
    with pytest.raises(InstrumentRuleViolation) as error:
        plan_order(order(qty="0.0009"), RULES, band=WIDE, slippage_pct=Decimal(2))
    assert error.value.details == {
        "rule": "min_qty",
        "field": "qty",
        "min_qty": "0.001",
        "qty_step": "0.001",
        "rounded_qty": "0",
    }


@pytest.mark.req("FR-13", "FR-17")
def test_order_value_above_the_limit_is_refused_with_the_allowed_value() -> None:
    plan = plan_order(order(qty="0.013"), RULES, band=WIDE, slippage_pct=Decimal(2))  # 1040 USDT

    with pytest.raises(LimitExceeded) as error:
        check_limits(plan, policy(), leverage=Decimal(10))

    assert error.value.details == {
        "limit": "max_order_notional_usdt",
        "allowed": "1000",
        "notional": "1040",
        "price": "80000",
    }


@pytest.mark.req("FR-10")
def test_edge_limit_price_off_tick_is_an_error_with_the_nearest_prices() -> None:
    with pytest.raises(InstrumentRuleViolation) as error:
        plan_order(order(price="80000.05"), RULES, band=WIDE, slippage_pct=Decimal(2))

    assert error.value.details["rule"] == "tick_size"
    assert (error.value.details["nearest_lower"], error.value.details["nearest_upper"]) == (
        "80000",
        "80000.1",
    )
    assert "80000 and 80000.1" in error.value.message


@pytest.mark.req("FR-10", "FR-48")
@pytest.mark.parametrize(
    ("side", "price", "key", "bound"),
    [(OrderSide.BUY, "81800.0", "max_price", "81747.7"), (OrderSide.SELL, "80000.0", "min_price", "80128.9")],
)
def test_limit_price_outside_the_exchange_band_is_an_error(
    side: OrderSide, price: str, key: str, bound: str
) -> None:
    """Spike 004: Bybit would silently move such a perp order to the band edge (spot rejects it)."""
    band = PriceBand(buy_max=Decimal("81747.70"), sell_min=Decimal("80128.90"))
    with pytest.raises(InstrumentRuleViolation) as error:
        plan_order(order(side=side, price=price), RULES, band=band, slippage_pct=Decimal(2))
    assert error.value.details == {"rule": "price_band", "field": "price", key: bound}


@pytest.mark.req("FR-10")
def test_a_limit_order_rests_good_till_cancelled_at_its_price() -> None:
    plan = plan_order(order(), RULES, band=WIDE, slippage_pct=Decimal(2))
    assert (plan.time_in_force, plan.price, plan.qty_adjustment) == (
        OrderTimeInForce.GTC,
        Decimal("80000.0"),
        None,
    )


# --- market orders: worst price (ADR-010) ------------------------------------------------------


@pytest.mark.req("FR-13", "FR-48")
def test_market_notional_is_computed_at_the_worst_price() -> None:
    """Buy: ask 80000.1 x 1.02 = 81600.102 -> down to the tick 81600.1; notional = qty x that."""
    plan = plan_order(
        order(kind=OrderKind.MARKET, price=None), RULES, band=WIDE, slippage_pct=Decimal(2), best=BOOK
    )

    assert plan.time_in_force is OrderTimeInForce.IOC
    assert plan.best_price == Decimal("80000.1")
    assert plan.worst_price == plan.price == Decimal("81600.1")
    assert plan.notional == Decimal("0.01") * Decimal("81600.1")
    with pytest.raises(LimitExceeded):  # 816 USDT: fine at 1000, refused at 800
        check_limits(plan, policy(max_order_notional_usdt=Decimal(800)), leverage=Decimal(10))


@pytest.mark.req("FR-48")
def test_worst_price_rounds_to_the_safe_side() -> None:
    tick = Decimal("0.1")
    # buy: 100.05 * 1.01 = 101.0505 -> 101.0 (down, never above the bound)
    assert worst_price(Decimal("100.05"), OrderSide.BUY, Decimal(1), tick) == Decimal("101.0")
    # sell: 100.05 * 0.99 = 99.0495 -> 99.1 (up, never below the bound)
    assert worst_price(Decimal("100.05"), OrderSide.SELL, Decimal(1), tick) == Decimal("99.1")


@pytest.mark.req("FR-48")
@pytest.mark.parametrize("slippage", ["3.01", "5", "-0.1"])
def test_slippage_above_the_hard_maximum_is_refused(slippage: str) -> None:
    with pytest.raises(LimitExceeded) as error:
        worst_price(Decimal(80000), OrderSide.BUY, Decimal(slippage), Decimal("0.1"))
    assert error.value.details == {"limit": "slippage_pct", "allowed": "3"}


def test_exactly_the_hard_maximum_is_allowed() -> None:
    assert worst_price(Decimal(100), OrderSide.BUY, Decimal(3), Decimal("0.1")) == Decimal("103.0")


@pytest.mark.req("FR-48")
def test_market_bound_stays_inside_the_price_band() -> None:
    """The band can only improve the bound: buy lower, sell higher than the worst price."""
    band = PriceBand(buy_max=Decimal("81000.05"), sell_min=Decimal("79500.01"))
    buy = plan_order(
        order(kind=OrderKind.MARKET, price=None), RULES, band=band, slippage_pct=Decimal(2), best=BOOK
    )
    sell = plan_order(
        order(kind=OrderKind.MARKET, price=None, side=OrderSide.SELL),
        RULES,
        band=band,
        slippage_pct=Decimal(2),
        best=BOOK,
    )

    assert (buy.worst_price, buy.price) == (Decimal("81600.1"), Decimal("81000.0"))
    assert (sell.worst_price, sell.price) == (Decimal("78400.0"), Decimal("79500.1"))


def test_a_market_order_needs_the_opposite_side_of_the_book() -> None:
    with pytest.raises(ExchangeUnavailable) as error:
        plan_order(
            order(kind=OrderKind.MARKET, price=None),
            RULES,
            band=WIDE,
            slippage_pct=Decimal(2),
            best=BestPrices(bid=Decimal(1), ask=None),
        )
    assert error.value.details == {"reason": "no_market_price"}


# --- allowlist, leverage, status (FR-14, FR-15) -------------------------------------------------


@pytest.mark.req("FR-15", "FR-17")
def test_instrument_outside_the_allowlist_is_refused() -> None:
    plan = plan_order(order(), RULES, band=WIDE, slippage_pct=Decimal(2))

    with pytest.raises(LimitExceeded) as error:
        check_limits(plan, policy(allowlist=frozenset()), leverage=Decimal(10))

    assert error.value.details == {"limit": "allowlist", "instrument": "linear:BTCUSDT"}


@pytest.mark.req("FR-15", "FR-43")
def test_reduce_only_outside_the_allowlist_with_a_position_is_allowed() -> None:
    plan = plan_order(
        order(side=OrderSide.SELL, reduce_only=True, qty="0.005", price="80200.0"),
        RULES,
        band=WIDE,
        slippage_pct=Decimal(2),
        position=long_position(),
    )

    check_limits(
        plan, policy(allowlist=frozenset(), max_order_notional_usdt=Decimal(1)), leverage=Decimal(50)
    )
    assert plan.risk is RiskDirection.REDUCES


@pytest.mark.req("FR-43")
def test_edge_reduce_only_larger_than_the_position_is_cut_to_the_position() -> None:
    plan = plan_order(
        order(side=OrderSide.SELL, reduce_only=True, qty="0.5", price="80200.0"),
        RULES,
        band=WIDE,
        slippage_pct=Decimal(2),
        position=long_position("0.010"),
    )

    assert plan.qty == Decimal("0.010")
    assert plan.qty_adjustment is QtyAdjustment.CLAMPED_TO_POSITION
    assert plan.position_size == Decimal("0.010")


@pytest.mark.req("FR-43")
@pytest.mark.parametrize(
    ("side", "position", "reason"),
    [(OrderSide.SELL, None, "no_position"), (OrderSide.BUY, long_position(), "side_would_increase")],
)
def test_reduce_only_without_a_position_it_can_reduce_is_refused(
    side: OrderSide, position: Position | None, reason: str
) -> None:
    with pytest.raises(PositionNotFound) as error:
        plan_order(
            order(side=side, reduce_only=True), RULES, band=WIDE, slippage_pct=Decimal(2), position=position
        )
    assert error.value.details["reason"] == reason


@pytest.mark.req("FR-14", "FR-17")
def test_a_perpetual_set_to_leverage_above_the_policy_is_refused() -> None:
    plan = plan_order(order(), RULES, band=WIDE, slippage_pct=Decimal(2))

    with pytest.raises(LimitExceeded) as error:
        check_limits(plan, policy(max_leverage=Decimal(5)), leverage=Decimal(10))
    assert error.value.details == {"limit": "max_leverage", "allowed": "5", "leverage": "10"}

    with pytest.raises(LimitExceeded):  # unknown leverage: fail closed
        check_limits(plan, policy(), leverage=None)


def test_spot_has_no_leverage_limit() -> None:
    spot_policy = policy(allowlist=frozenset({InstrumentRef(Category.SPOT, "BTCUSDT")}))
    plan = plan_order(
        order(category=Category.SPOT, price="80000.00"), SPOT_RULES, band=WIDE, slippage_pct=Decimal(2)
    )
    check_limits(plan, spot_policy, leverage=None)


@pytest.mark.req("FR-10", "FR-43")
def test_new_orders_need_a_trading_instrument_while_reduce_only_may_close_on_a_delisting_one() -> None:
    settling = make_rules(status=InstrumentStatus.SETTLING)
    with pytest.raises(InstrumentRuleViolation) as error:
        plan_order(order(), settling, band=WIDE, slippage_pct=Decimal(2))
    assert error.value.details == {"rule": "status", "status": "settling"}

    closing = order(side=OrderSide.SELL, reduce_only=True, qty="0.010", price="80200.0")
    plan = plan_order(closing, settling, band=WIDE, slippage_pct=Decimal(2), position=long_position())
    assert plan.qty == Decimal("0.010")

    with pytest.raises(InstrumentRuleViolation):
        plan_order(
            closing,
            make_rules(status=InstrumentStatus.CLOSED),
            band=WIDE,
            slippage_pct=Decimal(2),
            position=long_position(),
        )


# --- confirmation rule and environment ---------------------------------------------------------


@pytest.mark.req("FR-19")
def test_confirmation_is_required_by_kind_or_by_order_value() -> None:
    assert requires_confirmation(
        policy(confirm_actions=frozenset({ActionKind.PLACE_ORDER})), ActionKind.PLACE_ORDER, Decimal(1)
    )
    assert not requires_confirmation(policy(), ActionKind.PLACE_ORDER, Decimal(99))
    assert requires_confirmation(
        policy(confirm_above_notional_usdt=Decimal(100)), ActionKind.PLACE_ORDER, Decimal(100)
    )
    assert not requires_confirmation(
        policy(confirm_above_notional_usdt=Decimal(100)), ActionKind.PLACE_ORDER, Decimal(99)
    )
    # The default policy confirms everything (FR-19).
    default = strictest(default_ceiling(Environment.TESTNET), NONE, Environment.TESTNET)
    assert requires_confirmation(default, ActionKind.PLACE_ORDER, Decimal(1))


def test_v1_trades_on_the_test_environment_only() -> None:
    require_trading_environment(Environment.TESTNET)
    with pytest.raises(PermissionDenied) as error:
        require_trading_environment(Environment.MAINNET)
    assert error.value.details == {"reason": "mainnet_trading_unavailable"}
