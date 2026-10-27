"""Everything that differs between database dialects (PLAN ADR-007, D-10).

- ``DecimalText``: money as exact text (SQLite NUMERIC would round through a 64-bit float).
- ``EpochMillis``: timezone-aware UTC ``datetime`` as INTEGER milliseconds since the epoch.
- SQLite connection setup: WAL, ``busy_timeout``, ``foreign_keys=ON``, ``synchronous=NORMAL``,
  and transaction control taken over from the driver so a write unit of work starts with
  ``BEGIN IMMEDIATE`` and a read one with plain (deferred) ``BEGIN``.
- ``lock_user`` and ``insert_for``: row locking and upsert-capable INSERT per dialect.

Postgres arrives with ticket 028; until then its branches are deliberately not implemented.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import BigInteger, Connection, Dialect, Engine, String, Table, TypeDecorator, event
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncConnection

WRITE_OPTION: Final = "gx_write"
"""Execution option set on a connection before ``begin()``: ``True`` -> ``BEGIN IMMEDIATE``."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MS: Final = timedelta(milliseconds=1)


class DecimalText(TypeDecorator[Decimal]):
    """``Decimal`` stored as its exact string; ``Decimal("0.10")`` reads back as ``Decimal("0.10")``.

    Never compare or sum these columns in SQL: text order is not numeric order.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(f"DecimalText accepts Decimal only, got {type(value).__name__}")
        if not value.is_finite():
            raise ValueError(f"non-finite decimal cannot be stored: {value}")
        return str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


class EpochMillis(TypeDecorator[datetime]):
    """Aware ``datetime`` <-> INTEGER epoch milliseconds, exact in both directions.

    Naive datetimes and sub-millisecond precision are rejected instead of being silently
    shifted or truncated: ``Clock.now()`` already returns whole milliseconds.
    """

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> int | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"EpochMillis accepts datetime only, got {type(value).__name__}")
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be stored; use timezone-aware UTC")
        if value.microsecond % 1000:
            raise ValueError(f"sub-millisecond precision would be lost: {value.isoformat()}")
        return (value - _EPOCH) // _MS

    def process_result_value(self, value: int | None, dialect: Dialect) -> datetime | None:
        return None if value is None else _EPOCH + value * _MS


def configure_sqlite(engine: Engine, *, busy_timeout_ms: int) -> None:
    """Install connection pragmas and explicit BEGIN handling on a (sync or async's sync) engine."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        # Disable the driver's legacy transaction control; SQLAlchemy emits BEGIN below.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")  # first: others may wait
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Connection) -> None:
        write = bool(connection.get_execution_options().get(WRITE_OPTION, False))
        connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")


def insert_for(connection: AsyncConnection | Connection, table: Table) -> sqlite.Insert:
    """INSERT construct of the connection's dialect, with ``on_conflict_do_*`` available."""
    name = connection.dialect.name
    if name == "sqlite":
        return sqlite.insert(table)
    raise NotImplementedError(f"dialect {name!r} is not supported yet (Postgres: ticket 028)")


async def lock_user(connection: AsyncConnection, user_id: str) -> None:
    """Serialise writers acting for one user until the transaction ends.

    SQLite has one writer per database and a write unit of work holds that lock from
    ``BEGIN IMMEDIATE``, so the only job here is to refuse being called from a read unit of
    work, where the guarantee would silently not hold. Postgres will use ``SELECT ... FOR
    UPDATE`` on the user row (ticket 028).
    """
    name = connection.dialect.name
    if name != "sqlite":
        raise NotImplementedError(f"dialect {name!r} is not supported yet (Postgres: ticket 028)")
    sync = connection.sync_connection
    if sync is None or not sync.get_execution_options().get(WRITE_OPTION, False):
        raise RuntimeError(f"lock_user({user_id!r}) needs a write unit of work")
