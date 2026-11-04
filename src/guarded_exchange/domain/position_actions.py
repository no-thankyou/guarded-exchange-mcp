"""Stop-loss / take-profit and leverage of a perpetual (SPEC FR-6, FR-8, FR-43; PLAN D-21; ticket 014).

Pure plans. Closing a position is not here: it is a reduce-only market order and uses ``plan_order``.

- **Risk (FR-43).** Setting or moving a stop-loss / take-profit reduces risk (the spec's rule; removing
  a level is not offered). Leverage: a value above the current one (or an unknown current one)
  increases risk, anything else reduces it.
- **Confirming what happened (D-21).** There is no client id for these actions: the position is read
  back, and ``stop_applied`` / the leverage it shows say whether the change is visible.
"""

from dataclasses import dataclass
from decimal import Decimal

from guarded_exchange.domain.account import Position, PositionSide
from guarded_exchange.domain.actions import SetLeverage, SetTradingStop
from guarded_exchange.domain.audit import canonical_decimal
from guarded_exchange.domain.errors import InstrumentRuleViolation, PositionNotFound
from guarded_exchange.domain.instruments import InstrumentRules
from guarded_exchange.domain.money import is_multiple, neighbours
from guarded_exchange.domain.risk import RiskDirection, classify_leverage


def _s(value: Decimal) -> str:
    return canonical_decimal(value)


@dataclass(frozen=True, slots=True)
class StopPlan:
    """The levels that will be sent, and the position they protect (as read)."""

    action: SetTradingStop
    position_side: PositionSide
    position_size: Decimal
    current_take_profit: Decimal | None
    current_stop_loss: Decimal | None
    risk: RiskDirection = RiskDirection.REDUCES


def plan_trading_stop(action: SetTradingStop, rules: InstrumentRules, position: Position | None) -> StopPlan:
    if position is None or position.symbol != action.symbol:
        raise PositionNotFound(
            f"there is no open {action.symbol} position to protect",
            next_step="Check get_positions; a stop-loss / take-profit belongs to an open position.",
            details={"symbol": action.symbol, "reason": "no_position"},
        )
    for field, value in (("take_profit", action.take_profit), ("stop_loss", action.stop_loss)):
        if value is not None and not is_multiple(value, rules.tick_size):
            lower, upper = neighbours(value, rules.tick_size)
            raise InstrumentRuleViolation(
                f"{field} must be a multiple of the tick size {_s(rules.tick_size)}; nearest valid prices: "
                f"{_s(lower)} and {_s(upper)}",
                next_step=f"Use {_s(lower)} or {_s(upper)}.",
                details={"rule": "tick_size", "field": field, "tick_size": _s(rules.tick_size)},
            )
    return StopPlan(action, position.side, position.size, position.take_profit, position.stop_loss)


def stop_applied(plan: StopPlan, position: Position | None) -> bool:
    """The position shows every level this action set (a level it did not touch is not compared)."""
    if position is None:
        return False
    action = plan.action
    return (action.take_profit is None or position.take_profit == action.take_profit) and (
        action.stop_loss is None or position.stop_loss == action.stop_loss
    )


@dataclass(frozen=True, slots=True)
class LeveragePlan:
    action: SetLeverage
    current: Decimal | None
    """The leverage the exchange showed before the change; ``None``: not reported."""
    risk: RiskDirection


def plan_leverage(action: SetLeverage, current: Decimal | None) -> LeveragePlan:
    return LeveragePlan(action, current, classify_leverage(current, action.leverage))
