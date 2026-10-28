"""Composition root: the only module that reads configuration and wires adapters to use cases."""

import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, NoReturn

import anyio
import httpx2
import structlog
from mcp.server import MCPServer

from guarded_exchange.application.admin import InitializeInstallation, InitResult, MasterKeyMissing
from guarded_exchange.application.auth import PrincipalUnavailable, ResolveLocalPrincipal
from guarded_exchange.application.policy import (
    AuthorizeAccess,
    PolicyView,
    ReadPolicy,
    SetCeiling,
    TightenPolicy,
)
from guarded_exchange.application.ports import UnitOfWorkFactory
from guarded_exchange.application.read_market import GetTicker
from guarded_exchange.config import ConfigError, Settings, load_settings
from guarded_exchange.domain.errors import DomainError
from guarded_exchange.domain.policy import PolicyPatch
from guarded_exchange.infrastructure.bybit.client import BybitClient, RetryPolicy
from guarded_exchange.infrastructure.bybit.http import make_http_client
from guarded_exchange.infrastructure.clock import SystemClock, UuidGenerator
from guarded_exchange.infrastructure.db.migrate import (
    StorageNotReady,
    check_storage,
    install_lock,
    upgrade_to_head,
)
from guarded_exchange.infrastructure.db.uow import SqlUnitOfWorkFactory, create_sqlite_async_engine
from guarded_exchange.infrastructure.logging import configure_logging
from guarded_exchange.infrastructure.master_key import FileMasterKeyStore, MasterKeyError
from guarded_exchange.interface.cli.main import InitReport, PolicyCommands, Transport, build_cli
from guarded_exchange.interface.mcp.access import PrincipalProvider, fixed_principal
from guarded_exchange.interface.mcp.server import UseCases, create_server, serve_stdio


def _version() -> str:
    try:
        return version("guarded-exchange-mcp")
    except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
        return "0.0.0"


def build_use_cases(settings: Settings, http: httpx2.AsyncClient, uow: UnitOfWorkFactory) -> UseCases:
    exchange = BybitClient(
        http,
        retry=RetryPolicy(
            max_attempts=settings.bybit_get_max_attempts,
            base_delay_s=settings.bybit_get_backoff_base_s,
        ),
    )
    return UseCases(
        get_ticker=GetTicker(exchange), read_policy=ReadPolicy(uow), authorize=AuthorizeAccess(uow)
    )


@asynccontextmanager
async def app_server(
    settings: Settings,
    *,
    uow: UnitOfWorkFactory,
    principal: PrincipalProvider,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[MCPServer[Any]]:
    """Build a fully wired server and close its resources on exit.

    ``transport`` is a test seam (``httpx2.MockTransport``); production passes ``None``.
    """
    async with make_http_client(
        str(settings.bybit_base_url),
        connect_timeout_s=settings.bybit_connect_timeout_s,
        read_timeout_s=settings.bybit_read_timeout_s,
        transport=transport,
    ) as http:
        yield create_server(build_use_cases(settings, http, uow), principal=principal, version=_version())


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


async def _serve_stdio(settings: Settings) -> None:
    async with open_storage(settings) as uow:
        principal = await ResolveLocalPrincipal(uow)(settings.local_user)
        structlog.get_logger(__name__).info("principal_resolved", user_id=principal.id, user=principal.name)
        async with app_server(settings, uow=uow, principal=fixed_principal(principal.id)) as mcp:
            await serve_stdio(mcp)


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
        anyio.run(_serve_stdio, settings)
    except (StorageNotReady, PrincipalUnavailable, DomainError) as exc:
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


def show_policy(env_file: Path | None, user: str | None) -> PolicyView:
    return _with_storage(env_file, lambda uow, s: ReadPolicy(uow).by_name(user or s.local_user))


def tighten_policy(env_file: Path | None, change: PolicyPatch) -> PolicyView:
    return _with_storage(env_file, lambda uow, s: TightenPolicy(uow, SystemClock())(s.local_user, change))


def set_ceiling(env_file: Path | None, user: str | None, change: PolicyPatch) -> PolicyView:
    return _with_storage(
        env_file, lambda uow, s: SetCeiling(uow, SystemClock())(user or s.local_user, change)
    )


def cli() -> None:
    build_cli(
        serve, init, PolicyCommands(show=show_policy, tighten=tighten_policy, set_ceiling=set_ceiling)
    )()
