"""What ``set_trading_stop`` and ``set_leverage`` answer, and those answers as stored for repeats
(FR-6, FR-8, FR-26, FR-27; PLAN D-21; ticket 014).

An accepted request is not yet a visible one: ``applied`` only when the position read back shows the
new values, ``requested`` otherwise - never "failed". (``close_position`` answers like ``place_order``.)
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
from guarded_exchange.domain.account import PositionSide
from guarded_exchange.domain.actions import SetLeverage, SetTradingStop
from guarded_exchange.domain.audit import JsonValue
from guarded_exchange.domain.errors import DomainError
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.position_actions import LeveragePlan, StopPlan
from guarded_exchange.domain.risk import RiskDirection


class PositionChangeState(StrEnum):
    APPLIED = "applied"
    """Read back: the position / the symbol shows the new values."""
    REQUESTED = "requested"
    """The exchange accepted the request; the position did not show it (yet)."""


@dataclass(frozen=True, slots=True)
class StopResult:
    action_id: str
    plan: StopPlan
    state: PositionChangeState
    take_profit: Decimal | None
    stop_loss: Decimal | None
    """The position's levels as read after the request; ``None``: none set, or not read."""
    read_back: bool
    source: ResultSource = ResultSource.PLACED

    def observed(self) -> dict[str, JsonValue]:
        return {
            "state": self.state.value,
            "read_back": self.read_back,
            "take_profit": self.take_profit,
            "stop_loss": self.stop_loss,
        }


@dataclass(frozen=True, slots=True)
class LeverageResult:
    action_id: str
    plan: LeveragePlan
    state: PositionChangeState
    leverage: Decimal | None
    """The leverage as read after the request; ``None``: not read."""
    source: ResultSource = ResultSource.PLACED

    def observed(self) -> dict[str, JsonValue]:
        return {"state": self.state.value, "leverage": self.leverage}


PositionChangeResult = StopResult | LeverageResult


def position_answer_payload(answer: PositionChangeResult | DomainError) -> dict[str, JsonValue]:
    if isinstance(answer, DomainError):
        return error_payload(answer)
    if isinstance(answer, StopResult):
        plan, action = answer.plan, answer.plan.action
        return {
            "kind": "stop",
            "action_id": answer.action_id,
            "action": {
                "symbol": action.symbol,
                "category": action.category.value,
                "take_profit": action.take_profit,
                "stop_loss": action.stop_loss,
            },
            "position_side": plan.position_side.value,
            "position_size": plan.position_size,
            "current_take_profit": plan.current_take_profit,
            "current_stop_loss": plan.current_stop_loss,
            "risk": plan.risk.value,
            "read_back": answer.read_back,
            "state": answer.state.value,
            "take_profit": answer.take_profit,
            "stop_loss": answer.stop_loss,
        }
    action_l = answer.plan.action
    return {
        "kind": "leverage",
        "action_id": answer.action_id,
        "action": {
            "symbol": action_l.symbol,
            "category": action_l.category.value,
            "leverage": action_l.leverage,
        },
        "current": answer.plan.current,
        "risk": answer.plan.risk.value,
        "state": answer.state.value,
        "leverage": answer.leverage,
    }


def restore_position_answer(payload: Mapping[str, Any]) -> PositionChangeResult | DomainError:
    """The stored answer as it was given, marked as a replay. A malformed payload raises."""
    kind = payload["kind"]
    if kind == "error":
        return restore_error_payload(payload)
    raw = payload["action"]
    if kind == "stop":
        stop = SetTradingStop(
            str(raw["symbol"]), Category(raw["category"]), _dec(raw["take_profit"]), _dec(raw["stop_loss"])
        )
        plan = StopPlan(
            stop,
            PositionSide(payload["position_side"]),
            Decimal(payload["position_size"]),
            _dec(payload["current_take_profit"]),
            _dec(payload["current_stop_loss"]),
            RiskDirection(payload["risk"]),
        )
        return StopResult(
            str(payload["action_id"]),
            plan,
            PositionChangeState(payload["state"]),
            _dec(payload["take_profit"]),
            _dec(payload["stop_loss"]),
            bool(payload["read_back"]),
            ResultSource.REPLAY,
        )
    if kind != "leverage":
        raise ValueError("unknown stored answer")
    leverage = SetLeverage(str(raw["symbol"]), Category(raw["category"]), Decimal(raw["leverage"]))
    return LeverageResult(
        str(payload["action_id"]),
        LeveragePlan(leverage, _dec(payload["current"]), RiskDirection(payload["risk"])),
        PositionChangeState(payload["state"]),
        _dec(payload["leverage"]),
        ResultSource.REPLAY,
    )


def _dec(raw: object) -> Decimal | None:
    return None if raw is None else Decimal(str(raw))
