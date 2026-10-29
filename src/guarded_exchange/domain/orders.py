"""From a requested order to the exact order that goes to the exchange (SPEC FR-10, FR-13, FR-43,
FR-48; PLAN ADR-010, D-15; GP-1, GP-2).

``plan_order`` is pure: it takes the action, the instrument rules, the exchange's price band, the
best prices (market orders) and the position (reduce-only) and returns an ``OrderPlan`` or raises.

- Quantity is rounded **down** to the quantity step after the reduce-only clamp; limits are then
  checked on the rounded quantity (FR-10, FR-13).
- A limit price off the tick is an error with the two nearest valid prices (GP-1). A limit price
  outside the exchange's price band is an error too: Bybit would silently move a perpetual order to
  the band edge and reject a spot one (spike 004), so the price shown and journaled would not be
  the order's price.
- A market order becomes Limit IOC at the worst acceptable price: best opposite price x (1 +/-
  slippage), rounded to the tick on the safe side (buy down, sell up), then kept inside the price
  band (which can only make it better). Its notional uses that price (ADR-010).
"""

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Final

from guarded_exchange.domain.account import OrderSide, Position
from guarded_exchange.domain.actions import PlaceOrder
from guarded_exchange.domain.audit import canonical_decimal
from guarded_exchange.domain.errors import ExchangeUnavailable, InstrumentRuleViolation, LimitExceeded
from guarded_exchange.domain.instruments import Category, InstrumentRules, InstrumentStatus
from guarded_exchange.domain.money import is_multiple, neighbours, round_down, round_up
from guarded_exchange.domain.policy import MAX_SLIPPAGE_PCT
from guarded_exchange.domain.risk import RiskDirection, classify, reduce_only_qty

_HUNDRED: Final = Decimal(100)

REDUCING_STATUSES: Final = frozenset({InstrumentStatus.TRADING, InstrumentStatus.SETTLING})
"""A risk-reducing order may still go to an instrument being delisted (settling): a position there
must stay closable (FR-15, FR-43). The exchange has the last word."""


def _s(value: Decimal) -> str:
    return canonical_decimal(value)


@dataclass(frozen=True, slots=True)
class PriceBand:
    """The exchange's current price corridor for new orders: a buy above ``buy_max`` or a sell below
    ``sell_min`` is moved or refused by the exchange (Bybit ``price-limit``)."""

    buy_max: Decimal
    sell_min: Decimal

    def __post_init__(self) -> None:
        if not (self.buy_max.is_finite() and self.sell_min.is_finite()) or self.sell_min <= 0:
            raise ValueError("price band bounds must be positive finite decimals")
        if self.sell_min > self.buy_max:
            raise ValueError("price band is inverted")


@dataclass(frozen=True, slots=True)
class BestPrices:
    """Best bid and ask right now; ``None``: that side of the book is empty."""

    bid: Decimal | None
    ask: Decimal | None


class OrderTimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"


class QtyAdjustment(StrEnum):
    ROUNDED_DOWN = "rounded_down_to_step"
    CLAMPED_TO_POSITION = "clamped_to_position"


@dataclass(frozen=True, slots=True)
class OrderPlan:
    """Exactly what will be sent, and why.

    ``qty`` is final (rounded, clamped), ``price`` is the limit price sent (for a market order the
    IOC bound), ``notional`` = ``qty x price`` in USDT. ``best_price`` and ``worst_price`` are set for
    market orders only; ``worst_price`` is before the price band, ``price`` after it.
    """

    action: PlaceOrder
    risk: RiskDirection
    qty: Decimal
    qty_adjustment: QtyAdjustment | None
    price: Decimal
    time_in_force: OrderTimeInForce
    notional: Decimal
    best_price: Decimal | None = None
    worst_price: Decimal | None = None
    slippage_pct: Decimal | None = None
    position_size: Decimal | None = None


