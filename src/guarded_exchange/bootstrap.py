"""Composition root: the only module that reads configuration and wires adapters to use cases."""

import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Final, NoReturn

import anyio
import httpx2
import structlog

from guarded_exchange.application.admin import (
    CreateUser,
    InitializeInstallation,
    InitResult,
    IssueAccessToken,
    IssuedToken,
    ListUsers,
    MasterKeyMissing,
    RevokeAccessToken,
    RevokedToken,
)
from guarded_exchange.application.auth import (
    AuthenticateBearerToken,
    PrincipalUnavailable,
    ResolveLocalPrincipal,
)
from guarded_exchange.application.credentials import (
    AddCredential,
    DeleteCredential,
    ListCredentials,
    RecheckActiveCredentials,
    RecheckCredential,
    ReplaceCredential,
    StoredCredential,
)
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.policy import (
    AuthorizeAccess,
    OverridesReset,
    PolicyView,
    ReadPolicy,
    ResetOverrides,
    SetCeiling,
    TightenPolicy,
)
from guarded_exchange.application.ports import Authenticator, UnitOfWorkFactory
from guarded_exchange.application.read_market import GetCandles, GetInstrumentRules, GetOrderbook, GetTicker
from guarded_exchange.config import ConfigError, Settings, load_settings
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT, ApiKeyPair, Credential
from guarded_exchange.domain.errors import DomainError
from guarded_exchange.domain.policy import PolicyPatch
from guarded_exchange.domain.users import User
from guarded_exchange.infrastructure.bybit.client import BybitClient, RetryPolicy
from guarded_exchange.infrastructure.bybit.http import make_http_client
from guarded_exchange.infrastructure.clock import SystemClock, UuidGenerator
from guarded_exchange.infrastructure.crypto import AesGcmCredentialCipher, Keyring
from guarded_exchange.infrastructure.db.migrate import (
    StorageNotReady,
    check_storage,
    install_lock,
    upgrade_to_head,
)
from guarded_exchange.infrastructure.db.uow import SqlUnitOfWorkFactory, create_sqlite_async_engine
from guarded_exchange.infrastructure.logging import configure_logging
from guarded_exchange.infrastructure.master_key import FileMasterKeyStore, MasterKeyError
from guarded_exchange.infrastructure.tokens import SecretTokenGenerator
from guarded_exchange.interface.cli.main import (
    AdminCommands,
    InitReport,
    KeyCommands,
    PolicyCommands,
    Transport,
    build_cli,
)
from guarded_exchange.interface.mcp.access import PrincipalProvider, fixed_principal
from guarded_exchange.interface.mcp.guarded_server import GuardedMCPServer
from guarded_exchange.interface.mcp.http import create_http_server, http_app, serve_http
from guarded_exchange.interface.mcp.server import UseCases, create_server, serve_stdio


def _version() -> str:
    try:
        return version("guarded-exchange-mcp")
    except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
        return "0.0.0"


def _bybit_client(settings: Settings, http: httpx2.AsyncClient) -> BybitClient:
    return BybitClient(
        http,
        retry=RetryPolicy(
            max_attempts=settings.bybit_get_max_attempts,
            base_delay_s=settings.bybit_get_backoff_base_s,
        ),
    )


def _exchange_http(settings: Settings, transport: httpx2.AsyncBaseTransport | None) -> httpx2.AsyncClient:
    return make_http_client(
        str(settings.bybit_base_url),
        connect_timeout_s=settings.bybit_connect_timeout_s,
        read_timeout_s=settings.bybit_read_timeout_s,
        transport=transport,
    )


def build_use_cases(settings: Settings, http: httpx2.AsyncClient, uow: UnitOfWorkFactory) -> UseCases:
    exchange = _bybit_client(settings, http)
    rules = InstrumentRulesCache(
        exchange, SystemClock(), ttl=timedelta(seconds=settings.instrument_rules_ttl_s)
    )
    return UseCases(
        get_ticker=GetTicker(exchange),
        get_orderbook=GetOrderbook(exchange),
        get_candles=GetCandles(exchange),
        get_instrument_rules=GetInstrumentRules(rules),
        read_policy=ReadPolicy(uow),
        authorize=AuthorizeAccess(uow),
    )


def public_url(settings: Settings) -> str:
    host = f"[{settings.http_host}]" if ":" in settings.http_host else settings.http_host
    return f"http://{host}:{settings.http_port}"


