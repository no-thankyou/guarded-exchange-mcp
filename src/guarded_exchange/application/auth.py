"""Who is calling. Local mode has exactly one principal: the local user from configuration."""

from guarded_exchange.application.ports import UnitOfWorkFactory
from guarded_exchange.domain.users import User, validate_user_name


class PrincipalUnavailable(Exception):
    """The configured principal cannot act; the server must not start (fail closed, NFR-1)."""


class ResolveLocalPrincipal:
    """Load the local user named in configuration (PLAN ADR-004: no token in local mode)."""

    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def __call__(self, local_user_name: str) -> User:
        name = validate_user_name(local_user_name)
        async with self._uow(write=False) as uow:
            user = await uow.users.get_by_name(name)
        if user is None:
            raise PrincipalUnavailable(
                f"local user {name!r} does not exist; run `guarded-exchange init` "
                "with the same GX_LOCAL_USER first"
            )
        if not user.is_active:
            raise PrincipalUnavailable(f"local user {name!r} is {user.status.value}; access is revoked")
        return user
