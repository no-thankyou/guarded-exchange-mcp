"""Kill switch use cases (SPEC FR-16, FR-36, FR-44; PLAN D-8, D-16).

The agent may only turn its user's switch on (MCP ``activate_kill_switch``); the user and the admin
turn it on and off from the CLI, and the admin's switch only the admin turns off. Every change
takes ``lock_user`` (D-8): a trading decision holds the same lock, so it sees the switch either
before or after the change, never half of it, and anything decided after the change is refused.
A change is journaled in the same transaction; a call that changes nothing writes nothing.
Turning the switch on cancels nothing on the exchange (SPEC 8).
"""

from dataclasses import dataclass, replace

from guarded_exchange.application.policy import ADMIN_ACTOR, USER_ACTOR, active_principal, no_access
from guarded_exchange.application.ports import Clock, UnitOfWork, UnitOfWorkFactory
from guarded_exchange.application.submit_action import AGENT_ACTOR
from guarded_exchange.domain.audit import AuditEvent, AuditEventType, ClientDescription, JsonValue
from guarded_exchange.domain.errors import NotFound
from guarded_exchange.domain.kill_switch import KillSwitch, KillSwitchActor, turned_off, turned_on
from guarded_exchange.domain.users import User, validate_user_name


@dataclass(frozen=True, slots=True)
class KillSwitchChange:
    """The switch before and after one command (``user`` as it is after); ``changed`` is False if the
    switch already was in that state."""

    user: User
    before: KillSwitch
    after: KillSwitch

    @property
    def changed(self) -> bool:
        return self.before is not self.after


class ActivateKillSwitch:
    """The agent turns its own user's kill switch on (MCP, FR-44). It can never turn it off."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def __call__(
        self, user_id: str | None, client: ClientDescription | None = None
    ) -> KillSwitchChange:
        if user_id is None:
            raise no_access()
        async with self._uow(write=True) as uow:
            await uow.lock_user(user_id)
            user = await active_principal(uow, user_id)
            if user is None:
                raise no_access()
            client_payload = None if client is None else client.to_payload()
            return await _switch(
                uow,
                self._clock,
                user,
                after=turned_on(user.kill_switch, KillSwitchActor.USER),
                actor=AGENT_ACTOR,
                extra={"client": client_payload},
            )


class SetKillSwitch:
    """The user or the admin turns a user's kill switch on or off (CLI only, FR-16, FR-44).

    ``by=USER`` cannot turn off a switch the admin turned on (``permission_denied``); the admin
    turning it on over the user's makes it the admin's.
    """

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def __call__(self, user_name: str, *, on: bool, by: KillSwitchActor) -> KillSwitchChange:
        name = validate_user_name(user_name)
        async with self._uow(write=True) as uow:
            found = await uow.users.get_by_name(name)
            if found is None:
                raise NotFound(f"user {name!r} does not exist", details={"field": "user"})
            await uow.lock_user(found.id)
            user = await uow.users.get(found.id) or found  # re-read under the lock
            after = turned_on(user.kill_switch, by) if on else turned_off(user.kill_switch, by)
            actor = ADMIN_ACTOR if by is KillSwitchActor.ADMIN else USER_ACTOR
            return await _switch(uow, self._clock, user, after=after, actor=actor, extra={})


async def _switch(
    uow: UnitOfWork,
    clock: Clock,
    user: User,
    *,
    after: KillSwitch,
    actor: str,
    extra: dict[str, JsonValue],
) -> KillSwitchChange:
    change = KillSwitchChange(user=replace(user, kill_switch=after), before=user.kill_switch, after=after)
    if not change.changed:
        return change
    await uow.users.set_kill_switch(user.id, after)
    event = AuditEventType.KILL_SWITCH_ON if after.is_on else AuditEventType.KILL_SWITCH_OFF
    payload: dict[str, JsonValue] = {"before": change.before.value, "after": after.value, **extra}
    await uow.audit.append(user.id, AuditEvent(event, actor, payload), clock.now())
    return change
