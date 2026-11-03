"""Order domain, review round 1 of ticket 011: the risk sum of a market sell, exact limit
boundaries, reduce-only below the minimum order value, strict amount parsing (FR-10, FR-13, FR-14,
FR-19, FR-43)."""

from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from guarded_exchange.domain.account import OrderSide
from guarded_exchange.domain.actions import ActionKind, OrderKind
from guarded_exchange.domain.errors import InstrumentRuleViolation, InvalidParams, LimitExceeded
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.limits import check_limits, requires_confirmation
from guarded_exchange.domain.money import is_multiple, parse_amount, round_down, round_up
from guarded_exchange.domain.orders import BestPrices, OrderPlan, plan_order
from tests.fakes import make_rules
from tests.unit.test_order_domain import BOOK, RULES, SPOT_RULES, WIDE, long_position, order, policy


def _market_sell(qty: str = "0.0125") -> OrderPlan:
    """bid 80000.0, slippage 2 %: worst (and IOC bound) 78400.0."""
    sell = order(qty=qty, kind=OrderKind.MARKET, price=None, side=OrderSide.SELL)
    return plan_order(sell, RULES, band=WIDE, slippage_pct=Decimal(2), best=BOOK)


# --- FR-13 / FR-19: a market sell is worth up to qty x best bid --------------------------------


@pytest.mark.req("FR-13")
def test_market_sell_risk_sum_uses_the_best_bid_not_its_lower_bound() -> None:
    plan = _market_sell()

    assert plan.price == Decimal("78400.0")  # what is sent
    assert plan.notional_price == Decimal("80000.0")  # it can fill up to the bid
    assert plan.notional == Decimal("0.012") * Decimal("80000.0")  # qty rounded to 0.012
    assert plan.order_value == Decimal("0.012") * Decimal("78400.0")


@pytest.mark.req("FR-13", "FR-17")
def test_market_sell_within_the_limit_at_its_bound_but_above_at_the_bid_is_refused() -> None:
    plan = _market_sell()  # qty x worst = 940.8 <= 950 < qty x bid = 960

    with pytest.raises(LimitExceeded) as error:
        check_limits(plan, policy(max_order_notional_usdt=Decimal(950)), leverage=Decimal(10))

    assert error.value.details["notional"] == "960"
    assert error.value.details["price"] == "80000"
    assert "best bid 80000" in error.value.message
    check_limits(plan, policy(max_order_notional_usdt=Decimal(960)), leverage=Decimal(10))


@pytest.mark.req("FR-19")
def test_market_sell_confirmation_threshold_uses_the_risk_sum() -> None:
    plan = _market_sell()
    threshold = policy(confirm_above_notional_usdt=Decimal(950))  # 940.8 < 950 <= 960

    assert requires_confirmation(threshold, ActionKind.PLACE_ORDER, plan.notional)


def test_a_market_buy_risk_sum_is_its_bound() -> None:
    plan = plan_order(
        order(kind=OrderKind.MARKET, price=None),
        RULES,
        band=WIDE,
        slippage_pct=Decimal(2),
        best=BOOK,
    )
    assert plan.notional_price == plan.price
    assert plan.notional == plan.order_value


@pytest.mark.req("FR-10")
def test_minimum_order_value_of_a_sell_is_checked_at_the_order_price() -> None:
    """Spot sell 0.00005 at bid 100000: risk sum 5.0, but the order (bound 97000) is worth 4.85 < 5."""
    spot_sell = order(
        category=Category.SPOT, qty="0.00005", kind=OrderKind.MARKET, price=None, side=OrderSide.SELL
    )
    best = BestPrices(bid=Decimal("100000.00"), ask=Decimal("100000.01"))

    with pytest.raises(InstrumentRuleViolation) as error:
        plan_order(spot_sell, SPOT_RULES, band=WIDE, slippage_pct=Decimal(3), best=best)

    assert error.value.details["rule"] == "min_notional"
    assert error.value.details["notional"] == "4.85"


# --- exact boundaries (FR-13, FR-14) -------------------------------------------------------------


@pytest.mark.req("FR-13")
def test_an_order_exactly_at_the_limit_passes_and_one_step_more_is_refused() -> None:
    at_limit = plan_order(order(qty="0.012"), RULES, band=WIDE, slippage_pct=Decimal(2))  # 960
    one_more = plan_order(order(qty="0.013"), RULES, band=WIDE, slippage_pct=Decimal(2))  # 1040
    limit = policy(max_order_notional_usdt=Decimal(960))

    check_limits(at_limit, limit, leverage=Decimal(10))
    with pytest.raises(LimitExceeded):
        check_limits(one_more, limit, leverage=Decimal(10))


@pytest.mark.req("FR-14")
def test_leverage_exactly_at_the_maximum_passes_and_above_is_refused() -> None:
    plan = plan_order(order(), RULES, band=WIDE, slippage_pct=Decimal(2))

    check_limits(plan, policy(max_leverage=Decimal(10)), leverage=Decimal(10))
    with pytest.raises(LimitExceeded):
        check_limits(plan, policy(max_leverage=Decimal(10)), leverage=Decimal("10.01"))


# --- reduce-only below the minimum order value (FR-43) -------------------------------------------


@pytest.mark.req("FR-43")
def test_reduce_only_remainder_below_the_minimum_order_value_is_still_planned() -> None:
    rules = replace(make_rules(), min_notional=Decimal(100))
    closing = order(side=OrderSide.SELL, reduce_only=True, qty="0.001", price="80200.0")  # 80.2 < 100

    plan = plan_order(closing, rules, band=WIDE, slippage_pct=Decimal(2), position=long_position())

    assert plan.order_value == Decimal("80.2000")
    with pytest.raises(InstrumentRuleViolation):  # the same order opening risk is refused
        plan_order(
            order(side=OrderSide.SELL, qty="0.001", price="80200.0"),
            rules,
            band=WIDE,
            slippage_pct=Decimal(2),
        )


# --- strict amounts and exact step arithmetic ----------------------------------------------------


@pytest.mark.req("FR-10")
@pytest.mark.parametrize(
    "raw",
    ["\u0661\u0662", "\uff11\uff12", "1\u066b5", "0.\u0665"],  # Arabic-Indic, fullwidth digits
)
def test_amounts_accept_ascii_digits_only(raw: str) -> None:
    with pytest.raises(InvalidParams):
        parse_amount("qty", raw)


@pytest.mark.req("FR-10")
def test_amounts_with_more_than_28_significant_digits_are_refused() -> None:
    with pytest.raises(InvalidParams) as error:
        parse_amount("qty", "12345678901234567890.123456789")  # 29 significant digits
    assert error.value.details["reason"] == "too_many_digits"
    assert parse_amount("qty", "1234567890123456789.012345678") == Decimal("1234567890123456789.012345678")


def test_step_arithmetic_is_exact_whatever_the_callers_precision() -> None:
    value, step = Decimal("1234567890123456789.012345678"), Decimal("0.000000001")
    with localcontext() as ctx:
        ctx.prec = 5  # a caller with a coarse context must not get a rounded (larger) quantity
        assert round_down(value, step) == value
        assert round_up(value, step) == value
        assert is_multiple(value, step)
        assert round_down(Decimal("0.0129"), Decimal("0.001")) == Decimal("0.012")
