"""What ``place_order`` answers, and that answer as stored for repeats of the call (FR-26, ADR-014).

An idempotency record keeps the first call's answer: an accepted order (``OrderResult``) or the
error the agent got. Both are stored as canonical JSON (decimals as exact strings, no floats) and
restored to the same domain objects, so a repeat is answered exactly like the first call, marked as
a replay.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from guarded_exchange.domain.account import OrderSide
from guarded_exchange.domain.actions import OrderKind, PlaceOrder
from guarded_exchange.domain.audit import JsonValue
from guarded_exchange.domain.errors import DomainError, restore_error
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.orders import (
    FillStatus,
    OrderFill,
    OrderPlan,
    OrderTimeInForce,
    QtyAdjustment,
)
from guarded_exchange.domain.risk import RiskDirection

REPLAYED_DETAIL: Final = "replayed"
"""``details`` key of an error that repeats the stored answer of an earlier call."""


class ResultSource(StrEnum):
    PLACED = "placed"
    """This call placed the order and the exchange accepted it."""
    RECONNAISSANCE = "reconnaissance"
    """This call's send had no reliable answer; the order was then found by its client order id."""
    REPLAY = "replay"
    """The stored answer of an earlier call with the same key (or an identical call moments ago)."""


@dataclass(frozen=True, slots=True)
class OrderResult:
    """An order the exchange has. ``fill is None``: its execution is not visible yet (D-15)."""

    action_id: str
    client_order_id: str
    order_id: str
    plan: OrderPlan
    fill: OrderFill | None
    source: ResultSource = ResultSource.PLACED

    @property
    def price(self) -> Decimal:
        """The order's price as the exchange shows it; what was sent while the order is not visible."""
        if self.fill is not None and self.fill.price is not None:
            return self.fill.price
        return self.plan.price


# --- stored answers -------------------------------------------------------------------------------


def answer_payload(answer: OrderResult | DomainError) -> dict[str, JsonValue]:
    """The answer as canonical-JSON-ready data (``Decimal`` stays ``Decimal``)."""
    if isinstance(answer, DomainError):
        return {
            "kind": "error",
            "code": answer.code,
            "message": answer.message,
            "next_step": answer.next_step,
            "details": dict(answer.details),
            "untrusted_text": answer.untrusted_text,
        }
    return {
        "kind": "order",
        "action_id": answer.action_id,
        "client_order_id": answer.client_order_id,
        "order_id": answer.order_id,
        "plan": _plan_payload(answer.plan),
        "fill": None if answer.fill is None else _fill_payload(answer.fill),
    }


def restore_answer(payload: Mapping[str, Any]) -> OrderResult | DomainError:
    """The stored answer as it was given, marked as a replay. A malformed payload raises."""
    if payload["kind"] == "error":
        details = {str(k): str(v) for k, v in payload["details"].items()}
        return restore_error(
            str(payload["code"]),
            str(payload["message"]),
            next_step=payload["next_step"],
            details={**details, REPLAYED_DETAIL: "true"},
            untrusted_text=payload["untrusted_text"],
        )
    if payload["kind"] != "order":
        raise ValueError("unknown stored answer")
    fill = payload["fill"]
    return OrderResult(
        action_id=str(payload["action_id"]),
        client_order_id=str(payload["client_order_id"]),
        order_id=str(payload["order_id"]),
        plan=_plan_from(payload["plan"]),
        fill=None if fill is None else _fill_from(fill),
        source=ResultSource.REPLAY,
    )


def _plan_payload(plan: OrderPlan) -> dict[str, JsonValue]:
    action = plan.action
    return {
        "action": {
            "symbol": action.symbol,
            "category": action.category.value,
            "side": action.side.value,
            "order_type": action.order_type.value,
            "qty": action.qty,
            "price": action.price,
            "reduce_only": action.reduce_only,
        },
        "risk": plan.risk.value,
        "qty": plan.qty,
        "qty_adjustment": None if plan.qty_adjustment is None else plan.qty_adjustment.value,
        "price": plan.price,
        "time_in_force": plan.time_in_force.value,
        "notional": plan.notional,
        "notional_price": plan.notional_price,
        "order_value": plan.order_value,
        "best_price": plan.best_price,
        "worst_price": plan.worst_price,
        "slippage_pct": plan.slippage_pct,
        "position_size": plan.position_size,
    }


def _plan_from(raw: Mapping[str, Any]) -> OrderPlan:
    action = raw["action"]
    adjustment = raw["qty_adjustment"]
    return OrderPlan(
        action=PlaceOrder(
            symbol=str(action["symbol"]),
            category=Category(action["category"]),
            side=OrderSide(action["side"]),
            order_type=OrderKind(action["order_type"]),
            qty=Decimal(action["qty"]),
            price=_dec(action["price"]),
            reduce_only=bool(action["reduce_only"]),
        ),
        risk=RiskDirection(raw["risk"]),
        qty=Decimal(raw["qty"]),
        qty_adjustment=None if adjustment is None else QtyAdjustment(adjustment),
        price=Decimal(raw["price"]),
        time_in_force=OrderTimeInForce(raw["time_in_force"]),
        notional=Decimal(raw["notional"]),
        notional_price=Decimal(raw["notional_price"]),
        order_value=Decimal(raw["order_value"]),
        best_price=_dec(raw["best_price"]),
        worst_price=_dec(raw["worst_price"]),
        slippage_pct=_dec(raw["slippage_pct"]),
        position_size=_dec(raw["position_size"]),
    )


def _fill_payload(fill: OrderFill) -> dict[str, JsonValue]:
    return {
        "order_id": fill.order_id,
        "status": fill.status.value,
        "qty": fill.qty,
        "price": fill.price,
        "filled_qty": fill.filled_qty,
        "avg_fill_price": fill.avg_fill_price,
    }


def _fill_from(raw: Mapping[str, Any]) -> OrderFill:
    return OrderFill(
        order_id=str(raw["order_id"]),
        status=FillStatus(raw["status"]),
        qty=Decimal(raw["qty"]),
        price=_dec(raw["price"]),
        filled_qty=_dec(raw["filled_qty"]),
        avg_fill_price=_dec(raw["avg_fill_price"]),
    )


def _dec(raw: object) -> Decimal | None:
    return None if raw is None else Decimal(str(raw))
