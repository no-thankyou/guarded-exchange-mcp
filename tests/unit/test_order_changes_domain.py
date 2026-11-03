"""Domain of order changes: amend risk classification (FR-43), amend planning (FR-10, FR-13), which
orders cancel-all touches and how external ones are marked (FR-5, FR-21) - ticket 013."""

from dataclasses import replace
from decimal import Decimal

import pytest

from guarded_exchange.domain.account import (
    ConditionalKind,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    Page,
    PositionSide,
)
from guarded_exchange.domain.actions import (
    AmendOrder,
    CancelAllOrders,
    CancelOrder,
    parse_amend_order,
    parse_order_id,
)
from guarded_exchange.domain.errors import (
    InstrumentRuleViolation,
    InvalidParams,
    LimitExceeded,
    NotFound,
    PositionNotFound,
)
from guarded_exchange.domain.instruments import Category, InstrumentStatus
from guarded_exchange.domain.limits import check_amend_limits
from guarded_exchange.domain.order_changes import (
    CANCEL_ALL_MAX,
    OrderAmendment,
    amend_applied,
    plan_amend,
    plan_cancel,
    plan_cancel_all,
)
from guarded_exchange.domain.orders import QtyAdjustment
from guarded_exchange.domain.policy import NO_OVERRIDES, InstrumentRef, default_ceiling, strictest
from guarded_exchange.domain.risk import RiskDirection, classify_amend
from guarded_exchange.domain.users import Environment
from tests.fakes import BAND, make_position, make_rules
from tests.fakes_trading import plain_order

pytestmark = pytest.mark.req("FR-43")

INC, RED = RiskDirection.INCREASES, RiskDirection.REDUCES
LINEAR = Category.LINEAR


def amend(order_id: str = "o-1", qty: str | None = None, price: str | None = None) -> AmendOrder:
    return parse_amend_order(symbol="BTCUSDT", category="linear", order_id=order_id, qty=qty, price=price)


# --- classification: every combination of size, price and side ---------------------------------

_QTY = {"smaller": Decimal("0.001"), "same": Decimal("0.002"), "larger": Decimal("0.003")}
_PRICE = {"lower": Decimal(79000), "same": Decimal(80000), "higher": Decimal(81000)}


def _expected(side: OrderSide, qty: str, price: str) -> RiskDirection:
    worse = price == ("higher" if side is OrderSide.BUY else "lower")  # a buy pays more, a sell gets less
    return INC if qty == "larger" or worse else RED


@pytest.mark.parametrize("side", list(OrderSide))
@pytest.mark.parametrize("qty", list(_QTY))
@pytest.mark.parametrize("price", list(_PRICE))
def test_amend_increases_risk_only_with_a_larger_size_or_a_worse_price(
    side: OrderSide, qty: str, price: str
) -> None:
    got = classify_amend(
        side=side,
        reduce_only=False,
        current_qty=_QTY["same"],
        current_price=_PRICE["same"],
        new_qty=_QTY[qty],
        new_price=_PRICE[price],
    )
    assert got is _expected(side, qty, price)


@pytest.mark.parametrize("side", list(OrderSide))
@pytest.mark.parametrize("qty", list(_QTY))
@pytest.mark.parametrize("price", list(_PRICE))
def test_amending_a_reduce_only_order_always_reduces_risk(side: OrderSide, qty: str, price: str) -> None:
    got = classify_amend(
        side=side,
        reduce_only=True,
        current_qty=_QTY["same"],
        current_price=_PRICE["same"],
        new_qty=_QTY[qty],
        new_price=_PRICE[price],
    )
    assert got is RED


def test_the_classification_table_has_both_directions_for_each_side() -> None:
    """Guards the parametrised expectation itself: a buy raised in price and a sell lowered increase."""
    assert _expected(OrderSide.BUY, "same", "higher") is INC
    assert _expected(OrderSide.BUY, "same", "lower") is RED
    assert _expected(OrderSide.SELL, "same", "lower") is INC
    assert _expected(OrderSide.SELL, "same", "higher") is RED


# --- actions ------------------------------------------------------------------------------------


def test_an_amend_needs_something_to_change_and_positive_values() -> None:
    with pytest.raises(InvalidParams) as nothing:
        amend()
    assert nothing.value.details["reason"] == "no_change"
    with pytest.raises(InvalidParams):
        amend(qty="0")


@pytest.mark.parametrize("raw", ["", "a b", "x" * 65, "../etc", "id;drop"])
def test_an_order_id_is_checked_and_never_quoted(raw: str) -> None:
    with pytest.raises(InvalidParams) as error:
        parse_order_id(raw)
    assert raw not in error.value.message or raw == ""


