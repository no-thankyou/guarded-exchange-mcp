"""Exchange keys: add, replace, delete, re-check permissions, and the pre-trade check (SPEC FR-29,
FR-30, FR-31, FR-49; PLAN D-9, D-10, D-12, D-24, ADR-009).

Keys arrive only through the service channel (CLI), never through MCP (FR-30). The exchange is
asked about the key's permissions **outside** any database transaction (D-10); the decision and
the journal record are then written in one write transaction. Journal payloads carry ids,
permission names and verdicts, never the key or its secret (FR-31).

Every server instance talks to one exchange environment (the configured base URL), so the use
cases are built for one ``exchange`` and ``environment``; a user on another environment cannot
store or use a key here.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from guarded_exchange.application.policy import ADMIN_ACTOR, USER_ACTOR
from guarded_exchange.application.ports import (
    Clock,
    CredentialCipher,
    IdGenerator,
    KeyPermissionsReader,
    UnitOfWork,
    UnitOfWorkFactory,
)
from guarded_exchange.domain.audit import AuditEvent, AuditEventType, JsonValue
from guarded_exchange.domain.credentials import (
    ApiKeyPair,
    Credential,
    CredentialStatus,
    KeyAssessment,
    KeyPermissions,
    assess_key,
    disallowed_permissions,
    permissions_to_payload,
    require_trading_allowed,
    unverifiable_key,
)
from guarded_exchange.domain.errors import (
    DomainError,
    ExchangeCredentialsInvalid,
    ExchangeUnavailable,
    InvalidParams,
    NotFound,
    PermissionDenied,
)
from guarded_exchange.domain.users import Environment, User, validate_user_name

RECHECK_ACTOR: Final = "system:permissions-recheck"


async def _user_by_name(uow: UnitOfWork, raw_name: str) -> User:
    name = validate_user_name(raw_name)
    user = await uow.users.get_by_name(name)
    if user is None:
        raise NotFound(f"user {name!r} does not exist", details={"field": "user"})
    return user


@dataclass(frozen=True, slots=True)
class _Scope:
    """The exchange environment this server instance talks to."""

    exchange: str
    environment: Environment

    def require_user(self, user: User) -> None:
        if not user.is_active:
            raise PermissionDenied(f"user {user.name!r} is {user.status.value}")
        if user.environment is not self.environment:
            raise PermissionDenied(
                f"user {user.name!r} trades on {user.environment.value}, but this server is configured "
                f"for the {self.environment.value} exchange environment",
                next_step="Run the command against a server configured for the user's environment "
                "(GX_BYBIT_BASE_URL).",
                details={"reason": "environment_mismatch"},
            )


@dataclass(frozen=True, slots=True)
class StoredCredential:
    """Result of add/replace: the new credential, how it was judged, and what it replaced."""

    user: User
    credential: Credential
    assessment: KeyAssessment
    replaced_id: str | None


class _StoreCredential:
    """Shared flow of ``AddCredential`` and ``ReplaceCredential``.

    1. Read: the user exists, is active and on this environment; an active key exists (replace) or
       not (add).
    2. Network, no transaction: ask the exchange for the key's permissions.
    3. Decide (domain): allowlist, read-only, expiry; or the "cannot verify" branch with the admin's
       attestation on the test environment only.
    4. Write transaction: re-check the preconditions under ``lock_user``, soft-delete the old key
       (replace), store the new one encrypted with its binding as AAD, journal it.
    A rejected key is journaled (``credential_rejected``) and nothing is stored.
    """

    _REPLACE: bool = False

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        cipher: CredentialCipher,
        reader: KeyPermissionsReader,
        clock: Clock,
        ids: IdGenerator,
        *,
        exchange: str,
        environment: Environment,
    ) -> None:
        self._uow = uow
        self._cipher = cipher
        self._reader = reader
        self._clock = clock
        self._ids = ids
        self._scope = _Scope(exchange, environment)

    async def __call__(
        self, user_name: str, keys: ApiKeyPair, *, admin_attested: bool = False
    ) -> StoredCredential:
        async with self._uow(write=False) as uow:
            user = await _user_by_name(uow, user_name)
            self._scope.require_user(user)
            self._require_existing(await self._active(uow, user))

        try:
            permissions, assessment = await self._judge(keys, admin_attested=admin_attested)
        except ExchangeCredentialsInvalid as exc:
            await self._journal_rejection(user, exc, admin_attested=admin_attested)
            raise

        async with self._uow(write=True) as uow:
            await uow.lock_user(user.id)
            user = await _user_by_name(uow, user.name)
            self._scope.require_user(user)
            existing = await self._active(uow, user)
            self._require_existing(existing)
            now = self._clock.now()
            if existing is not None:
                await uow.credentials.mark_deleted(existing.id, now)
            credential = Credential(
                id=self._ids.new_id(),
                user_id=user.id,
                exchange=self._scope.exchange,
                environment=self._scope.environment,
                status=CredentialStatus.ACTIVE,
                permissions=permissions,
                permissions_checked_at=None if permissions is None else now,
                admin_attested=permissions is None,
                created_at=now,
            )
            await uow.credentials.add(credential, self._cipher.seal(keys, credential.binding))
            await uow.audit.append(user.id, self._stored_event(credential, assessment, existing), now)
        return StoredCredential(
            user=user,
            credential=credential,
            assessment=assessment,
            replaced_id=None if existing is None else existing.id,
        )

    async def _active(self, uow: UnitOfWork, user: User) -> Credential | None:
        return await uow.credentials.get_active(user.id, self._scope.exchange, self._scope.environment)

    def _require_existing(self, existing: Credential | None) -> None:
        if self._REPLACE and existing is None:
            raise NotFound(
                "there is no active exchange key to replace",
                next_step="Add one with `guarded-exchange keys add`.",
                details={"field": "user"},
            )
        if not self._REPLACE and existing is not None:
            raise InvalidParams(
                "the user already has an active exchange key for this environment",
                next_step="Use `guarded-exchange keys replace` (or `keys delete` first).",
                details={"field": "user"},
            )

    async def _judge(
        self, keys: ApiKeyPair, *, admin_attested: bool
    ) -> tuple[KeyPermissions | None, KeyAssessment]:
        try:
            permissions = await self._reader.get_key_permissions(keys)
        except ExchangeUnavailable:
            # FR-49: cannot verify -> refuse, except the test environment with the admin's word.
            return None, unverifiable_key(self._scope.environment, attested=admin_attested)
        return permissions, assess_key(self._scope.exchange, permissions, self._clock.now())

    async def _journal_rejection(
        self, user: User, error: ExchangeCredentialsInvalid, *, admin_attested: bool
    ) -> None:
        payload: dict[str, JsonValue] = {
            "exchange": self._scope.exchange,
            "environment": self._scope.environment.value,
            "operation": "replace" if self._REPLACE else "add",
            "reason": error.details.get("reason", error.code),
            "admin_attestation_offered": admin_attested,
        }
        if "permissions" in error.details:
            payload["permissions"] = error.details["permissions"].split(",")
        async with self._uow(write=True) as uow:
            await uow.audit.append(
                user.id,
                AuditEvent(AuditEventType.CREDENTIAL_REJECTED, USER_ACTOR, payload),
                self._clock.now(),
            )

    def _stored_event(
        self, credential: Credential, assessment: KeyAssessment, replaced: Credential | None
    ) -> AuditEvent:
        payload: dict[str, JsonValue] = {
            "credential_id": credential.id,
            "exchange": credential.exchange,
            "environment": credential.environment.value,
            "profile": assessment.profile.value,
            "permissions": permissions_to_payload(credential.permissions),
            "admin_attested": credential.admin_attested,
            "warnings": list(assessment.warnings),
        }
        if credential.admin_attested:
            # GP-3: the admin confirmed in the service channel that the key cannot withdraw or transfer.
            payload["attested_by"] = ADMIN_ACTOR
            payload["attestation"] = "no withdrawal or transfer permission"
        if replaced is not None:
            payload["replaced_credential_id"] = replaced.id
        event_type = AuditEventType.CREDENTIAL_REPLACED if self._REPLACE else AuditEventType.CREDENTIAL_ADDED
        actor = ADMIN_ACTOR if credential.admin_attested else USER_ACTOR
        return AuditEvent(event_type, actor, payload)


class AddCredential(_StoreCredential):
    """Store a first key for the user on this exchange environment (FR-30, FR-49)."""

    _REPLACE = False


class ReplaceCredential(_StoreCredential):
    """Swap the active key for a new one; the old one is soft-deleted in the same transaction (D-9).

    Cancelling pending confirmations of the old key is part of the pending machinery (ticket 016/017).
    """

    _REPLACE = True


class DeleteCredential:
    """Soft-delete the user's active key (FR-29, D-9): it is never used for new actions again.

    The encrypted row stays while executing/unknown actions may still refer to it; erasing it is
    part of the pending machinery (ticket 016/017).
    """

    def __init__(
        self, uow: UnitOfWorkFactory, clock: Clock, *, exchange: str, environment: Environment
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._scope = _Scope(exchange, environment)

    async def __call__(self, user_name: str) -> Credential:
        async with self._uow(write=True) as uow:
            user = await _user_by_name(uow, user_name)
            await uow.lock_user(user.id)
            existing = await uow.credentials.get_active(
                user.id, self._scope.exchange, self._scope.environment
            )
            if existing is None:
                raise NotFound(
                    f"user {user.name!r} has no active exchange key on {self._scope.environment.value}",
                    details={"field": "user"},
                )
            now = self._clock.now()
            await uow.credentials.mark_deleted(existing.id, now)
            await uow.audit.append(
                user.id,
                AuditEvent(
                    AuditEventType.CREDENTIAL_DELETED,
                    USER_ACTOR,
                    {
                        "credential_id": existing.id,
                        "exchange": existing.exchange,
                        "environment": existing.environment.value,
                    },
                ),
                now,
            )
        return replace(existing, status=CredentialStatus.DELETED, deleted_at=now)


class ListCredentials:
    """The user's keys, active and deleted, without secrets (for ``keys show``)."""

    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def __call__(self, user_name: str) -> tuple[User, Sequence[Credential]]:
        async with self._uow(write=False) as uow:
            user = await _user_by_name(uow, user_name)
            return user, await uow.credentials.list_for_user(user.id)


