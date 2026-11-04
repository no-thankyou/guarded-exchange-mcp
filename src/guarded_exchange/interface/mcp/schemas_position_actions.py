"""Output of ``set_trading_stop`` and ``set_leverage`` (FR-3, FR-6, FR-8, FR-27; ticket 014).
``close_position`` answers with ``PlaceOrderOut`` (it is a reduce-only market order)."""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from guarded_exchange.application.order_results import ResultSource
from guarded_exchange.application.position_results import LeverageResult, PositionChangeState, StopResult
from guarded_exchange.interface.mcp.schemas import DecimalStr

_SOURCE: Final = {
    ResultSource.PLACED: "sent",
    ResultSource.RECONNAISSANCE: "reconnaissance",
    ResultSource.REPLAY: "replay",
}
_SOURCE_DESCRIPTION: Final = (
    "sent: this call sent the request. reconnaissance: the request had no reliable answer and the position "
    "was then read. replay: the stored answer of an earlier call with the same idempotency_key; nothing new "
    "was sent."
)
_REQUESTED: Final = (
    "The exchange accepted the change but the position does not show it yet: check get_positions in a few "
    "seconds; do not treat it as failed."
)
_REPLAY: Final = (
    "This is the stored answer of an earlier call with the same idempotency_key; nothing was sent."
)


def _next_step(state: PositionChangeState, source: ResultSource) -> str | None:
    parts = [
        _REQUESTED if state is PositionChangeState.REQUESTED else None,
        _REPLAY if source is ResultSource.REPLAY else None,
    ]
    return " ".join(p for p in parts if p) or None


class SetTradingStopOut(BaseModel):
    """What is known about the stop-loss / take-profit of the position after this call."""

    model_config = ConfigDict(frozen=True)

    status: Literal["applied", "requested"] = Field(
        description="applied: the position shows the new levels. requested: accepted, not visible yet."
    )
    result_source: Literal["sent", "reconnaissance", "replay"] = Field(description=_SOURCE_DESCRIPTION)
    symbol: str
    position_side: Literal["long", "short"]
    position_size: DecimalStr = Field(description="Position size when read, base asset.")
    requested_take_profit: DecimalStr | None = Field(default=None, description="Take-profit price set, USDT.")
    requested_stop_loss: DecimalStr | None = Field(default=None, description="Stop-loss price set, USDT.")
    take_profit: DecimalStr | None = Field(
        default=None, description="The position's take-profit as read back afterwards, USDT; absent if none."
    )
    stop_loss: DecimalStr | None = Field(
        default=None, description="The position's stop-loss as read back afterwards, USDT; absent if none."
    )
    previous_take_profit: DecimalStr | None = Field(default=None, description="Before this call, USDT.")
    previous_stop_loss: DecimalStr | None = Field(default=None, description="Before this call, USDT.")
    next_step: str | None = None

    @classmethod
    def from_domain(cls, result: StopResult) -> "SetTradingStopOut":
        plan = result.plan
        return cls(
            status=result.state.value,
            result_source=_SOURCE[result.source],
            symbol=plan.action.symbol,
            position_side=plan.position_side.value,
            position_size=plan.position_size,
            requested_take_profit=plan.action.take_profit,
            requested_stop_loss=plan.action.stop_loss,
            take_profit=result.take_profit,
            stop_loss=result.stop_loss,
            previous_take_profit=plan.current_take_profit,
            previous_stop_loss=plan.current_stop_loss,
            next_step=_next_step(result.state, result.source),
        )


class SetLeverageOut(BaseModel):
    """What is known about the leverage of the instrument after this call."""

    model_config = ConfigDict(frozen=True)

    status: Literal["applied", "requested"] = Field(
        description="applied: the exchange shows this leverage (also when it already had it). requested: "
        "accepted, not visible yet."
    )
    result_source: Literal["sent", "reconnaissance", "replay"] = Field(description=_SOURCE_DESCRIPTION)
    symbol: str
    leverage: DecimalStr = Field(description="The leverage asked for.")
    previous_leverage: DecimalStr | None = Field(default=None, description="Before this call.")
    risk: Literal["increases", "reduces"] = Field(
        description="increases: higher than before (the kill switch blocks it and the policy's maximum "
        "leverage applies). reduces: the same or lower."
    )
    next_step: str | None = None

    @classmethod
    def from_domain(cls, result: LeverageResult) -> "SetLeverageOut":
        plan = result.plan
        return cls(
            status=result.state.value,
            result_source=_SOURCE[result.source],
            symbol=plan.action.symbol,
            leverage=plan.action.leverage,
            previous_leverage=plan.current,
            risk=plan.risk.value,
            next_step=_next_step(result.state, result.source),
        )
