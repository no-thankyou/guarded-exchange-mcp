"""Who is calling. Local mode has exactly one principal: the local user from configuration;
remote mode: the user a verified bearer token belongs to (PLAN ADR-004)."""

from guarded_exchange.application.ports import UnitOfWorkFactory
from guarded_exchange.domain.access_tokens import hash_token, is_well_formed, token_matches
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


class AuthenticateBearerToken:
    """``Authenticator`` for opaque bearer tokens (PLAN ADR-004), consulted on every HTTP request.

    Accepts only a well-formed token whose hash is stored, not revoked, and whose user is
    active. Every other case is the same ``None``: the caller learns nothing about why.
    """

    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def authenticate(self, credential: str) -> str | None:
        if not is_well_formed(credential):
            return None
        async with self._uow(write=False) as uow:
            token = await uow.access_tokens.get_by_hash(hash_token(credential))
            if token is None or token.is_revoked or not token_matches(token, credential):
                return None
            user = await uow.users.get(token.user_id)
        return user.id if user is not None and user.is_active else None