@asynccontextmanager
async def app_server(
    settings: Settings,
    *,
    uow: UnitOfWorkFactory,
    principal: PrincipalProvider | None = None,
    authenticator: Authenticator | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[GuardedMCPServer]:
    """Build a fully wired server and close its resources on exit.

    Exactly one source of the principal: ``principal`` (local mode: the configured user) or
    ``authenticator`` (remote mode: the bearer token of each HTTP request, ADR-004).
    ``transport`` is a test seam (``httpx2.MockTransport``); production passes ``None``.
    """
    if (principal is None) == (authenticator is None):
        raise ValueError("pass exactly one of principal (stdio) or authenticator (http)")
    async with _exchange_http(settings, transport) as http:
        use_cases = build_use_cases(settings, http, uow)
        if authenticator is not None:
            yield create_http_server(
                use_cases, authenticator, public_url=public_url(settings), version=_version()
            )
        elif principal is not None:
            yield create_server(use_cases, principal=principal, version=_version())


@asynccontextmanager
async def open_storage(settings: Settings) -> AsyncIterator[SqlUnitOfWorkFactory]:
    engine = create_sqlite_async_engine(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
    try:
        yield SqlUnitOfWorkFactory(engine)
    finally:
        await engine.dispose()


def _fail(what: str, exc: BaseException, code: int) -> NoReturn:
    # stdout belongs to the MCP protocol (serve) or to the report (init): errors go to stderr.
    print(f"guarded-exchange: {what}: {exc}", file=sys.stderr)
    raise SystemExit(code) from exc


def _load_settings(env_file: Path | None) -> Settings:
    try:
        return load_settings(env_file)
    except ConfigError as exc:
        _fail("configuration error", exc, 2)


# --- master key and exchange keys -----------------------------------------------------------


def load_keyring(settings: Settings) -> Keyring:
    """The master key (0600, regular file, 32 bytes) as a keyring; ``MasterKeyError`` otherwise."""
    return Keyring.single(FileMasterKeyStore(settings.master_key_file).load())


@dataclass(frozen=True, slots=True)
class CredentialUseCases:
    add: AddCredential
    replace: ReplaceCredential
    delete: DeleteCredential
    list: ListCredentials
    recheck_all: RecheckActiveCredentials


def build_credential_use_cases(
    settings: Settings, uow: UnitOfWorkFactory, http: httpx2.AsyncClient, keyring: Keyring
) -> CredentialUseCases:
    """Key management for the exchange environment of the configured base URL (D-12)."""
    clock, cipher, reader = SystemClock(), AesGcmCredentialCipher(keyring), _bybit_client(settings, http)
    exchange, environment = EXCHANGE_BYBIT, settings.bybit_environment
    recheck = RecheckCredential(uow, cipher, reader, clock)
    return CredentialUseCases(
        add=AddCredential(
            uow, cipher, reader, clock, UuidGenerator(), exchange=exchange, environment=environment
        ),
        replace=ReplaceCredential(
            uow, cipher, reader, clock, UuidGenerator(), exchange=exchange, environment=environment
        ),
        delete=DeleteCredential(uow, clock, exchange=exchange, environment=environment),
        list=ListCredentials(uow),
        recheck_all=RecheckActiveCredentials(uow, recheck, clock, exchange=exchange, environment=environment),
    )


async def recheck_credentials_on_start(
    settings: Settings,
    uow: UnitOfWorkFactory,
    keyring: Keyring,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    """D-24: re-check every active key when the server starts; never stops the server.

    A failure is only logged: state-changing actions re-check stale permissions themselves and
    refuse if that fails (``RequireTradingCredential``).
    """
    log = structlog.get_logger(__name__)
    try:
        async with _exchange_http(settings, transport) as http:
            outcomes = await build_credential_use_cases(settings, uow, http, keyring).recheck_all()
    except Exception as exc:  # a background check must not take the server down
        log.warning("credentials_recheck_failed", error_type=type(exc).__name__)
        return
    for outcome in outcomes:
        level = log.info if outcome.status in {"ok", "read_only"} else log.warning
        level(
            "credential_rechecked",
            credential_id=outcome.credential_id,
            user_id=outcome.user_id,
            status=outcome.status,
            reason=outcome.reason,
        )


# --- init ---------------------------------------------------------------------------------


async def _initialize(settings: Settings) -> InitResult:
    async with open_storage(settings) as uow:
        use_case = InitializeInstallation(
            uow, SystemClock(), UuidGenerator(), FileMasterKeyStore(settings.master_key_file)
        )
        return await use_case(settings.local_user)


def run_init(settings: Settings) -> InitReport:
    """Migrate to head, verify, then create the master key and local user (PLAN D-11).

    The whole sequence holds an exclusive host-wide lock, so parallel ``init`` runs are safe.
    """
    busy = settings.sqlite_busy_timeout_ms
    with install_lock(settings.db_path):
        upgrade_to_head(settings.db_path, busy_timeout_ms=busy)
        revision = check_storage(settings.db_path, busy_timeout_ms=busy)
        result = anyio.run(_initialize, settings)
    return InitReport(
        db_path=settings.db_path,
        schema_revision=revision,
        master_key_file=settings.master_key_file,
        master_key_created=result.master_key_created,
        user_name=result.user.name,
        user_id=result.user.id,
        user_created=result.user_created,
    )


def init(env_file: Path | None = None) -> InitReport:
    settings = _load_settings(env_file)
    configure_logging(settings.log_level)
    try:
        return run_init(settings)
    except (StorageNotReady, MasterKeyMissing, MasterKeyError, DomainError) as exc:
        _fail("init failed", exc, 1)


# --- serve --------------------------------------------------------------------------------


_RECHECK_GRACE_S: Final = 5.0


async def _serve_with_recheck(
    serve_mcp: Callable[[], Awaitable[None]], settings: Settings, uow: UnitOfWorkFactory, keyring: Keyring
) -> None:
    """Serve while the start-up re-check (D-24) runs in the background.

    However the server stops - normally, with an error or cancelled - the re-check gets up to
    ``_RECHECK_GRACE_S`` (shielded) to finish its database work before it is cancelled: cancelling
    an aiosqlite call mid-way leaves its worker thread posting to a closed event loop.
    """
    done = anyio.Event()

    async def recheck() -> None:
        try:
            await recheck_credentials_on_start(settings, uow, keyring)
        finally:
            done.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(recheck)
        try:
            await serve_mcp()
        finally:
            with anyio.CancelScope(deadline=anyio.current_time() + _RECHECK_GRACE_S, shield=True):
                await done.wait()
            tasks.cancel_scope.cancel()


async def _serve_stdio(settings: Settings, keyring: Keyring) -> None:
    async with open_storage(settings) as uow:
        principal = await ResolveLocalPrincipal(uow)(settings.local_user)
        structlog.get_logger(__name__).info("principal_resolved", user_id=principal.id, user=principal.name)
        async with app_server(settings, uow=uow, principal=fixed_principal(principal.id)) as mcp:
            await _serve_with_recheck(lambda: serve_stdio(mcp), settings, uow, keyring)


async def _serve_http(settings: Settings, keyring: Keyring) -> None:
    async with (
        open_storage(settings) as uow,
        app_server(settings, uow=uow, authenticator=AuthenticateBearerToken(uow)) as mcp,
    ):
        app = http_app(
            mcp, allowed_hosts=settings.http_allowed_hosts, allowed_origins=settings.http_allowed_origins
        )
        log = structlog.get_logger(__name__)
        log.info(
            "http_listening", url=f"{public_url(settings)}/mcp", allowed_hosts=settings.http_allowed_hosts
        )
        if settings.http_host not in _LOOPBACK_HOSTS:
            log.warning(
                "http_not_loopback",
                host=settings.http_host,
                hint="plain HTTP: bearer tokens travel in clear text; expose it only behind a "
                "TLS-terminating reverse proxy and list the public host in GX_HTTP_ALLOWED_HOSTS",
            )
        await _serve_with_recheck(
            lambda: serve_http(app, host=settings.http_host, port=settings.http_port), settings, uow, keyring
        )


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def serve(transport: Transport, env_file: Path | None = None) -> None:
    settings = _load_settings(env_file)
    configure_logging(settings.log_level)  # before MCPServer(): it calls logging.basicConfig
    log = structlog.get_logger(__name__)
    log.info(
        "server_starting",
        transport=transport,
        exchange_base_url=str(settings.bybit_base_url),
        db_path=str(settings.db_path),
        version=_version(),
    )
    try:
        # Never migrate here (D-11): clients start several server processes at once.
        check_storage(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
        # The master key is loaded before MCP starts: missing, too open, a symlink or corrupt ->
        # refuse to start (fail closed) instead of failing later on the first key operation.
        keyring = load_keyring(settings)
        anyio.run(_serve_http if transport == "http" else _serve_stdio, settings, keyring)
    except (StorageNotReady, PrincipalUnavailable, DomainError, MasterKeyError) as exc:
        log.error("startup_refused", reason=str(exc))
        _fail("cannot start", exc, 2)


# --- policy -------------------------------------------------------------------------------


def _with_storage[T](
    env_file: Path | None, action: Callable[[UnitOfWorkFactory, Settings], Awaitable[T]]
) -> T:
    """Run one storage action from the CLI: checked schema, errors to stderr with exit code 1."""
    settings = _load_settings(env_file)
    configure_logging(settings.log_level)

    async def main() -> T:
        async with open_storage(settings) as uow:
            return await action(uow, settings)

    try:
        check_storage(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
        return anyio.run(main)
    except DomainError as exc:
        hint = f" ({exc.next_step})" if exc.next_step else ""
        _fail("refused", RuntimeError(f"{exc.message}{hint}"), 1)
    except StorageNotReady as exc:
        _fail("cannot open storage", exc, 1)
    except MasterKeyError as exc:
        _fail("cannot use the master key", exc, 1)
    except Exception as exc:  # anything else (e.g. a corrupt policy row): one clear line, no traceback
        structlog.get_logger(__name__).error("cli_command_failed", error_type=type(exc).__name__)
        _fail("unexpected error", RuntimeError(f"{type(exc).__name__}: {exc}"), 1)


def show_policy(env_file: Path | None, user: str | None) -> PolicyView:
    return _with_storage(env_file, lambda uow, s: ReadPolicy(uow).by_name(user or s.local_user))


def tighten_policy(env_file: Path | None, change: PolicyPatch) -> PolicyView:
    return _with_storage(env_file, lambda uow, s: TightenPolicy(uow, SystemClock())(s.local_user, change))


def set_ceiling(env_file: Path | None, user: str | None, change: PolicyPatch) -> PolicyView:
    return _with_storage(
        env_file, lambda uow, s: SetCeiling(uow, SystemClock())(user or s.local_user, change)
    )


def reset_overrides(env_file: Path | None, user: str, fields: frozenset[str] | None) -> OverridesReset:
    return _with_storage(env_file, lambda uow, s: ResetOverrides(uow, SystemClock())(user, fields))


# --- users and access tokens (remote mode) ------------------------------------------------


def create_user(env_file: Path | None, name: str) -> User:
    return _with_storage(env_file, lambda uow, s: CreateUser(uow, SystemClock(), UuidGenerator())(name))


def list_users(env_file: Path | None) -> Sequence[User]:
    return _with_storage(env_file, lambda uow, s: ListUsers(uow)())


def issue_token(env_file: Path | None, user: str) -> IssuedToken:
    return _with_storage(
        env_file,
        lambda uow, s: IssueAccessToken(uow, SystemClock(), UuidGenerator(), SecretTokenGenerator())(user),
    )


def revoke_token(env_file: Path | None, token_id: str) -> RevokedToken:
    return _with_storage(env_file, lambda uow, s: RevokeAccessToken(uow, SystemClock())(token_id))


# --- exchange keys ------------------------------------------------------------------------


def _with_credentials[T](
    env_file: Path | None,
    action: Callable[[CredentialUseCases, Settings], Awaitable[T]],
    transport: httpx2.AsyncBaseTransport | None,
) -> T:
    async def run(uow: UnitOfWorkFactory, settings: Settings) -> T:
        keyring = load_keyring(settings)
        async with _exchange_http(settings, transport) as http:
            return await action(build_credential_use_cases(settings, uow, http, keyring), settings)

    return _with_storage(env_file, run)


def add_key(
    env_file: Path | None,
    user: str | None,
    keys: ApiKeyPair,
    admin_attest: bool,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> StoredCredential:
    """``transport`` is a test seam (``httpx2.MockTransport``); the CLI passes ``None``."""
    return _with_credentials(
        env_file, lambda c, s: c.add(user or s.local_user, keys, admin_attested=admin_attest), transport
    )


def replace_key(
    env_file: Path | None,
    user: str | None,
    keys: ApiKeyPair,
    admin_attest: bool,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> StoredCredential:
    return _with_credentials(
        env_file, lambda c, s: c.replace(user or s.local_user, keys, admin_attested=admin_attest), transport
    )


def delete_key(env_file: Path | None, user: str | None) -> Credential:
    """Needs neither the master key nor the exchange: deleting must work even with a broken key."""
    return _with_storage(
        env_file,
        lambda uow, s: DeleteCredential(
            uow, SystemClock(), exchange=EXCHANGE_BYBIT, environment=s.bybit_environment
        )(user or s.local_user),
    )


def show_keys(env_file: Path | None, user: str | None) -> tuple[User, Sequence[Credential]]:
    return _with_storage(env_file, lambda uow, s: ListCredentials(uow)(user or s.local_user))


def cli() -> None:
    build_cli(
        serve,
        init,
        PolicyCommands(
            show=show_policy,
            tighten=tighten_policy,
            set_ceiling=set_ceiling,
            reset_overrides=reset_overrides,
        ),
        AdminCommands(
            create_user=create_user, list_users=list_users, issue_token=issue_token, revoke_token=revoke_token
        ),
        KeyCommands(add=add_key, replace=replace_key, delete=delete_key, show=show_keys),
    )()
