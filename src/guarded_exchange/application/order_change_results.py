"""What ``cancel_order``, ``amend_order`` and ``cancel_all_orders`` answer, and those answers as stored
for repeats of the call (FR-5, FR-26, FR-27; PLAN D-21; ticket 013).

The exchange applies cancels and amends asynchronously: an accepted request is not yet an applied
one. So every answer says what is **known**: ``cancelled`` / ``amended`` only when the order read
back shows it; ``cancel_requested`` / ``amend_requested`` when the exchange accepted the request but
the order does not show it yet - never "failed".
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from guarded_exchange.application.order_results import (
    ResultSource,
    error_payload,
    restore_error_payload,
)
from guarded_exchange.domain.account import OrderOrigin, OrderSide, OrderStatus
from guarded_exchange.domain.actions import AmendOrder, CancelAllOrders, CancelOrder
from guarded_exchange.domain.audit import JsonValue
from guarded_exchange.domain.errors import DomainError
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.order_changes import AmendPlan, CancelAllPlan, CancelPlan, TouchedOrder
from guarded_exchange.domain.orders import QtyAdjustment
from guarded_exchange.domain.risk import RiskDirection


class ChangeState(StrEnum):
    """What is known about one cancel or amend after this call."""

    CANCELLED = "cancelled"
    """Read back: the order is cancelled (also after a partial fill)."""
    CANCEL_REQUESTED = "cancel_requested"
    """The exchange accepted the cancel; the order did not show it yet (asynchronous)."""
    FILLED = "filled"
    """The order filled completely before the cancel could take effect: nothing is left to cancel."""
    AMENDED = "amended"
    """Read back: the order carries the new qty and price."""
    AMEND_REQUESTED = "amend_requested"
    """The exchange accepted the amend; the order did not show it yet (asynchronous)."""


@dataclass(frozen=True, slots=True)
class CancelResult:
    action_id: str
    plan: CancelPlan
    state: ChangeState
    after: TouchedOrder | None
    """The order as read back after the request; ``None``: not read (or not visible)."""
    source: ResultSource = ResultSource.PLACED


@dataclass(frozen=True, slots=True)
class AmendResult:
    action_id: str
    plan: AmendPlan
    state: ChangeState
    after: TouchedOrder | None
    source: ResultSource = ResultSource.PLACED


class ItemState(StrEnum):
    """One order of a ``cancel_all_orders`` call."""

    CLOSED = "closed"
    """The cancel was accepted and the order is no longer open (cancelled, or filled just before)."""
    CANCEL_REQUESTED = "cancel_requested"
    """The cancel was accepted; the order was still listed as open when read back."""
    NOT_OPEN = "not_open"
    """The exchange said the order was not open any more (filled or cancelled meanwhile)."""
    REJECTED = "rejected"
    """The exchange refused to cancel it; ``code`` says why."""
    UNKNOWN = "unknown"
    """No reliable answer, and it was still listed (or the list could not be read)."""
    NOT_SENT = "not_sent"
    """Not attempted: an earlier cancel of this call failed in a way that stopped the rest."""


@dataclass(frozen=True, slots=True)
class CancelAllItem:
    order: TouchedOrder
    state: ItemState
    code: str | None = None
    """The error code behind ``rejected``, ``unknown`` and the first ``not_sent``."""


@dataclass(frozen=True, slots=True)
class CancelAllResult:
    action_id: str
    plan: CancelAllPlan
    items: tuple[CancelAllItem, ...]
    stopped_by: str | None = None
    """The code of the failure that stopped the remaining cancels (e.g. ``rate_limited``)."""
    source: ResultSource = ResultSource.PLACED

    def count(self, state: ItemState) -> int:
        return sum(1 for item in self.items if item.state is state)


ChangeResult = CancelResult | AmendResult | CancelAllResult


# --- stored answers -------------------------------------------------------------------------------


def change_answer_payload(answer: ChangeResult | DomainError) -> dict[str, JsonValue]:
    """The answer as canonical-JSON-ready data (``Decimal`` stays ``Decimal``)."""
    if isinstance(answer, DomainError):
        return error_payload(answer)
    if isinstance(answer, CancelResult):
        return {
            "kind": "cancel",
            "action_id": answer.action_id,
            "action": _cancel_action(answer.plan.action),
            "order": _touched(answer.plan.order),
            "state": answer.state.value,
            "after": None if answer.after is None else _touched(answer.after),
        }
    if isinstance(answer, AmendResult):
        plan = answer.plan
        return {
            "kind": "amend",
            "action_id": answer.action_id,
            "action": _amend_action(plan.action),
            "order": _touched(plan.order),
            "risk": plan.risk.value,
            "qty": plan.qty,
            "qty_adjustment": None if plan.qty_adjustment is None else plan.qty_adjustment.value,
            "price": plan.price,
            "notional": plan.notional,
            "position_size": plan.position_size,
            "state": answer.state.value,
            "after": None if answer.after is None else _touched(answer.after),
        }
    return {
        "kind": "cancel_all",
        "action_id": answer.action_id,
        "action": {"symbol": answer.plan.action.symbol, "category": answer.plan.action.category.value},
        "has_more": answer.plan.has_more,
        "items": [
            {"order": _touched(item.order), "state": item.state.value, "code": item.code}
            for item in answer.items
        ],
        "stopped_by": answer.stopped_by,
    }


def restore_change_answer(payload: Mapping[str, Any]) -> ChangeResult | DomainError:
    """The stored answer as it was given, marked as a replay. A malformed payload raises."""
    kind = payload["kind"]
    if kind == "error":
        return restore_error_payload(payload)
    after = payload.get("after")
    if kind == "cancel":
        raw = payload["action"]
        cancel = CancelOrder(str(raw["symbol"]), Category(raw["category"]), str(raw["order_id"]))
        return CancelResult(
            action_id=str(payload["action_id"]),
            plan=CancelPlan(cancel, _touched_from(payload["order"])),
            state=ChangeState(payload["state"]),
            after=None if after is None else _touched_from(after),
            source=ResultSource.REPLAY,
        )
    if kind == "amend":
        raw = payload["action"]
        adjustment = payload["qty_adjustment"]
        amend = AmendOrder(
            str(raw["symbol"]),
            Category(raw["category"]),
            str(raw["order_id"]),
            qty=_dec(raw["qty"]),
            price=_dec(raw["price"]),
        )
        plan = AmendPlan(
            action=amend,
            order=_touched_from(payload["order"]),
            risk=RiskDirection(payload["risk"]),
            qty=Decimal(payload["qty"]),
            qty_adjustment=None if adjustment is None else QtyAdjustment(adjustment),
            price=Decimal(payload["price"]),
            notional=Decimal(payload["notional"]),
            position_size=_dec(payload["position_size"]),
        )
        return AmendResult(
            action_id=str(payload["action_id"]),
            plan=plan,
            state=ChangeState(payload["state"]),
            after=None if after is None else _touched_from(after),
            source=ResultSource.REPLAY,
        )
    if kind != "cancel_all":
        raise ValueError("unknown stored answer")
    raw = payload["action"]
    items = tuple(
        CancelAllItem(_touched_from(item["order"]), ItemState(item["state"]), item["code"])
        for item in payload["items"]
    )
    plan_all = CancelAllPlan(
        CancelAllOrders(str(raw["symbol"]), Category(raw["category"])),
        tuple(item.order for item in items),
        has_more=bool(payload["has_more"]),
    )
    return CancelAllResult(
        action_id=str(payload["action_id"]),
        plan=plan_all,
        items=items,
        stopped_by=payload["stopped_by"],
        source=ResultSource.REPLAY,
    )


def _cancel_action(action: CancelOrder) -> dict[str, JsonValue]:
    return {"symbol": action.symbol, "category": action.category.value, "order_id": action.order_id}


def _amend_action(action: AmendOrder) -> dict[str, JsonValue]:
    return {
        "symbol": action.symbol,
        "category": action.category.value,
        "order_id": action.order_id,
        "qty": action.qty,
        "price": action.price,
    }


def _touched(order: TouchedOrder) -> dict[str, JsonValue]:
    return {
        "order_id": order.order_id,
        "symbol": order.symbol,
        "category": order.category.value,
        "side": order.side.value,
        "origin": order.origin.value,
        "status": order.status.value,
        "qty": order.qty,
        "filled_qty": order.filled_qty,
        "price": order.price,
        "reduce_only": order.reduce_only,
    }


def _touched_from(raw: Mapping[str, Any]) -> TouchedOrder:
    return TouchedOrder(
        order_id=str(raw["order_id"]),
        symbol=str(raw["symbol"]),
        category=Category(raw["category"]),
        side=OrderSide(raw["side"]),
        origin=OrderOrigin(raw["origin"]),
        status=OrderStatus(raw["status"]),
        qty=Decimal(raw["qty"]),
        filled_qty=Decimal(raw["filled_qty"]),
        price=_dec(raw["price"]),
        reduce_only=bool(raw["reduce_only"]),
    )


def _dec(raw: object) -> Decimal | None:
    return None if raw is None else Decimal(str(raw))
