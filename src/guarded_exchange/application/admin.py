"""Administrative use cases. For now: initialising a local installation (ticket 005)."""

from dataclasses import dataclass

from guarded_exchange.application.ports import Clock, IdGenerator, MasterKeyStore, UnitOfWorkFactory
from guarded_exchange.domain.audit import SYSTEM_CHAIN, AuditEvent, AuditEventType
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
                await uow.audit.append(
                    SYSTEM_CHAIN,
                    AuditEvent(
                        AuditEventType.USER_CREATED,
                        self.ACTOR,
                        {
                            "user_id": user.id,
                            "name": user.name,
                            "status": user.status.value,
                            "environment": user.environment.value,
                        },
                    ),
                    now,
                )
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
