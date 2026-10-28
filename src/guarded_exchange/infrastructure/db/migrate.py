"""Schema migrations and the start-up readiness check (PLAN ADR-007, D-11).

Migrations run only from ``guarded-exchange init`` under an exclusive file lock, never when the
server starts: MCP clients start several server processes at once. The server only checks
that the schema is exactly at head and the append-only triggers are in place, and refuses to
start otherwise (fail closed).
"""

import fcntl
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.exc import DatabaseError

from guarded_exchange.infrastructure.db.dialect import WRITE_OPTION
from guarded_exchange.infrastructure.db.triggers import APPEND_ONLY_TRIGGERS, normalize_sql
from guarded_exchange.infrastructure.db.uow import create_sqlite_sync_engine

MIN_SQLITE_VERSION: Final = (3, 35, 0)  # INSERT/UPDATE ... RETURNING
SCRIPT_LOCATION: Final = "guarded_exchange.infrastructure.db:migrations"


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
    A database at a revision this version does not know (a newer release) is never touched.
    """
    check_sqlite_version()
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not db_path.exists():
        db_path.touch(mode=0o600)  # SQLite gives -wal/-shm the same mode as the database
    engine = create_sqlite_sync_engine(db_path, busy_timeout_ms=busy_timeout_ms)
    try:
        with _storage_errors(db_path), engine.connect() as raw_connection:
            connection = raw_connection.execution_options(**{WRITE_OPTION: True})
            config = _alembic_config()
            config.attributes["connection"] = connection
            with connection.begin():  # alembic joins this transaction; we commit it
                before = _current_revision(connection)
                if before is not None and before not in _known_revisions():
                    raise StorageNotReady(_unknown_revision_message(before))
                command.upgrade(config, revision)
                current = _current_revision(connection)
    finally:
        engine.dispose()
    return current or ""


def check_storage(db_path: Path, *, busy_timeout_ms: int) -> str:
    """Raise ``StorageNotReady`` unless the database is usable by this version; return the revision.

    Opens the file read-only: the check never creates, migrates or otherwise writes to it.
    """
    check_sqlite_version()
    if not db_path.is_file():
        raise StorageNotReady(f"database not found at {db_path}; run `guarded-exchange init` first")
    engine = create_sqlite_sync_engine(db_path, busy_timeout_ms=busy_timeout_ms, read_only=True)
    try:
        with _storage_errors(db_path), engine.connect() as connection:
            revision = _current_revision(connection)
            _check_revision(revision)
            _check_append_only_triggers(connection)
            connection.rollback()
    finally:
        engine.dispose()
    return revision or ""


@contextmanager
def _storage_errors(db_path: Path) -> Iterator[None]:
    """A corrupt, foreign or unreadable file becomes a clear refusal instead of a traceback."""
    try:
        yield
    except DatabaseError as exc:
        reason = str(exc.orig) if exc.orig is not None else type(exc).__name__
        raise StorageNotReady(
            f"cannot use the database at {db_path}: {reason}. It may be corrupt or not a "
            "guarded-exchange database; restore it from backup or point GX_DB_PATH elsewhere"
        ) from exc


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


def _known_revisions() -> set[str]:
    return {script.revision for script in ScriptDirectory.from_config(_alembic_config()).walk_revisions()}


def _unknown_revision_message(revision: str) -> str:
    return (
        f"database schema revision {revision!r} is unknown to this version (expected {head_revision()!r}); "
        "it was probably created by a newer guarded-exchange - upgrade the server"
    )


def _check_revision(revision: str | None) -> None:
    head = head_revision()
    if revision is None:
        raise StorageNotReady("database schema is not initialised; run `guarded-exchange init`")
    if revision == head:
        return
    if revision not in _known_revisions():
        raise StorageNotReady(_unknown_revision_message(revision))
    raise StorageNotReady(
        f"database schema is at revision {revision!r}, this version needs {head!r}; "
        "run `guarded-exchange init` to migrate"
    )


def _check_append_only_triggers(connection: Connection) -> None:
    rows = connection.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'audit_log'")
    ).all()
    actual = {name: normalize_sql(sql or "") for name, sql in rows}
    bad = [
        name for name, expected in APPEND_ONLY_TRIGGERS.items() if actual.get(name) != normalize_sql(expected)
    ]
    if bad:
        raise StorageNotReady(
            "append-only triggers on audit_log are missing or altered (" + ", ".join(bad) + "); the "
            "journal is not protected, refusing to start (FR-37). Restore the database from backup."
        )
