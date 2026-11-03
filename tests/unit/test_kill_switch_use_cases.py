"""Kill switch use cases on fakes (ticket 015; SPEC FR-16, FR-36, FR-44; PLAN D-8): the agent turns it
on, the user and the admin turn it on and off from the CLI, each change is journaled under
``lock_user``."""

from typing import Any

import pytest

from guarded_exchange.application.kill_switch import ActivateKillSwitch, SetKillSwitch
from guarded_exchange.application.policy import ReadPolicy
from guarded_exchange.domain.audit import AuditRecord, ClientDescription
from guarded_exchange.domain.errors import NotFound, PermissionDenied
from guarded_exchange.domain.kill_switch import KillSwitch, KillSwitchActor
from guarded_exchange.domain.users import UserStatus
from tests.fakes import FakeClock, InMemoryDatabase

pytestmark = pytest.mark.req("FR-16")

USER, ADMIN = KillSwitchActor.USER, KillSwitchActor.ADMIN
CLIENT = ClientDescription.sanitized("claude-code", "2.1.312")


@pytest.fixture
def db() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def journal(db: InMemoryDatabase, user_id: str) -> list[AuditRecord]:
    return [r for r in db.state.audit.get(user_id, []) if r.event_type.startswith("kill_switch")]


def payload(record: AuditRecord) -> dict[str, Any]:
    return dict(record.payload)


def stored(db: InMemoryDatabase, user_id: str) -> KillSwitch:
    return db.state.users[user_id].kill_switch


# --- the agent (MCP): on only --------------------------------------------------------------------


@pytest.mark.req("FR-44", "FR-36")
async def test_the_agent_turns_its_users_kill_switch_on_under_the_user_lock(
    db: InMemoryDatabase, clock: FakeClock
) -> None:
    user = db.add_user("alice")

    change = await ActivateKillSwitch(db, clock)(user.id, CLIENT)

    assert (change.before, change.after, change.changed) == (KillSwitch.NONE, KillSwitch.USER, True)
    assert stored(db, user.id) is KillSwitch.USER
    [record] = journal(db, user.id)
    assert (record.event_type, record.actor) == ("kill_switch_on", "mcp:agent")
    assert payload(record) == {"before": "none", "after": "user", "client": CLIENT.to_payload()}
    assert db.audit_appends[-1] == (user.id, "kill_switch_on", True)  # D-8: lock_user held


async def test_turning_it_on_again_changes_and_journals_nothing(
    db: InMemoryDatabase, clock: FakeClock
) -> None:
    user = db.add_user("alice")
    activate = ActivateKillSwitch(db, clock)
    await activate(user.id)

    again = await activate(user.id)

    assert (again.after, again.changed) == (KillSwitch.USER, False)
    assert len(journal(db, user.id)) == 1


async def test_the_agent_turning_it_on_keeps_the_admins_switch(
    db: InMemoryDatabase, clock: FakeClock
) -> None:
    user = db.add_user("alice")
    await SetKillSwitch(db, clock)("alice", on=True, by=ADMIN)

    change = await ActivateKillSwitch(db, clock)(user.id)

    assert (change.after, change.changed) == (KillSwitch.ADMIN, False)
    assert stored(db, user.id) is KillSwitch.ADMIN


@pytest.mark.req("NFR-1")
@pytest.mark.parametrize("who", ["unknown", "revoked", "none"])
async def test_no_access_without_an_active_principal(
    db: InMemoryDatabase, clock: FakeClock, who: str
) -> None:
    revoked = db.add_user("bob", status=UserStatus.REVOKED)
    user_id = {"unknown": "user-ghost", "revoked": revoked.id, "none": None}[who]

    with pytest.raises(PermissionDenied):
        await ActivateKillSwitch(db, clock)(user_id)

    assert stored(db, revoked.id) is KillSwitch.NONE
    assert db.all_audit() == []


# --- the user and the admin (CLI) ----------------------------------------------------------------


@pytest.mark.req("FR-36")
async def test_the_user_turns_their_own_switch_on_and_off(db: InMemoryDatabase, clock: FakeClock) -> None:
    user = db.add_user("alice")
    switch = SetKillSwitch(db, clock)

    on = await switch("alice", on=True, by=USER)
    off = await switch("alice", on=False, by=USER)

    assert (on.after, off.before, off.after) == (KillSwitch.USER, KillSwitch.USER, KillSwitch.NONE)
    assert stored(db, user.id) is KillSwitch.NONE
    records = journal(db, user.id)
    assert [(r.event_type, r.actor) for r in records] == [
        ("kill_switch_on", "cli:user"),
        ("kill_switch_off", "cli:user"),
    ]
    assert payload(records[1]) == {"before": "user", "after": "none"}
    assert [a[2] for a in db.audit_appends] == [True, True]  # each change under lock_user


@pytest.mark.req("FR-44")
async def test_the_user_cannot_turn_off_a_kill_switch_the_admin_turned_on(
    db: InMemoryDatabase, clock: FakeClock
) -> None:
    user = db.add_user("alice")
    switch = SetKillSwitch(db, clock)
    await switch("alice", on=True, by=USER)
    await switch("alice", on=True, by=ADMIN)  # the admin takes it over

    with pytest.raises(PermissionDenied) as error:
        await switch("alice", on=False, by=USER)

    assert error.value.details["reason"] == "admin_kill_switch"
    assert stored(db, user.id) is KillSwitch.ADMIN
    assert [payload(r)["after"] for r in journal(db, user.id)] == ["user", "admin"]  # no "off"

    off = await switch("alice", on=False, by=ADMIN)

    assert (off.before, off.after) == (KillSwitch.ADMIN, KillSwitch.NONE)
    assert journal(db, user.id)[-1].actor == "cli:admin"


async def test_the_admin_can_switch_any_user(db: InMemoryDatabase, clock: FakeClock) -> None:
    db.add_user("alice")
    bob = db.add_user("bob")

    await SetKillSwitch(db, clock)("bob", on=True, by=ADMIN)

    assert stored(db, bob.id) is KillSwitch.ADMIN
    assert stored(db, "user-alice") is KillSwitch.NONE


async def test_an_unknown_user_is_not_found(db: InMemoryDatabase, clock: FakeClock) -> None:
    with pytest.raises(NotFound):
        await SetKillSwitch(db, clock)("ghost", on=True, by=ADMIN)


async def test_the_effective_policy_shows_the_kill_switch(db: InMemoryDatabase, clock: FakeClock) -> None:
    user = db.add_user("alice")
    await ActivateKillSwitch(db, clock)(user.id)

    view = await ReadPolicy(db).for_principal(user.id)

    assert view.effective.kill_switch is KillSwitch.USER