def worst_price(best: Decimal, side: OrderSide, slippage_pct: Decimal, tick: Decimal) -> Decimal:
    """The worst price a market order may execute at (ADR-010), on the tick, never worse than
    ``best x (1 +/- slippage)``. Slippage above the hard maximum is refused (PLAN section 6)."""
    if not slippage_pct.is_finite() or slippage_pct < 0 or slippage_pct > MAX_SLIPPAGE_PCT:
        raise LimitExceeded(
            f"slippage must be from 0 to {MAX_SLIPPAGE_PCT}%",
            next_step="Do not retry; the slippage tolerance comes from the policy, which only the admin "
            "sets.",
            details={"limit": "slippage_pct", "allowed": _s(MAX_SLIPPAGE_PCT)},
        )
    if side is OrderSide.BUY:
        return round_down(best * (1 + slippage_pct / _HUNDRED), tick)
    return round_up(best * (1 - slippage_pct / _HUNDRED), tick)


def plan_order(
    action: PlaceOrder,
    rules: InstrumentRules,
    *,
    band: PriceBand,
    slippage_pct: Decimal,
    best: BestPrices | None = None,
    position: Position | None = None,
) -> OrderPlan:
    """Size and price ``action`` for the exchange or raise (nothing is sent on any error).

    ``best`` is needed for a market order, ``position`` for a reduce-only one.
    """
    if (rules.instrument.symbol, rules.instrument.category) != (action.symbol, action.category):
        raise ValueError("rules of another instrument")
    risk = classify(action)
    _require_status(rules, risk)
    qty, adjustment = _size(action, rules, position)
    if action.price is not None:  # a limit order (PlaceOrder: limit <=> price)
        price = _limit_price(action.side, action.price, rules, band)
        plan = OrderPlan(action, risk, qty, adjustment, price, OrderTimeInForce.GTC, qty * price)
    else:
        best_price = _best(action, best)
        worst = worst_price(best_price, action.side, slippage_pct, rules.tick_size)
        price = _inside_band(action.side, worst, band, rules.tick_size)
        plan = OrderPlan(
            action,
            risk,
            qty,
            adjustment,
            price,
            OrderTimeInForce.IOC,
            qty * price,
            best_price=best_price,
            worst_price=worst,
            slippage_pct=slippage_pct,
        )
    if risk is RiskDirection.REDUCES and position is not None:  # reduce_only_qty needs a position
        # No minimum order value here: closing a small remainder must stay possible; the
        # exchange refuses it if its own rule applies.
        return replace(plan, position_size=position.size)
    _require_min_notional(plan, rules)
    return plan


def _require_status(rules: InstrumentRules, risk: RiskDirection) -> None:
    allowed = {InstrumentStatus.TRADING} if risk is RiskDirection.INCREASES else REDUCING_STATUSES
    if rules.status not in allowed:
        raise InstrumentRuleViolation(
            f"{rules.instrument.symbol} is not trading (status {rules.status.value})",
            next_step="Do not retry now; check get_instrument_rules for the status.",
            details={"rule": "status", "status": rules.status.value},
        )


def _size(
    action: PlaceOrder, rules: InstrumentRules, position: Position | None
) -> tuple[Decimal, QtyAdjustment | None]:
    wanted = action.qty
    adjustment: QtyAdjustment | None = None
    if action.reduce_only:
        wanted = reduce_only_qty(action, position)
        if wanted < action.qty:
            adjustment = QtyAdjustment.CLAMPED_TO_POSITION
    qty = round_down(wanted, rules.qty_step)
    if qty < wanted and adjustment is None:
        adjustment = QtyAdjustment.ROUNDED_DOWN
    if qty <= 0 or qty < rules.min_qty:
        raise InstrumentRuleViolation(
            f"qty rounded down to the step {_s(rules.qty_step)} is {_s(qty)}, below the minimum "
            f"{_s(rules.min_qty)} {rules.instrument.base}",
            next_step=f"Use at least {_s(max(rules.min_qty, rules.qty_step))} {rules.instrument.base}, "
            f"a multiple of {_s(rules.qty_step)}.",
            details={
                "rule": "min_qty",
                "field": "qty",
                "min_qty": _s(rules.min_qty),
                "qty_step": _s(rules.qty_step),
                "rounded_qty": _s(qty),
            },
        )
    return qty, adjustment


