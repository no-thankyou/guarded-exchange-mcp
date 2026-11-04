"""Engines and the SQL ``UnitOfWorkFactory`` (PLAN D-10).

One unit of work = one connection = one transaction. ``write=True`` begins with
``BEGIN IMMEDIATE``: the database write lock is taken up front (waiting up to ``busy_timeout``
for another writer) instead of being upgraded on the first write, which in WAL mode fails
with ``database is locked`` without waiting when another process committed in between.
"""

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from guarded_exchange.infrastructure.db.dialect import WRITE_OPTION, configure_sqlite, lock_user
from guarded_exchange.infrastructure.db.repositories import (
    SqlAccessTokenRepository,
    SqlAuditLogRepository,
    SqlConfirmationRepository,
    SqlCredentialRepository,
    SqlIdempotencyRepository,
    SqlPolicyRepository,
    SqlUserRepository,
)


def create_sqlite_async_engine(db_path: Path, *, busy_timeout_ms: int) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    configure_sqlite(engine.sync_engine, busy_timeout_ms=busy_timeout_ms)
    return engine


def create_sqlite_sync_engine(db_path: Path, *, busy_timeout_ms: int, read_only: bool = False) -> Engine:
    """Sync engine (stdlib ``sqlite3``) for migrations and, with ``read_only``, start-up checks.

    Read-only opens the file with ``mode=ro``: the check can neither create nor modify it.
    """
    if read_only:
        uri = f"file:{quote(str(db_path.resolve()))}?mode=ro"
        engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    else:
        engine = create_engine(f"sqlite:///{db_path}")
    configure_sqlite(engine, busy_timeout_ms=busy_timeout_ms, read_only=read_only)
    return engine


class SqlUnitOfWork:
    def __init__(self, connection: AsyncConnection, *, write: bool) -> None:
        self._connection = connection
        self._users = SqlUserRepository(connection, writable=write)
        self._audit = SqlAuditLogRepository(connection, writable=write)
        self._policies = SqlPolicyRepository(connection, writable=write)
        self._access_tokens = SqlAccessTokenRepository(connection, writable=write)
        self._credentials = SqlCredentialRepository(connection, writable=write)
        self._idempotency = SqlIdempotencyRepository(connection, writable=write)
        self._confirmations = SqlConfirmationRepository(connection, writable=write)

    @property
    def users(self) -> SqlUserRepository:
        return self._users

    @property
    def audit(self) -> SqlAuditLogRepository:
        return self._audit

    @property
    def policies(self) -> SqlPolicyRepository:
        return self._policies

    @property
    def access_tokens(self) -> SqlAccessTokenRepository:
        return self._access_tokens

    @property
    def credentials(self) -> SqlCredentialRepository:
        return self._credentials

    @property
    def idempotency(self) -> SqlIdempotencyRepository:
        return self._idempotency

    @property
    def confirmations(self) -> SqlConfirmationRepository:
        return self._confirmations

    async def lock_user(self, user_id: str) -> None:
        await lock_user(self._connection, user_id)


class SqlUnitOfWorkFactory:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def __call__(self, *, write: bool) -> AsyncIterator[SqlUnitOfWork]:
        async with self._engine.connect() as connection:
            await connection.execution_options(**{WRITE_OPTION: write})
            async with connection.begin():  # commit on success, rollback on exception
                yield SqlUnitOfWork(connection, write=write)