class RecheckCredential:
    """Ask the exchange again what a stored key may do and store the answer (FR-49, D-24).

    A change is journaled as ``credential_permissions_changed`` with ``blocked`` set when the new
    permissions leave the allowlist; the pre-trade check then refuses state-changing actions.
    Errors (exchange unavailable, key refused, undecryptable row) propagate unchanged.
    """

    def __init__(
        self, uow: UnitOfWorkFactory, cipher: CredentialCipher, reader: KeyPermissionsReader, clock: Clock
    ) -> None:
        self._uow = uow
        self._cipher = cipher
        self._reader = reader
        self._clock = clock

    async def __call__(self, credential: Credential) -> Credential:
        keys = await _open_keys(self._uow, self._cipher, credential)
        permissions = await self._reader.get_key_permissions(keys)
        async with self._uow(write=True) as uow:
            await uow.lock_user(credential.user_id)
            current = await uow.credentials.get(credential.id)
            if current is None or not current.is_active:
                return current or credential  # deleted or replaced meanwhile: nothing to update
            now = self._clock.now()
            await uow.credentials.update_permissions(current.id, permissions, now)
            if permissions != current.permissions:
                await uow.audit.append(current.user_id, _changed_event(current, permissions), now)
        return replace(current, permissions=permissions, permissions_checked_at=now)