def _limit_price(side: OrderSide, price: Decimal, rules: InstrumentRules, band: PriceBand) -> Decimal:
    tick = rules.tick_size
    if not is_multiple(price, tick):
        lower, upper = neighbours(price, tick)
        raise InstrumentRuleViolation(
            f"price must be a multiple of the tick size {_s(tick)}; nearest valid prices: "
            f"{_s(lower)} and {_s(upper)}",
            next_step=f"Use {_s(lower)} or {_s(upper)} (or another multiple of {_s(tick)}).",
            details={
                "rule": "tick_size",
                "field": "price",
                "tick_size": _s(tick),
                "nearest_lower": _s(lower),
                "nearest_upper": _s(upper),
            },
        )
    if side is OrderSide.BUY and price > band.buy_max:
        raise _outside_band("buy", "above", round_down(band.buy_max, tick), "max_price")
    if side is OrderSide.SELL and price < band.sell_min:
        raise _outside_band("sell", "below", round_up(band.sell_min, tick), "min_price")
    return price


def _outside_band(side: str, where: str, bound: Decimal, key: str) -> InstrumentRuleViolation:
    return InstrumentRuleViolation(
        f"a {side} price {where} {_s(bound)} is outside the exchange's current price band",
        next_step=f"Use a price no {'higher' if where == 'above' else 'lower'} than {_s(bound)}, or a "
        "market order; the band moves with the market.",
        details={"rule": "price_band", "field": "price", key: _s(bound)},
    )


def _best(action: PlaceOrder, best: BestPrices | None) -> Decimal:
    price = None if best is None else (best.ask if action.side is OrderSide.BUY else best.bid)
    if price is None or price <= 0:
        side = "ask" if action.side is OrderSide.BUY else "bid"
        raise ExchangeUnavailable(
            f"there is no best {side} for {action.symbol} right now, so a market order has no price bound",
            next_step="Retry in a few seconds, or place a limit order with an explicit price.",
            details={"reason": "no_market_price"},
        )
    return price


def _inside_band(side: OrderSide, worst: Decimal, band: PriceBand, tick: Decimal) -> Decimal:
    if side is OrderSide.BUY:
        return min(worst, round_down(band.buy_max, tick))
    return max(worst, round_up(band.sell_min, tick))


def _require_min_notional(plan: OrderPlan, rules: InstrumentRules) -> None:
    minimum = rules.min_notional
    if minimum is not None and plan.notional < minimum:
        raise InstrumentRuleViolation(
            f"order value {_s(plan.notional)} {rules.instrument.quote} is below the instrument's minimum "
            f"{_s(minimum)}",
            next_step=f"Increase qty so that qty x price is at least {_s(minimum)} {rules.instrument.quote}.",
            details={
                "rule": "min_notional",
                "field": "qty",
                "min_notional": _s(minimum),
                "notional": _s(plan.notional),
            },
        )


# --- what goes to the exchange and what comes back (port types) ------------------------------


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """One limit order for the exchange adapter (a market order arrives here as IOC)."""

    symbol: str
    category: Category
    side: OrderSide
    qty: Decimal
    price: Decimal
    time_in_force: OrderTimeInForce
    reduce_only: bool
    client_order_id: str

    @classmethod
    def from_plan(cls, plan: OrderPlan, client_order_id: str) -> "OrderRequest":
        return cls(
            symbol=plan.action.symbol,
            category=plan.action.category,
            side=plan.action.side,
            qty=plan.qty,
            price=plan.price,
            time_in_force=plan.time_in_force,
            reduce_only=plan.action.reduce_only,
            client_order_id=client_order_id,
        )


@dataclass(frozen=True, slots=True)
class PlacedOrder:
    """The exchange accepted the request; execution is reported separately (D-15)."""

    order_id: str


class FillStatus(StrEnum):
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    """Also an IOC whose remainder the exchange cancelled; ``filled_qty`` says how much executed."""
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OrderFill:
    """An order as the exchange shows it after placement: its real price and execution so far."""

    order_id: str
    status: FillStatus
    qty: Decimal
    price: Decimal | None
    filled_qty: Decimal
    avg_fill_price: Decimal | None
