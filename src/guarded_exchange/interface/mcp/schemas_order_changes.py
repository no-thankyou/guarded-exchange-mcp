"""Output of ``cancel_order``, ``amend_order`` and ``cancel_all_orders`` (FR-3, FR-5, FR-21, FR-27;
ticket 013). Separate from ``schemas`` to keep each slice's models together."""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from guarded_exchange.application.order_change_results import (
    AmendResult,
    CancelAllResult,
    CancelResult,
    ChangeState,
    ItemState,
)
from guarded_exchange.application.order_results import ResultSource
from guarded_exchange.domain.order_changes import TouchedOrder
from guarded_exchange.interface.mcp.schemas import DecimalStr

_SOURCE: Final = {
    ResultSource.PLACED: "sent",
    ResultSource.RECONNAISSANCE: "reconnaissance",
    ResultSource.REPLAY: "replay",
}
_SOURCE_DESCRIPTION: Final = (
    "sent: this call sent the request. reconnaissance: the request had no reliable answer and the order "
    "was then read by its id. replay: the stored answer of an earlier call with the same idempotency_key; "
    "nothing new was sent."
)
_ORIGIN: Final = (
    "system: placed through this server (its client id has the gx- prefix; not verified against the "
    "journal). external: placed by a person or another bot - this change affects activity outside this "
    "chat; tell the user."
)
_NEXT: Final = {
    ChangeState.CANCEL_REQUESTED: "The exchange accepted the cancel but the order does not show it yet "
    "(cancels are applied asynchronously): check get_open_orders in a few seconds; do not treat it as "
    "failed.",
    ChangeState.AMEND_REQUESTED: "The exchange accepted the change but the order does not show it yet "
    "(applied asynchronously): check get_open_orders in a few seconds; do not treat it as failed.",
    ChangeState.ORDER_CLOSED: "The order is closed (filled or cancelled) and does not show the change: it "
    "was not applied. Check get_order_history and get_positions; do not amend it again.",
    ChangeState.FILLED: "The order filled completely before the cancel could take effect: there was "
    "nothing left to cancel. Check get_positions.",
}
_REPLAY: Final = (
    "This is the stored answer of an earlier call with the same idempotency_key; nothing was sent."
)


class ChangedOrderOut(BaseModel):
    """An order as this call saw it."""

    model_config = ConfigDict(frozen=True)

    order_id: str = Field(description="Exchange order id.")
    symbol: str
    category: Literal["spot", "linear"]
    side: Literal["buy", "sell"]
    origin: Literal["system", "external"] = Field(description=_ORIGIN)
    status: str = Field(description="Order status as read: new, partially_filled, cancelled, filled, ...")
    qty: DecimalStr = Field(description="Total order quantity, base asset.")
    filled_qty: DecimalStr = Field(description="Executed so far, base asset.")
    price: DecimalStr | None = Field(default=None, description="Limit price, quote currency.")
    reduce_only: bool

    @classmethod
    def of(cls, order: TouchedOrder) -> "ChangedOrderOut":
        return cls(
            order_id=order.order_id,
            symbol=order.symbol,
            category=order.category.value,
            side=order.side.value,
            origin=order.origin.value,
            status=order.status.value,
            qty=order.qty,
            filled_qty=order.filled_qty,
            price=order.price,
            reduce_only=order.reduce_only,
        )


def _next_step(state: ChangeState | None, source: ResultSource) -> str | None:
    parts = [
        _NEXT.get(state) if state is not None else None,
        _REPLAY if source is ResultSource.REPLAY else None,
    ]
    present = [p for p in parts if p]
    return " ".join(present) or None


class CancelOrderOut(BaseModel):
    """What is known about the cancel after this call."""

    model_config = ConfigDict(frozen=True)

    status: Literal["cancelled", "cancel_requested", "filled"] = Field(
        description="cancelled: the order shows it. cancel_requested: accepted, not visible yet. filled: the "
        "order filled before the cancel took effect."
    )
    result_source: Literal["sent", "reconnaissance", "replay"] = Field(description=_SOURCE_DESCRIPTION)
    order: ChangedOrderOut = Field(description="The order before the cancel.")
    after: ChangedOrderOut | None = Field(default=None, description="The order as read back afterwards.")
    next_step: str | None = None

    @classmethod
    def from_domain(cls, result: CancelResult) -> "CancelOrderOut":
        return cls(
            status=result.state.value,
            result_source=_SOURCE[result.source],
            order=ChangedOrderOut.of(result.plan.order),
            after=None if result.after is None else ChangedOrderOut.of(result.after),
            next_step=_next_step(result.state, result.source),
        )