# --- amend plan -----------------------------------------------------------------------------------


def test_a_larger_buy_is_planned_as_increasing_with_its_new_value() -> None:
    plan = plan_amend(amend(qty="0.0035"), plain_order("o-1"), make_rules(), band=BAND, position=None)

    assert (plan.risk, plan.qty, plan.qty_adjustment) == (INC, Decimal("0.003"), QtyAdjustment.ROUNDED_DOWN)
    assert plan.notional == Decimal(240)  # 0.003 x 80000: the whole order, filled part included
    assert OrderAmendment.from_plan(plan).price is None  # only what changes is sent


def test_a_cheaper_buy_reduces_risk_and_sends_only_the_price() -> None:
    plan = plan_amend(amend(price="79990.5"), plain_order("o-1"), make_rules(), band=BAND, position=None)

    assert plan.risk is RED
    amendment = OrderAmendment.from_plan(plan)
    assert (amendment.qty, amendment.price) == (None, Decimal("79990.5"))


@pytest.mark.req("FR-10")
def test_a_new_price_off_the_tick_or_outside_the_band_is_refused() -> None:
    with pytest.raises(InstrumentRuleViolation) as tick:
        plan_amend(amend(price="80000.05"), plain_order("o-1"), make_rules(), band=BAND, position=None)
    assert tick.value.details["rule"] == "tick_size"
    with pytest.raises(InstrumentRuleViolation) as band:
        plan_amend(amend(price="90000"), plain_order("o-1"), make_rules(), band=BAND, position=None)
    assert band.value.details["rule"] == "price_band"


def test_no_change_and_a_qty_not_above_the_filled_part_are_refused() -> None:
    with pytest.raises(InvalidParams) as same:
        plan_amend(
            amend(qty="0.002", price="80000"), plain_order("o-1"), make_rules(), band=BAND, position=None
        )
    assert same.value.details["reason"] == "no_change"
    partly = plain_order("o-1", status=OrderStatus.PARTIALLY_FILLED, filled="0.002", qty="0.003")
    with pytest.raises(InvalidParams) as filled:
        plan_amend(amend(qty="0.002"), partly, make_rules(), band=BAND, position=None)
    assert filled.value.details["reason"] == "not_above_filled"


@pytest.mark.req("FR-15")
def test_a_reducing_amend_works_on_a_settling_instrument_an_increasing_one_does_not() -> None:
    settling = make_rules(status=InstrumentStatus.SETTLING)
    assert plan_amend(amend(qty="0.001"), plain_order("o-1"), settling, band=BAND, position=None).risk is RED
    with pytest.raises(InstrumentRuleViolation):
        plan_amend(amend(qty="0.003"), plain_order("o-1"), settling, band=BAND, position=None)


def test_a_growing_reduce_only_order_is_cut_to_the_position() -> None:
    order = plain_order("o-1", side=OrderSide.SELL, price="81000", qty="0.004", reduce_only=True)
    long = make_position(side=PositionSide.LONG)  # size 0.010

    plan = plan_amend(amend(qty="0.05"), order, make_rules(), band=BAND, position=long)

    assert (plan.risk, plan.qty, plan.qty_adjustment) == (
        RED,
        Decimal("0.010"),
        QtyAdjustment.CLAMPED_TO_POSITION,
    )
    with pytest.raises(PositionNotFound):
        plan_amend(amend(qty="0.05"), order, make_rules(), band=BAND, position=None)


def test_only_open_plain_limit_orders_can_be_amended() -> None:
    with pytest.raises(NotFound) as closed:
        plan_amend(
            amend(qty="0.003"),
            plain_order("o-1", status=OrderStatus.FILLED),
            make_rules(),
            band=BAND,
            position=None,
        )
    assert closed.value.details == {"reason": "order_not_open", "order_status": "filled"}
    stop = plain_order("o-1", conditional=ConditionalKind.STOP_LOSS)
    with pytest.raises(InvalidParams) as conditional:
        plan_amend(amend(qty="0.003"), stop, make_rules(), band=BAND, position=None)
    assert conditional.value.details["reason"] == "conditional_order"
    with pytest.raises(NotFound):
        plan_amend(amend(qty="0.003"), None, make_rules(), band=BAND, position=None)