def _changed_event(credential: Credential, permissions: KeyPermissions) -> AuditEvent:
    extra = disallowed_permissions(credential.exchange, permissions)
    return AuditEvent(
        AuditEventType.CREDENTIAL_PERMISSIONS_CHANGED,
        RECHECK_ACTOR,
        {
            "credential_id": credential.id,
            "before": permissions_to_payload(credential.permissions),
            "after": permissions_to_payload(permissions),
            "blocked": bool(extra),
            "disallowed": extra,
        },
    )


async def _open_keys(
    uow_factory: UnitOfWorkFactory, cipher: CredentialCipher, credential: Credential
) -> ApiKeyPair:
    async with uow_factory(write=False) as uow:
        sealed = await uow.credentials.get_sealed(credential.id)
    if sealed is None:
        raise NotFound("the exchange key is gone", details={"field": "credential"})
    return cipher.open(sealed, credential.binding)


@dataclass(frozen=True, slots=True)
class TradingCredential:
    """A key cleared for a state-changing action, decrypted for the exchange adapter only."""

    credential: Credential
    keys: ApiKeyPair


class RequireTradingCredential:
    """The pre-trade key check (hook for ticket 011): call before every state-changing action.

    Permissions last checked over ``RECHECK_INTERVAL`` ago are re-checked first (D-24). A key whose
    permissions grew beyond the allowlist -> ``credentials_permissions_changed``; expired or missing
    -> ``exchange_credentials_invalid``; read-only -> ``permission_denied``. If a stale key cannot be
    re-checked, the action is refused (fail closed), unless the key was accepted on the admin's
    attestation, whose permissions could never be checked in the first place (FR-49, GP-3).
    """

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        cipher: CredentialCipher,
        recheck: RecheckCredential,
        clock: Clock,
        *,
        exchange: str,
        environment: Environment,
    ) -> None:
        self._uow = uow
        self._cipher = cipher
        self._recheck = recheck
        self._clock = clock
        self._scope = _Scope(exchange, environment)

    async def __call__(self, user_id: str) -> TradingCredential:
        async with self._uow(write=False) as uow:
            credential = await uow.credentials.get_active(
                user_id, self._scope.exchange, self._scope.environment
            )
        if credential is None:
            raise ExchangeCredentialsInvalid(
                "no exchange key is configured for this user",
                next_step="Tell the user to add a key with `guarded-exchange keys add`, outside the chat.",
                details={"reason": "no_key"},
            )
        if credential.needs_recheck(self._clock.now()):
            credential = await self._fresh(credential)
        require_trading_allowed(credential, self._clock.now())
        return TradingCredential(credential, await _open_keys(self._uow, self._cipher, credential))

    async def _fresh(self, credential: Credential) -> Credential:
        try:
            return await self._recheck(credential)
        except ExchangeUnavailable as exc:
            if credential.admin_attested:
                return credential
            raise ExchangeUnavailable(
                "the exchange key's permissions could not be re-checked; state-changing actions are "
                "blocked until they are",
                next_step="Retry in a minute; if it keeps failing, tell the user the exchange is down.",
                details={"reason": "permissions_recheck_failed"},
            ) from exc


