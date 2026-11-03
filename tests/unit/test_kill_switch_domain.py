"""Kill switch rules (ticket 015; SPEC FR-16, FR-43, FR-44, GP-5): who may turn it on and off, and
which actions it blocks. The action kinds of tickets 013/014 arrive with their own risk direction
(FR-43); the rule here is the one their decisions call."""

import pytest

from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.errors import KillSwitchActive, PermissionDenied, restore_error
from guarded_exchange.domain.kill_switch import (
    KillSwitch,
    KillSwitchActor,
    require_allowed,
    turned_off,
    turned_on,
)
from guarded_exchange.domain.risk import RiskDirection

pytestmark = pytest.mark.req("FR-16")

INCREASES, REDUCES = RiskDirection.INCREASES, RiskDirection.REDUCES
ON = (KillSwitch.USER, KillSwitch.ADMIN)

RISK_INCREASING = [
    pytest.param(ActionKind.PLACE_ORDER, id="place_order-not-reduce-only"),
    pytest.param(ActionKind.SET_LEVERAGE, id="leverage-increase"),
    pytest.param(ActionKind.AMEND_ORDER, id="amend-larger-or-worse-price"),
]
RISK_REDUCING = [
    pytest.param(ActionKind.CANCEL_ORDER, id="cancel"),
    pytest.param(ActionKind.CANCEL_ALL_ORDERS, id="cancel-all"),
    pytest.param(ActionKind.PLACE_ORDER, id="reduce-only-order"),
    pytest.param(ActionKind.CLOSE_POSITION, id="close-position"),
    pytest.param(ActionKind.SET_TRADING_STOP, id="sl-tp"),
    pytest.param(ActionKind.SET_LEVERAGE, id="leverage-decrease"),
    pytest.param(ActionKind.AMEND_ORDER, id="amend-smaller-or-better-price"),
]


# --- what it blocks (FR-16, FR-43) ----------------------------------------------------------------


@pytest.mark.req("FR-43")
@pytest.mark.parametrize("state", ON)
@pytest.mark.parametrize("kind", RISK_INCREASING)
def test_under_kill_switch_risk_increasing_actions_are_refused(state: KillSwitch, kind: ActionKind) -> None:
    with pytest.raises(KillSwitchActive) as error:
        require_allowed(state, INCREASES, kind)

    assert error.value.code == "kill_switch_active"
    assert error.value.details == {"kill_switch": state.value, "action": kind.value, "risk": "increases"}
    assert "Do not retry" in (error.value.next_step or "")
    assert "cancel orders" in (error.value.next_step or "")  # the way out that still works


@pytest.mark.req("FR-43")
@pytest.mark.parametrize("state", ON)
@pytest.mark.parametrize("kind", RISK_REDUCING)
def test_under_kill_switch_risk_reducing_actions_are_allowed(state: KillSwitch, kind: ActionKind) -> None:
    require_allowed(state, REDUCES, kind)  # does not raise


@pytest.mark.parametrize("kind", RISK_INCREASING)
def test_without_kill_switch_nothing_is_refused(kind: ActionKind) -> None:
    require_allowed(KillSwitch.NONE, INCREASES, kind)


def test_the_refusal_names_who_can_turn_the_admin_switch_off() -> None:
    with pytest.raises(KillSwitchActive) as error:
        require_allowed(KillSwitch.ADMIN, INCREASES, ActionKind.PLACE_ORDER)

    assert "only the admin can turn" in (error.value.next_step or "")


@pytest.mark.req("FR-26")
def test_a_stored_kill_switch_refusal_is_restored_as_itself() -> None:
    restored = restore_error(
        "kill_switch_active", "on", next_step="wait", details={"kill_switch": "user"}, untrusted_text=None
    )

    assert isinstance(restored, KillSwitchActive)
    assert restored.details == {"kill_switch": "user"}


# --- who turns it on and off (FR-16, FR-44) -------------------------------------------------------


def test_turning_it_on_records_who_did_it_and_the_admins_switch_stays_the_admins() -> None:
    assert turned_on(KillSwitch.NONE, KillSwitchActor.USER) is KillSwitch.USER
    assert turned_on(KillSwitch.USER, KillSwitchActor.USER) is KillSwitch.USER
    assert turned_on(KillSwitch.NONE, KillSwitchActor.ADMIN) is KillSwitch.ADMIN
    assert turned_on(KillSwitch.USER, KillSwitchActor.ADMIN) is KillSwitch.ADMIN  # now only the admin
    assert turned_on(KillSwitch.ADMIN, KillSwitchActor.USER) is KillSwitch.ADMIN  # not downgraded


@pytest.mark.req("FR-44")
def test_the_user_cannot_turn_off_the_admins_kill_switch() -> None:
    with pytest.raises(PermissionDenied) as error:
        turned_off(KillSwitch.ADMIN, KillSwitchActor.USER)

    assert error.value.details["reason"] == "admin_kill_switch"


@pytest.mark.parametrize(
    ("current", "by"),
    [
        (KillSwitch.USER, KillSwitchActor.USER),
        (KillSwitch.NONE, KillSwitchActor.USER),
        (KillSwitch.USER, KillSwitchActor.ADMIN),
        (KillSwitch.ADMIN, KillSwitchActor.ADMIN),
    ],
)
def test_who_may_turn_it_off(current: KillSwitch, by: KillSwitchActor) -> None:
    assert turned_off(current, by) is KillSwitch.NONE
