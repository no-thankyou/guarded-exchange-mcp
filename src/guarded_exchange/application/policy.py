"""Policy use cases: read it, let the admin set the ceiling or reset the user's own settings, let
the user tighten it, and decide what a principal may call (SPEC FR-11, FR-12, FR-36, FR-44; PLAN
D-23, ADR-012).

``AuthorizeAccess`` is the single place where the profile is checked (FR-11): the MCP layer asks
it which tools to list and whether a call may proceed; it never decides by itself.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Final

from guarded_exchange.application.ports import Clock, UnitOfWork, UnitOfWorkFactory
from guarded_exchange.domain.audit import AuditEvent, AuditEventType
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT, cap_profile
from guarded_exchange.domain.errors import InvalidParams, NotFound, PermissionDenied
from guarded_exchange.domain.policy import (
    NO_OVERRIDES,
    AdminCeiling,
    EffectivePolicy,
    Permission,
    PolicyPatch,
    PolicyRecord,
    UserOverrides,
    apply_patch,
    default_ceiling,
    parse_policy_fields,
    patch_to_payload,
    permitted,
    require_permission,
    settings_to_payload,
    strictest,
    validate_override,
    without_fields,
)
from guarded_exchange.domain.users import User, validate_user_name

ADMIN_ACTOR: Final = "cli:admin"
USER_ACTOR: Final = "cli:user"


@dataclass(frozen=True, slots=True)
class PolicyView:
    """Everything about one user's policy: where each value comes from and what applies."""

    user: User
    ceiling: AdminCeiling
    ceiling_is_default: bool
    overrides: UserOverrides
    effective: EffectivePolicy
    key_read_only: bool = False  # the active exchange key is read-only and caps the profile (FR-49)


async def load_policy_view(uow: UnitOfWork, user: User) -> PolicyView:
    record = await uow.policies.get(user.id)
    stored_ceiling = record.ceiling if record is not None else None
    overrides = record.overrides if record is not None else NO_OVERRIDES
    ceiling = stored_ceiling if stored_ceiling is not None else default_ceiling(user.environment)
    effective = strictest(ceiling, overrides, user.environment)
    key = await uow.credentials.get_active(user.id, EXCHANGE_BYBIT, user.environment)
    return PolicyView(
        user=user,
        ceiling=ceiling,
        ceiling_is_default=stored_ceiling is None,
        overrides=overrides,
        effective=replace(
            effective, profile=cap_profile(effective.profile, key), kill_switch=user.kill_switch
        ),
        key_read_only=key is not None and key.read_only,
    )


async def _user_by_name(uow: UnitOfWork, raw_name: str) -> User:
    name = validate_user_name(raw_name)
    user = await uow.users.get_by_name(name)
    if user is None:
        raise NotFound(f"user {name!r} does not exist", details={"field": "user"})
    return user


async def active_principal(uow: UnitOfWork, user_id: str | None) -> User | None:
    """The calling user if it exists and is active; ``None`` otherwise (callers fail closed)."""
    if user_id is None:
        return None
    user = await uow.users.get(user_id)
    return user if user is not None and user.is_active else None


def no_access() -> PermissionDenied:
    # Same answer for "no principal", "unknown user" and "revoked": nothing to learn from it (FR-33).
    return PermissionDenied(
        "access denied for this caller",
        next_step="Do not retry. Tell the user to check the server's access configuration.",
    )


class ReadPolicy:
    """The policy of the calling user (MCP ``get_my_policy``) or of a named user (CLI)."""

    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def for_principal(self, user_id: str | None) -> PolicyView:
        async with self._uow(write=False) as uow:
            user = await active_principal(uow, user_id)
            if user is None:
                raise no_access()
            return await load_policy_view(uow, user)

    async def by_name(self, user_name: str) -> PolicyView:
        async with self._uow(write=False) as uow:
            return await load_policy_view(uow, await _user_by_name(uow, user_name))


class AuthorizeAccess:
    """FR-11 in one place: what the principal's effective profile allows.

    Unknown, revoked or missing principals get nothing (NFR-1). The policy is read on every
    call, so a change applies to the next ``tools/list`` and ``tools/call`` without reconnecting.
    """

    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def visible(self, user_id: str | None, requirements: Mapping[str, Permission]) -> frozenset[str]:
        """Names from ``requirements`` the principal may use; the rest should not be listed."""
        return permitted(await self._effective(user_id), requirements)

    async def require(self, user_id: str | None, permission: Permission) -> None:
        """Raise ``PermissionDenied`` unless the principal's profile grants ``permission``."""
        policy = await self._effective(user_id)
        if policy is None:
            raise no_access()
        require_permission(policy, permission)

    async def _effective(self, user_id: str | None) -> EffectivePolicy | None:
        async with self._uow(write=False) as uow:
            user = await active_principal(uow, user_id)
            return None if user is None else (await load_policy_view(uow, user)).effective


