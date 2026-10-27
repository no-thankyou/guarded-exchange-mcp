"""Engines and the SQL ``UnitOfWorkFactory`` (PLAN D-10).

One unit of work = one connection = one transaction. ``write=True`` begins with
``BEGIN IMMEDIATE``: the database write lock is taken up front (waiting up to ``busy_timeout``
for another writer) instead of being upgraded on the first write, which in WAL mode fails
with ``database is locked`` without waiting when another process committed in between.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from guarded_exchange.infrastructure.db.dialect import WRITE_OPTION, configure_sqlite
from guarded_exchange.infrastructure.db.repositories import SqlAuditLogRepository, SqlUserRepository


def create_sqlite_async_engine(db_path: Path, *, busy_timeout_ms: int) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    configure_sqlite(engine.sync_engine, busy_timeout_ms=busy_timeout_ms)
    return engine


def create_sqlite_sync_engine(db_path: Path, *, busy_timeout_ms: int) -> Engine:
    """Sync engine (stdlib ``sqlite3``) for migrations and start-up checks."""
    engine = create_engine(f"sqlite:///{db_path}")
    configure_sqlite(engine, busy_timeout_ms=busy_timeout_ms)
    return engine


class SqlUnitOfWork:
    def __init__(self, connection: AsyncConnection, *, write: bool) -> None:
        self._users = SqlUserRepository(connection, writable=write)
        self._audit = SqlAuditLogRepository(connection, writable=write)

    @property
    def users(self) -> SqlUserRepository:
        return self._users

    @property
    def audit(self) -> SqlAuditLogRepository:
        return self._audit


class SqlUnitOfWorkFactory:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def __call__(self, *, write: bool) -> AsyncIterator[SqlUnitOfWork]:
        async with self._engine.connect() as connection:
            await connection.execution_options(**{WRITE_OPTION: write})
            async with connection.begin():  # commit on success, rollback on exception
                yield SqlUnitOfWork(connection, write=write)
