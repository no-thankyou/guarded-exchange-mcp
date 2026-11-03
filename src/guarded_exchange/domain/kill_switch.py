"""Kill switch: who may turn it on and off, and what it blocks (SPEC FR-16, FR-43, FR-44; G-1, GP-5).

The user (for themselves) and the admin (for any user) turn it on; while it is on, every action
that **increases risk** is refused, and reading and risk-reducing actions keep working (cancel,
reduce-only, closing, SL/TP, lowering leverage). It cancels nothing on the exchange by itself
(SPEC 8). The agent can only turn it on (MCP); turning it off is a service-channel (CLI) action,
and a kill switch the admin turned on only the admin can turn off.

One state per user (PLAN 5.1: ``none | user | admin``): the admin turning it on over the user's
makes it the admin's, so the user cannot turn off what the admin wants on.
"""

from enum import StrEnum

from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.errors import KillSwitchActive, PermissionDenied
from guarded_exchange.domain.risk import RiskDirection


class KillSwitch(StrEnum):
    """The stored state: off, or on and by whom."""

    NONE = "none"
    USER = "user"
    ADMIN = "admin"

    @property
    def is_on(self) -> bool:
        return self is not KillSwitch.NONE


class KillSwitchActor(StrEnum):
    """Who flips the switch. The agent acts as the user, and only ever turns it on."""

    USER = "user"
    ADMIN = "admin"


def turned_on(current: KillSwitch, by: KillSwitchActor) -> KillSwitch:
    """The state after ``by`` turns the switch on. The admin's switch stays the admin's."""
    if by is KillSwitchActor.ADMIN or current is KillSwitch.ADMIN:
        return KillSwitch.ADMIN
    return KillSwitch.USER


def turned_off(current: KillSwitch, by: KillSwitchActor) -> KillSwitch:
    """The state after ``by`` turns the switch off; the user cannot turn off the admin's (FR-16)."""
    if current is KillSwitch.ADMIN and by is not KillSwitchActor.ADMIN:
        raise PermissionDenied(
            "the kill switch was turned on by the admin; only the admin can turn it off",
            next_step="Ask the admin: `guarded-exchange admin kill-switch off --user <name>`.",
            details={"kill_switch": current.value, "reason": "admin_kill_switch"},
        )
    return KillSwitch.NONE


def require_allowed(state: KillSwitch, risk: RiskDirection, kind: ActionKind) -> None:
    """Raise ``KillSwitchActive`` if the switch is on and the action increases risk (FR-16).

    The caller passes the action's risk direction (FR-43): a new order that is not reduce-only
    (any spot order, GP-5), an amend to a larger size or a worse price and a higher leverage
    increase it; cancelling, reduce-only orders, closing, SL/TP and a lower leverage reduce it.
    """
    if not state.is_on or risk is RiskDirection.REDUCES:
        return
    who = "the admin" if state is KillSwitch.ADMIN else "the user"
    raise KillSwitchActive(
        f"the kill switch is on (turned on by {who}): {kind.value} increases risk and is blocked; "
        "nothing was sent",
        next_step="Do not retry or work around it. Risk-reducing actions still work: cancel orders, "
        "reduce-only orders, closing positions, stop-loss / take-profit. Tell the user; only "
        + ("the admin" if state is KillSwitch.ADMIN else "the user")
        + " can turn the kill switch off, outside the chat.",
        details={"kill_switch": state.value, "action": kind.value, "risk": risk.value},
    )
