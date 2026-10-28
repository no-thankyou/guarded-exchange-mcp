"""SQL repositories, unit of work and SQLite settings on a real database file."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Column, MetaData, String, Table, insert, select, text
from sqlalchemy.exc import IntegrityError

from guarded_exchange.application.ports import UnitOfWorkFactory
from guarded_exchange.domain.access_tokens import AccessToken
from guarded_exchange.domain.audit import SYSTEM_CHAIN, AuditEvent
from guarded_exchange.domain.users import UserNameTaken, new_user
from guarded_exchange.infrastructure.db.dialect import WRITE_OPTION, DecimalText, EpochMillis, lock_user
from guarded_exchange.infrastructure.db.repositories import ReadOnlyUnitOfWorkError
from guarded_exchange.infrastructure.db.schema import metadata
from guarded_exchange.infrastructure.db.uow import (
    SqlUnitOfWorkFactory,
    create_sqlite_async_engine,
    create_sqlite_sync_engine,
)
from tests.fakes import FakeClock
from tests.integration.conftest import BUSY_TIMEOUT_MS

AT = datetime(2026, 10, 27, 18, 53, 43, 449000, tzinfo=UTC)


def test_sql_factory_satisfies_the_port(uow: SqlUnitOfWorkFactory) -> None:
    port: UnitOfWorkFactory = uow
    assert port is uow


async def test_user_round_trip(uow: SqlUnitOfWorkFactory) -> None:
    user = new_user("u-1", "local", AT)

    async with uow(write=True) as tx:
        await tx.users.add(user)
    async with uow(write=False) as tx:
        by_id = await tx.users.get("u-1")
        by_name = await tx.users.get_by_name("local")
        missing = await tx.users.get("nope")

    assert by_id == user
    assert by_name == user
    assert missing is None
    assert by_id is not None
    assert by_id.created_at.tzinfo is UTC


async def test_duplicate_user_name_is_rejected_by_the_database(uow: SqlUnitOfWorkFactory) -> None:
    async with uow(write=True) as tx:
        await tx.users.add(new_user("u-1", "local", AT))

    with pytest.raises(UserNameTaken, match="'local' already exists"):
        async with uow(write=True) as tx:
            await tx.users.add(new_user("u-2", "local", AT))
    with pytest.raises(IntegrityError):  # a duplicate id is a programming error, not a domain one
        async with uow(write=True) as tx:
            await tx.users.add(new_user("u-1", "other", AT))


@pytest.mark.req("FR-36")
async def test_audit_append_numbers_each_chain_from_one_and_lists_in_order(uow: SqlUnitOfWorkFactory) -> None:
    async with uow(write=True) as tx:
        first = await tx.audit.append(SYSTEM_CHAIN, AuditEvent("a", "cli", {"price": Decimal("0.10")}), AT)
        other = await tx.audit.append("u-1", AuditEvent("b", "agent"), AT)
        second = await tx.audit.append(SYSTEM_CHAIN, AuditEvent("c", "cli", {"at": AT}), AT)

    async with uow(write=False) as tx:
        system = await tx.audit.list(SYSTEM_CHAIN)
        tail = await tx.audit.list(SYSTEM_CHAIN, after_seq=1)
        limited = await tx.audit.list(SYSTEM_CHAIN, limit=1)

    assert (first.seq, second.seq, other.seq) == (1, 2, 1)
    assert list(system) == [first, second]
    assert list(tail) == [second]
    assert list(limited) == [first]
    assert system[0].payload == {"price": "0.1"}
    assert system[1].payload == {"at": "2026-10-27T18:53:43.449Z"}
    assert system[0].ts == AT


@pytest.mark.req("FR-36")
async def test_rolled_back_unit_of_work_leaves_no_trace(uow: SqlUnitOfWorkFactory) -> None:
    async def write_then_fail() -> None:
        async with uow(write=True) as tx:
            await tx.users.add(new_user("u-1", "local", AT))
            await tx.audit.append(SYSTEM_CHAIN, AuditEvent("user_created", "cli"), AT)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await write_then_fail()

    async with uow(write=True) as tx:
        record = await tx.audit.append(SYSTEM_CHAIN, AuditEvent("next", "cli"), AT)
        assert await tx.users.get("u-1") is None
    assert record.seq == 1  # the head bump was rolled back too: no gap


async def test_read_unit_of_work_refuses_writes(uow: SqlUnitOfWorkFactory) -> None:
    async with uow(write=False) as tx:
        with pytest.raises(ReadOnlyUnitOfWorkError):
            await tx.users.add(new_user("u-1", "local", AT))
        with pytest.raises(ReadOnlyUnitOfWorkError):
            await tx.audit.append(SYSTEM_CHAIN, AuditEvent("x", "cli"), AT)


async def test_now_is_taken_from_the_clock_not_the_database(uow: SqlUnitOfWorkFactory) -> None:
    clock = FakeClock(datetime(2001, 2, 3, 4, 5, 6, 7000, tzinfo=UTC))

    async with uow(write=True) as tx:
        await tx.audit.append(SYSTEM_CHAIN, AuditEvent("x", "cli"), clock.now())
    async with uow(write=False) as tx:
        (record,) = await tx.audit.list(SYSTEM_CHAIN)

    assert record.ts == clock.now()


@pytest.mark.parametrize(
    "value", [Decimal("0.10"), Decimal("123456789012345678901234567890.123456789012345")]
)
async def test_decimal_and_time_survive_a_real_sqlite_round_trip(tmp_path: Path, value: Decimal) -> None:
    # NUMERIC affinity in SQLite would turn these into 64-bit floats; TEXT keeps them exact.
    probe = Table(
        "probe",
        MetaData(),
        Column("id", String, primary_key=True),
        Column("v", DecimalText()),
        Column("at", EpochMillis()),
    )
    engine = create_sqlite_async_engine(tmp_path / "probe.db", busy_timeout_ms=BUSY_TIMEOUT_MS)
    at = AT + timedelta(milliseconds=1)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(probe.metadata.create_all)
            await conn.execute(insert(probe).values(id="x", v=value, at=at))
        async with engine.connect() as conn:
            row = (await conn.execute(select(probe.c.v, probe.c.at))).one()
            raw = (await conn.execute(text("SELECT typeof(v), v, typeof(at) FROM probe"))).one()
    finally:
        await engine.dispose()

    assert row.v == value
    assert str(row.v) == str(value)
    assert row.at == at
    assert tuple(raw) == ("text", str(value), "integer")


def test_connection_pragmas(db_path: Path) -> None:
    engine = create_sqlite_sync_engine(db_path, busy_timeout_ms=1234)
    try:
        with engine.connect() as conn:
            values = {
                name: conn.exec_driver_sql(f"PRAGMA {name}").scalar()
                for name in (
                    "journal_mode",
                    "busy_timeout",
                    "foreign_keys",
                    "synchronous",
                    "recursive_triggers",
                )
            }
    finally:
        engine.dispose()

    assert values == {
        "journal_mode": "wal",
        "busy_timeout": 1234,
        "foreign_keys": 1,
        "synchronous": 1,
        "recursive_triggers": 1,
    }


async def test_write_unit_of_work_takes_the_write_lock_at_begin(
    db_path: Path, uow: SqlUnitOfWorkFactory
) -> None:
    other = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    try:
        async with uow(write=True):
            # Nothing written yet, but BEGIN IMMEDIATE already holds the lock.
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")  # released on commit
        other.execute("ROLLBACK")
    finally:
        other.close()


async def test_read_unit_of_work_does_not_block_writers(db_path: Path, uow: SqlUnitOfWorkFactory) -> None:
    other = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    try:
        async with uow(write=False) as tx:
            await tx.audit.list(SYSTEM_CHAIN)
            other.execute("BEGIN IMMEDIATE")
            other.execute("INSERT INTO audit_heads (chain_id, last_seq) VALUES ('x', 1)")
            other.execute("COMMIT")
    finally:
        other.close()


def test_why_immediate_deferred_read_then_write_fails_without_waiting(db_path: Path) -> None:
    """The failure BEGIN IMMEDIATE prevents: a deferred reader cannot upgrade to a writer after
    another connection committed, and busy_timeout does not help (SQLITE_BUSY_SNAPSHOT)."""
    reader = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    writer = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM audit_heads").fetchone()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO audit_heads (chain_id, last_seq) VALUES ('w', 1)")
        writer.execute("COMMIT")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            reader.execute("INSERT INTO audit_heads (chain_id, last_seq) VALUES ('r', 1)")
    finally:
        reader.close()
        writer.close()


async def test_concurrent_write_units_of_work_in_one_process_serialise(uow: SqlUnitOfWorkFactory) -> None:
    async def append(i: int) -> None:
        async with uow(write=True) as tx:
            await tx.audit.list(SYSTEM_CHAIN)
            await asyncio.sleep(0)
            await tx.audit.append(SYSTEM_CHAIN, AuditEvent("x", "test", {"i": i}), AT)

    await asyncio.gather(*(append(i) for i in range(20)))

    async with uow(write=False) as tx:
        records = await tx.audit.list(SYSTEM_CHAIN)
    assert [r.seq for r in records] == list(range(1, 21))


async def test_lock_user_requires_a_write_unit_of_work(db_path: Path) -> None:
    engine = create_sqlite_async_engine(db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    try:
        async with engine.connect() as conn, conn.begin():
            with pytest.raises(RuntimeError, match="write unit of work"):
                await lock_user(conn, "u-1")
        async with engine.connect() as conn:
            await conn.execution_options(**{WRITE_OPTION: True})
            async with conn.begin():
                await lock_user(conn, "u-1")  # no-op: BEGIN IMMEDIATE already holds the lock
    finally:
        await engine.dispose()


def test_schema_module_matches_the_migrations(db_path: Path) -> None:
    engine = create_sqlite_sync_engine(db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    try:
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), metadata)
    finally:
        engine.dispose()

    assert diff == []


def test_users_constraints_reject_unknown_status(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO users (id, name, status, environment, created_at) "
                "VALUES ('u', 'n', 'superuser', 'testnet', 0)"
            )
    finally:
        conn.close()


async def test_access_token_revoke_reports_whether_it_changed_the_row(uow: SqlUnitOfWorkFactory) -> None:
    async with uow(write=True) as tx:
        await tx.users.add(new_user("u-1", "alice", AT))
        await tx.access_tokens.add(AccessToken("t-1", "u-1", "a" * 64, AT))

    async with uow(write=True) as tx:
        first = await tx.access_tokens.revoke("t-1", AT + timedelta(seconds=1))
    async with uow(write=True) as tx:
        second = await tx.access_tokens.revoke("t-1", AT + timedelta(seconds=2))
        missing = await tx.access_tokens.revoke("nope", AT)
    async with uow(write=False) as tx:
        stored = await tx.access_tokens.get("t-1")

    assert (first, second, missing) == (True, False, False)
    assert stored is not None
    assert stored.revoked_at == AT + timedelta(seconds=1)  # the second call did not move it