@dataclass(frozen=True, slots=True)
class RecheckOutcome:
    credential_id: str
    user_id: str
    status: str  # "ok" | "read_only" | "blocked" | "failed"
    reason: str | None = None


class RecheckActiveCredentials:
    """Re-check every active key of this exchange environment, e.g. when the server starts (D-24).

    One failing key does not stop the others; the caller logs the outcomes.
    """

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        recheck: RecheckCredential,
        clock: Clock,
        *,
        exchange: str,
        environment: Environment,
    ) -> None:
        self._uow = uow
        self._recheck = recheck
        self._clock = clock
        self._scope = _Scope(exchange, environment)

    async def __call__(self) -> list[RecheckOutcome]:
        async with self._uow(write=False) as uow:
            active = await uow.credentials.list_active(self._scope.exchange, self._scope.environment)
        outcomes: list[RecheckOutcome] = []
        for credential in active:
            try:
                fresh = await self._recheck(credential)
                if not fresh.read_only:
                    require_trading_allowed(fresh, self._clock.now())
            except DomainError as exc:
                status = "failed" if isinstance(exc, ExchangeUnavailable) else "blocked"
                outcomes.append(RecheckOutcome(credential.id, credential.user_id, status, exc.code))
            else:
                status = "read_only" if fresh.read_only else "ok"
                outcomes.append(RecheckOutcome(credential.id, credential.user_id, status))
        return outcomes
