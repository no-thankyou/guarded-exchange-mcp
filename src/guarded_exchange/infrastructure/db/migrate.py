"""Schema migrations and the start-up readiness check (PLAN ADR-007, D-11).

Migrations run only from ``guarded-exchange init`` under an exclusive file lock, never when the
server starts: MCP clients start several server processes at once. The server only checks
that the schema is exactly at head and the append-only triggers are in place, and refuses to
start otherwise (fail closed).
"""

import fcntl
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text

from guarded_exchange.infrastructure.db.dialect import WRITE_OPTION
from guarded_exchange.infrastructure.db.uow import create_sqlite_sync_engine

MIN_SQLITE_VERSION: Final = (3, 35, 0)  # INSERT/UPDATE ... RETURNING
SCRIPT_LOCATION: Final = "guarded_exchange.infrastructure.db:migrations"

# Each append-only table needs a trigger per event that unconditionally aborts the statement.
_APPEND_ONLY: Final = {"audit_log": ("UPDATE", "DELETE")}


class StorageNotReady(Exception):
    """The database cannot be used by this version of the server; ``str()`` says why and what to do."""


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    return config


def head_revision() -> str:
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if head is None:  # pragma: no cover - the package always ships migrations
        raise RuntimeError("no migrations found")
    return head


def check_sqlite_version(version: tuple[int, int, int] = sqlite3.sqlite_version_info) -> None:
    if version < MIN_SQLITE_VERSION:
        found = ".".join(map(str, version))
        need = ".".join(map(str, MIN_SQLITE_VERSION))
        raise StorageNotReady(
            f"SQLite {found} is too old: {need} or newer is required (RETURNING). "
            "Use a newer Python build or the Docker image."
        )


@contextmanager
def install_lock(db_path: Path) -> Iterator[None]:
    """Exclusive lock for init/migrations, shared by all processes on this host (flock)."""
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = db_path.with_name(db_path.name + ".init.lock")
    with lock_path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # released by the OS if the process dies
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def upgrade_to_head(db_path: Path, *, busy_timeout_ms: int, revision: str = "head") -> str:
    """Create or upgrade the database to ``revision`` (``head`` in production); return it.

    Call under ``install_lock``. All migration steps run in one ``BEGIN IMMEDIATE`` transaction.
    """
    check_sqlite_version()
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not db_path.exists():
        db_path.touch(mode=0o600)  # SQLite gives -wal/-shm the same mode as the database
    engine = create_sqlite_sync_engine(db_path, busy_timeout_ms=busy_timeout_ms)
    try:
        with engine.connect() as raw_connection:
            connection = raw_connection.execution_options(**{WRITE_OPTION: True})
            config = _alembic_config()
            config.attributes["connection"] = connection
            with connection.begin():  # alembic joins this transaction; we commit it
                command.upgrade(config, revision)
                current = _current_revision(connection)
    finally:
        engine.dispose()
    return current or ""


def check_storage(db_path: Path, *, busy_timeout_ms: int) -> str:
    """Raise ``StorageNotReady`` unless the database is usable by this version; return the revision."""
    check_sqlite_version()
    if not db_path.is_file():
        raise StorageNotReady(f"database not found at {db_path}; run `guarded-exchange init` first")
    engine = create_sqlite_sync_engine(db_path, busy_timeout_ms=busy_timeout_ms)
    try:
        with engine.connect() as connection:
            revision = _current_revision(connection)
            _check_revision(revision)
            _check_append_only_triggers(connection)
            connection.rollback()
    finally:
        engine.dispose()
    return revision or ""


def _current_revision(connection: Connection) -> str | None:
    has_table = connection.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'")
    ).first()
    if has_table is None:
        return None
    heads = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(heads) > 1:
        raise StorageNotReady(f"database has several schema heads {sorted(heads)}; it needs repair")
    return heads[0] if heads else None


def _check_revision(revision: str | None) -> None:
    head = head_revision()
    if revision is None:
        raise StorageNotReady("database schema is not initialised; run `guarded-exchange init`")
    if revision == head:
        return
    known = {script.revision for script in ScriptDirectory.from_config(_alembic_config()).walk_revisions()}
    if revision not in known:
        raise StorageNotReady(
            f"database schema revision {revision!r} is unknown to this version (expected {head!r}); "
            "it was probably created by a newer guarded-exchange - upgrade the server"
        )
    raise StorageNotReady(
        f"database schema is at revision {revision!r}, this version needs {head!r}; "
        "run `guarded-exchange init` to migrate"
    )


def _check_append_only_triggers(connection: Connection) -> None:
    rows = connection.execute(text("SELECT tbl_name, sql FROM sqlite_master WHERE type = 'trigger'")).all()
    missing: list[str] = []
    for table, events in _APPEND_ONLY.items():
        bodies = [_normalize_sql(sql) for tbl, sql in rows if tbl == table and sql]
        for event in events:
            pattern = re.compile(
                rf"BEFORE {event} ON \"?{table.upper()}\"?( FOR EACH ROW)? BEGIN SELECT RAISE\(ABORT,"
            )
            if not any(pattern.search(body) for body in bodies):
                missing.append(f"{table} BEFORE {event}")
    if missing:
        raise StorageNotReady(
            "append-only triggers are missing or altered (" + ", ".join(missing) + "); the journal "
            "is not protected, refusing to start (FR-37). Restore the database from backup."
        )


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.upper()).replace("( ", "(")