@pytest.mark.req("FR-13")
def test_amend_limits_apply_to_increasing_changes_only() -> None:
    ceiling = replace(
        default_ceiling(Environment.TESTNET), max_order_notional_usdt=Decimal(100), allowlist=frozenset()
    )
    strict = strictest(ceiling, NO_OVERRIDES, Environment.TESTNET)
    reducing = plan_amend(amend(qty="0.001"), plain_order("o-1"), make_rules(), band=BAND, position=None)
    check_amend_limits(reducing, strict, leverage=None)  # nothing applies to a reducing change
    growing = plan_amend(amend(qty="0.003"), plain_order("o-1"), make_rules(), band=BAND, position=None)
    with pytest.raises(LimitExceeded) as allow:
        check_amend_limits(growing, strict, leverage=Decimal(1))
    assert allow.value.details["limit"] == "allowlist"
    listed = replace(strict, allowlist=frozenset({InstrumentRef(LINEAR, "BTCUSDT")}))
    with pytest.raises(LimitExceeded) as value:
        check_amend_limits(growing, listed, leverage=Decimal(1))
    assert value.value.details["limit"] == "max_order_notional_usdt"


def test_amend_applied_compares_only_what_changes() -> None:
    plan = plan_amend(amend(price="79990.5"), plain_order("o-1"), make_rules(), band=BAND, position=None)
    assert not amend_applied(plan, plain_order("o-1"))
    assert amend_applied(plan, plain_order("o-1", price="79990.5", qty="0.002"))
    assert not amend_applied(plan, None)


# --- cancel and cancel-all --------------------------------------------------------------------------


@pytest.mark.req("FR-5", "FR-21")
def test_cancel_all_lists_the_plain_open_orders_and_marks_external_ones() -> None:
    page = Page(
        items=(
            plain_order("ours"),
            plain_order("manual", origin=OrderOrigin.EXTERNAL),
            plain_order("stop", conditional=ConditionalKind.STOP_LOSS, status=OrderStatus.UNTRIGGERED),
            plain_order(
                "tp-limit", conditional=ConditionalKind.TAKE_PROFIT
            ),  # listed as New: still not plain
            plain_order("done", status=OrderStatus.FILLED),
            replace(plain_order("eth"), symbol="ETHUSDT"),
        ),
        has_more=False,
    )

    plan = plan_cancel_all(CancelAllOrders("BTCUSDT", LINEAR), page)

    assert [o.order_id for o in plan.orders] == ["ours", "manual"]
    assert [o.external for o in plan.orders] == [False, True]
    assert (plan.external_count, plan.has_more, plan.risk) == (1, False, RED)


def test_cancel_all_takes_at_most_its_maximum_and_says_there_is_more() -> None:
    page = Page(items=tuple(plain_order(f"o-{i}") for i in range(CANCEL_ALL_MAX + 1)), has_more=False)

    plan = plan_cancel_all(CancelAllOrders("BTCUSDT", LINEAR), page)

    assert len(plan.orders) == CANCEL_ALL_MAX
    assert plan.has_more


@pytest.mark.req("FR-5")
def test_a_cancel_targets_exactly_the_order_read_and_reduces_risk() -> None:
    plan = plan_cancel(CancelOrder("BTCUSDT", LINEAR, "o-1"), plain_order("o-1", origin=OrderOrigin.EXTERNAL))
    assert (plan.order.order_id, plan.order.external, plan.risk) == ("o-1", True, RED)
    with pytest.raises(NotFound):
        plan_cancel(CancelOrder("BTCUSDT", LINEAR, "o-2"), plain_order("o-1"))


def test_a_reduce_only_buy_against_a_long_cannot_grow() -> None:
    """A reduce-only buy reduces a short only; against a long there is nothing it may grow into."""
    order = plain_order("o-1", side=OrderSide.BUY, qty="0.002", reduce_only=True)
    long = make_position(side=PositionSide.LONG)

    with pytest.raises(PositionNotFound):
        plan_amend(amend(qty="0.004"), order, make_rules(), band=BAND, position=long)
    short = make_position(side=PositionSide.SHORT)
    assert plan_amend(amend(qty="0.004"), order, make_rules(), band=BAND, position=short).qty == Decimal(
        "0.004"
    )


@pytest.mark.req("FR-10")
def test_only_a_reduce_only_order_is_exempt_from_the_minimum_order_value() -> None:
    rules = replace(make_rules(), min_notional=Decimal(1000))
    reduce_only = plain_order("o-1", side=OrderSide.SELL, price="81000", qty="0.004", reduce_only=True)
    plain = plain_order("o-2", side=OrderSide.SELL, price="81000", qty="0.004")

    assert plan_amend(amend("o-1", qty="0.003"), reduce_only, rules, band=BAND, position=None).qty == Decimal(
        "0.003"
    )
    with pytest.raises(InstrumentRuleViolation) as error:
        plan_amend(amend("o-2", qty="0.003"), plain, rules, band=BAND, position=None)
    assert error.value.details["rule"] == "min_notional"