def _require_changes(change: PolicyPatch) -> None:
    if change.is_empty:
        raise InvalidParams("nothing to change: pass at least one setting", next_step="See --help.")


class SetCeiling:
    """Admin changes a user's ceiling (CLI only, FR-12, FR-44). Raising it is allowed; hard
    bounds (e.g. slippage <= 3%, TTL <= 10 min) still apply. The user's overrides are kept: if the
    new ceiling is stricter than them, the ceiling wins in the effective policy.
    """

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def __call__(self, user_name: str, change: PolicyPatch) -> PolicyView:
        _require_changes(change)
        async with self._uow(write=True) as uow:
            user = await _user_by_name(uow, user_name)
            await uow.lock_user(user.id)
            before = await load_policy_view(uow, user)
            ceiling = apply_patch(before.ceiling, change)
            now = self._clock.now()
            await uow.policies.save(
                user.id, PolicyRecord(ceiling=ceiling, overrides=before.overrides, updated_at=now)
            )
            await uow.audit.append(
                user.id,
                AuditEvent(
                    AuditEventType.POLICY_CEILING_SET,
                    ADMIN_ACTOR,
                    {"changes": patch_to_payload(change), "ceiling": settings_to_payload(ceiling)},
                ),
                now,
            )
            return await load_policy_view(uow, user)


class TightenPolicy:
    """The user tightens their own policy (CLI only, FR-12, FR-44).

    Every changed field must be at least as strict as the current effective value; otherwise
    nothing changes and the error names the fields. So the command can never loosen anything,
    including the user's own earlier settings.
    """

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def __call__(self, user_name: str, change: PolicyPatch) -> PolicyView:
        _require_changes(change)
        async with self._uow(write=True) as uow:
            user = await _user_by_name(uow, user_name)
            if not user.is_active:
                raise PermissionDenied(f"user {user.name!r} is {user.status.value}")
            await uow.lock_user(user.id)
            before = await load_policy_view(uow, user)
            validate_override(before.effective, change)
            overrides = apply_patch(before.overrides, change)
            now = self._clock.now()
            stored_ceiling = None if before.ceiling_is_default else before.ceiling
            await uow.policies.save(
                user.id, PolicyRecord(ceiling=stored_ceiling, overrides=overrides, updated_at=now)
            )
            after = await load_policy_view(uow, user)
            await uow.audit.append(
                user.id,
                AuditEvent(
                    AuditEventType.POLICY_TIGHTENED,
                    USER_ACTOR,
                    {"changes": patch_to_payload(change), "effective": settings_to_payload(after.effective)},
                ),
                now,
            )
            return after


@dataclass(frozen=True, slots=True)
class OverridesReset:
    """What ``ResetOverrides`` did: the policy afterwards and the user settings it removed."""

    view: PolicyView
    reset_fields: tuple[str, ...]  # policy field order; empty: there was nothing to reset


class ResetOverrides:
    """Admin removes a user's own settings (CLI only, FR-12, FR-44): all of them or the named
    fields, which then follow the ceiling again. With nothing to remove it is a no-op that writes
    nothing to the journal.

    The way back from ``policy tighten``, which the user cannot undo. It may make the effective
    policy looser, but never looser than the ceiling the admin set.
    """

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def __call__(self, user_name: str, fields: Iterable[str] | None = None) -> OverridesReset:
        """``fields=None``: every user setting; else only these (unknown names raise ``InvalidParams``)."""
        wanted = None if fields is None else parse_policy_fields(fields)
        if wanted is not None and not wanted:
            raise InvalidParams("no settings named; omit the field list to reset all of them")
        async with self._uow(write=True) as uow:
            user = await _user_by_name(uow, user_name)
            await uow.lock_user(user.id)
            before = await load_policy_view(uow, user)
            own = patch_to_payload(before.overrides)
            reset = tuple(name for name in own if wanted is None or name in wanted)
            if not reset:
                return OverridesReset(view=before, reset_fields=())
            now = self._clock.now()
            stored_ceiling = None if before.ceiling_is_default else before.ceiling
            overrides = without_fields(before.overrides, frozenset(reset))
            await uow.policies.save(
                user.id, PolicyRecord(ceiling=stored_ceiling, overrides=overrides, updated_at=now)
            )
            after = await load_policy_view(uow, user)
            effective_before = settings_to_payload(before.effective)
            effective_after = settings_to_payload(after.effective)
            await uow.audit.append(
                user.id,
                AuditEvent(
                    AuditEventType.POLICY_OVERRIDES_RESET,
                    ADMIN_ACTOR,
                    {
                        "fields": list(reset),
                        "removed": {name: own[name] for name in reset},
                        "effective_before": {name: effective_before[name] for name in reset},
                        "effective_after": {name: effective_after[name] for name in reset},
                    },
                ),
                now,
            )
            return OverridesReset(view=after, reset_fields=reset)
