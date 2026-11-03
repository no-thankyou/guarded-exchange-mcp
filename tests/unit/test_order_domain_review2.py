"""Order domain, review round 2 of ticket 011: products of amounts do not depend on the caller's
decimal context (FR-10, FR-13, ADR-010)."""

from decimal import Decimal, localcontext

import pytest

from guarded_exchange.domain.account import OrderSide
from guarded_exchange.domain.actions import OrderKind
from guarded_exchange.domain.money import exact_product, shift_by_pct
from guarded_exchange.domain.orders import BestPrices, plan_order, worst_price
from tests.unit.test_order_domain import RULES, WIDE, order

TICK = Decimal("0.10")
BEST = Decimal("80571.1")


@pytest.mark.req("FR-10")
def test_products_of_amounts_are_exact_whatever_the_callers_precision() -> None:
    with localcontext() as ctx:
        ctx.prec = 4
        assert exact_product(Decimal("0.012"), BEST) == Decimal("966.8532")
        assert shift_by_pct(BEST, Decimal(1)) == Decimal("81376.811")
        assert shift_by_pct(BEST, Decimal(-1)) == Decimal("79765.389")


@pytest.mark.req("FR-13")
def test_worst_price_does_not_depend_on_the_callers_precision() -> None:
    """At prec 4, ``80571.1 x 1.01`` would be 81380 - a buy bound above ``best x (1 + slippage)``."""
    with localcontext() as ctx:
        ctx.prec = 4
        buy = worst_price(BEST, OrderSide.BUY, Decimal(1), TICK)
        sell = worst_price(BEST, OrderSide.SELL, Decimal(1), TICK)

    assert (buy, sell) == (Decimal("81376.8"), Decimal("79765.4"))


@pytest.mark.req("FR-13")
def test_order_sums_do_not_depend_on_the_callers_precision() -> None:
    limit = order(qty="0.0125", price="80571.1")  # qty rounded down to 0.012
    market_sell = order(qty="0.012", kind=OrderKind.MARKET, price=None, side=OrderSide.SELL)
    book = BestPrices(bid=BEST, ask=Decimal("80571.2"))
    with localcontext() as ctx:
        ctx.prec = 4
        limit_plan = plan_order(limit, RULES, band=WIDE, slippage_pct=Decimal(1))
        sell_plan = plan_order(market_sell, RULES, band=WIDE, slippage_pct=Decimal(1), best=book)

    assert (limit_plan.notional, limit_plan.order_value) == (Decimal("966.8532"), Decimal("966.8532"))
    assert sell_plan.price == Decimal("79765.4")
    assert (sell_plan.notional, sell_plan.order_value) == (Decimal("966.8532"), Decimal("957.1848"))