class AmendOrderOut(BaseModel):
    """What is known about the change after this call."""

    model_config = ConfigDict(frozen=True)

    status: Literal["amended", "amend_requested", "order_closed"] = Field(
        description="amended: the order shows the new values. amend_requested: accepted, not visible yet. "
        "order_closed: the order filled or was cancelled before the change applied; it was not changed."
    )
    result_source: Literal["sent", "reconnaissance", "replay"] = Field(description=_SOURCE_DESCRIPTION)
    risk: Literal["increases", "reduces"] = Field(
        description="increases: a larger qty or a worse price (a buy pays more, a sell receives less); the "
        "risk limits applied. reduces: neither."
    )
    qty: DecimalStr = Field(
        description="The order's new total quantity, base asset (rounded down to the step)."
    )
    qty_adjustment: Literal["rounded_down_to_step", "clamped_to_position"] | None = None
    price: DecimalStr = Field(description="The order's new limit price, quote currency.")
    notional_usdt: DecimalStr = Field(description="qty x price: what the limits were checked on.")
    order: ChangedOrderOut = Field(description="The order before the change.")
    after: ChangedOrderOut | None = Field(default=None, description="The order as read back afterwards.")
    next_step: str | None = None

    @classmethod
    def from_domain(cls, result: AmendResult) -> "AmendOrderOut":
        plan = result.plan
        return cls(
            status=result.state.value,
            result_source=_SOURCE[result.source],
            risk=plan.risk.value,
            qty=plan.qty,
            qty_adjustment=None if plan.qty_adjustment is None else plan.qty_adjustment.value,
            price=plan.price,
            notional_usdt=plan.notional,
            order=ChangedOrderOut.of(plan.order),
            after=None if result.after is None else ChangedOrderOut.of(result.after),
            next_step=_next_step(result.state, result.source),
        )


class CancelAllItemOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    order: ChangedOrderOut
    status: Literal["closed", "cancel_requested", "not_open", "rejected", "unknown", "not_sent"] = Field(
        description="closed: no longer open (cancelled, or filled just before). cancel_requested: accepted, "
        "still listed. not_open: already filled or cancelled. rejected: the exchange refused (see code). "
        "unknown: no reliable answer, may or may not be cancelled. not_sent: not attempted."
    )
    code: str | None = Field(default=None, description="Error code behind rejected, unknown or not_sent.")


class CancelAllOut(BaseModel):
    """The plain open orders of one instrument this call cancelled, one by one."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    category: Literal["spot", "linear"]
    result_source: Literal["sent", "reconnaissance", "replay"] = Field(description=_SOURCE_DESCRIPTION)
    orders: list[CancelAllItemOut] = Field(
        description="Every order this call touched (ours and external); conditional orders and a "
        "position's TP/SL are never included."
    )
    external_count: int = Field(description="How many of them were external (placed outside this server).")
    has_more: bool = Field(description="True: more plain open orders remain; call again for the rest.")
    stopped_by: str | None = Field(
        default=None, description="Error code that stopped the remaining cancels (they are not_sent)."
    )
    next_step: str | None = None

    @classmethod
    def from_domain(cls, result: CancelAllResult) -> "CancelAllOut":
        plan = result.plan
        steps = []
        if result.count(ItemState.UNKNOWN):
            steps.append(
                "Some cancels have no reliable answer (unknown): check get_open_orders in a few seconds. "
                "Calling cancel_all_orders again cannot add risk, but it takes a NEW snapshot and also "
                "cancels orders placed after this call, external ones included."
            )
        if result.count(ItemState.NOT_SENT) or result.count(ItemState.CANCEL_REQUESTED) or plan.has_more:
            steps.append(
                "Call cancel_all_orders again (a new idempotency_key, or none) for what is left: a repeat "
                "takes a NEW snapshot, so it also cancels orders placed after this call, external ones "
                "included; check get_open_orders first if that matters to the user."
            )
        if result.source is ResultSource.REPLAY:
            steps.append(_REPLAY)
        return cls(
            symbol=plan.action.symbol,
            category=plan.action.category.value,
            result_source=_SOURCE[result.source],
            orders=[
                CancelAllItemOut(
                    order=ChangedOrderOut.of(item.order),
                    status=item.state.value,
                    code=item.code,
                )
                for item in result.items
            ],
            external_count=plan.external_count,
            has_more=plan.has_more,
            stopped_by=result.stopped_by,
            next_step=" ".join(steps) or None,
        )
