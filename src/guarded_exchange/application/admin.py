"""Administrative use cases (CLI only, never MCP): initialising a local installation (ticket 005),
users and bearer access tokens of the remote mode (ticket 026, PLAN ADR-004)."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from guarded_exchange.application.policy import ADMIN_ACTOR
from guarded_exchange.application.ports import (
    Clock,
    IdGenerator,
    MasterKeyStore,
    TokenGenerator,
    UnitOfWork,
    UnitOfWorkFactory,
)
from guarded_exchange.domain.access_tokens import AccessToken, hash_token, is_well_formed
from guarded_exchange.domain.audit import SYSTEM_CHAIN, AuditEvent, AuditEventType
from guarded_exchange.domain.errors import InvalidParams, NotFound, PermissionDenied
from guarded_exchange.domain.users import User, new_user, validate_user_name


class MasterKeyMissing(Exception):
    """The installation is initialised but its master key is gone.

    Never silently replace it: a new key would make every stored secret undecryptable and the
    journal unverifiable.
    """


@dataclass(frozen=True, slots=True)
class InitResult:
    user: User
    user_created: bool
    master_key_created: bool
    first_init: bool


class InitializeInstallation:
    """Create the master key and the local user once; re-running changes nothing (idempotent).

    The schema must already be migrated. All database changes and their journal records are
    written in one write transaction, so a crash leaves either everything or nothing. An
    installation counts as initialised once the ``system`` chain has its first record.
    """

    ACTOR = "cli:init"

    def __init__(
        self, uow: UnitOfWorkFactory, clock: Clock, ids: IdGenerator, master_key: MasterKeyStore
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._master_key = master_key

    async def __call__(self, local_user_name: str) -> InitResult:
        name = validate_user_name(local_user_name)
        async with self._uow(write=True) as uow:
            first_init = not await uow.audit.list(SYSTEM_CHAIN, limit=1)
            key_created = self._ensure_master_key(first_init=first_init)
            now = self._clock.now()
            if first_init:
                await uow.audit.append(
                    SYSTEM_CHAIN,
                    AuditEvent(
                        AuditEventType.INSTALLATION_INITIALIZED,
                        self.ACTOR,
                        {"local_user": name, "master_key_created": key_created},
                    ),
                    now,
                )
            user = await uow.users.get_by_name(name)
            user_created = user is None
            if user is None:
                user = new_user(self._ids.new_id(), name, now)
                await uow.users.add(user)
                await uow.audit.append(SYSTEM_CHAIN, _user_created_event(user, self.ACTOR), now)
        return InitResult(
            user=user, user_created=user_created, master_key_created=key_created, first_init=first_init
        )

    def _ensure_master_key(self, *, first_init: bool) -> bool:
        if self._master_key.exists():
            return False
        if not first_init:
            raise MasterKeyMissing(
                "this installation is already initialised but its master key file is missing; "
                "restore the key from backup (or point GX_MASTER_KEY_FILE at it). A new key "
                "would not decrypt existing secrets, so none is generated."
            )
        self._master_key.create()
        return True


def _user_created_event(user: User, actor: str) -> AuditEvent:
    return AuditEvent(
        AuditEventType.USER_CREATED,
        actor,
        {
            "user_id": user.id,
            "name": user.name,
            "status": user.status.value,
            "environment": user.environment.value,
        },
    )


async def _require_user(uow: UnitOfWork, raw_name: str) -> User:
    name = validate_user_name(raw_name)
    user = await uow.users.get_by_name(name)
    if user is None:
        raise NotFound(f"user {name!r} does not exist", details={"field": "user"})
    return user


class CreateUser:
    """A new user: active, test environment, read-only default policy (FR-29, FR-45)."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, ids: IdGenerator) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids

    async def __call__(self, user_name: str) -> User:
        name = validate_user_name(user_name)
        async with self._uow(write=True) as uow:
            if await uow.users.get_by_name(name) is not None:
                raise InvalidParams(
                    f"user {name!r} already exists",
                    next_step="Pick another name or issue a token for the existing user.",
                    details={"field": "name"},
                )
            now = self._clock.now()
            user = new_user(self._ids.new_id(), name, now)
            await uow.users.add(user)
            await uow.audit.append(SYSTEM_CHAIN, _user_created_event(user, ADMIN_ACTOR), now)
        return user


class ListUsers:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def __call__(self) -> Sequence[User]:
        async with self._uow(write=False) as uow:
            return await uow.users.list()


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """The only object that ever holds the secret: shown to the admin once, never stored."""

    token_id: str
    user: User
    secret: str
    created_at: datetime


class IssueAccessToken:
    """Issue a bearer token for an active user. Only the hash is stored; the journal gets the id."""

    def __init__(
        self, uow: UnitOfWorkFactory, clock: Clock, ids: IdGenerator, tokens: TokenGenerator
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._tokens = tokens

    async def __call__(self, user_name: str) -> IssuedToken:
        secret = self._tokens.new_token()
        if not is_well_formed(secret):  # a broken generator must not hand out unusable tokens
            raise RuntimeError("token generator produced a malformed token")
        async with self._uow(write=True) as uow:
            user = await _require_user(uow, user_name)
            if not user.is_active:
                raise PermissionDenied(f"user {user.name!r} is {user.status.value}; no token is issued")
            now = self._clock.now()
            token = AccessToken(
                id=self._ids.new_id(), user_id=user.id, token_hash=hash_token(secret), created_at=now
            )
            await uow.access_tokens.add(token)
            await uow.audit.append(
                user.id,
                AuditEvent(AuditEventType.ACCESS_TOKEN_ISSUED, ADMIN_ACTOR, {"token_id": token.id}),
                now,
            )
        return IssuedToken(token_id=token.id, user=user, secret=secret, created_at=now)


@dataclass(frozen=True, slots=True)
class RevokedToken:
    token: AccessToken
    already_revoked: bool


class RevokeAccessToken:
    """Revoke one token; the next request with it gets 401 (FR-29). Re-revoking changes nothing.

    Revoking the user (and cancelling their pending confirmations) is ticket 027.
    """

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def __call__(self, token_id: str) -> RevokedToken:
        async with self._uow(write=True) as uow:
            token = await uow.access_tokens.get(token_id)
            if token is None:
                raise NotFound(f"token {token_id!r} does not exist", details={"field": "token_id"})
            if token.is_revoked:
                return RevokedToken(token=token, already_revoked=True)
            now = self._clock.now()
            await uow.access_tokens.revoke(token.id, now)
            await uow.audit.append(
                token.user_id,
                AuditEvent(AuditEventType.ACCESS_TOKEN_REVOKED, ADMIN_ACTOR, {"token_id": token.id}),
                now,
            )
        return RevokedToken(token=replace(token, revoked_at=now), already_revoked=False)
