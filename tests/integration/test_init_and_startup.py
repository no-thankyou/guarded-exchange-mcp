"""``guarded-exchange init`` and the server's start-up checks on real files (D-11, FR-37, NFR-1)."""

import logging
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from guarded_exchange import bootstrap
from guarded_exchange.config import Settings
from guarded_exchange.infrastructure.db.migrate import (
    StorageNotReady,
    check_sqlite_version,
    check_storage,
    upgrade_to_head,
)
from tests.integration.conftest import BUSY_TIMEOUT_MS, make_settings

pytestmark = pytest.mark.req("NFR-1")

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """``serve``/``init`` reconfigure the root logger onto the captured stderr; undo it."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _rows(db: Path, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _env_file(tmp_path: Path, settings: Settings) -> Path:
    env = tmp_path / "gx.env"
    env.write_text(
        f"GX_DB_PATH={settings.db_path}\n"
        f"GX_MASTER_KEY_FILE={settings.master_key_file}\n"
        f"GX_SQLITE_BUSY_TIMEOUT_MS={BUSY_TIMEOUT_MS}\n"
    )
    return env


# --- init ---------------------------------------------------------------------------------


@pytest.mark.req("FR-36", "FR-45")
def test_init_on_an_empty_directory_creates_database_key_and_user(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    report = bootstrap.run_init(settings)

    assert settings.db_path.is_file()
    assert stat.S_IMODE(settings.db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.master_key_file.stat().st_mode) == 0o600
    assert settings.master_key_file.parent != settings.db_path.parent
    assert report.master_key_created
    assert report.user_created
    assert report.schema_revision == check_storage(settings.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    assert _rows(settings.db_path, "SELECT id, name, status, environment FROM users") == [
        (report.user_id, "local", "active", "testnet")
    ]
    assert _rows(settings.db_path, "SELECT chain_id, seq, event_type, actor FROM audit_log ORDER BY seq") == [
        ("system", 1, "installation_initialized", "cli:init"),
        ("system", 2, "user_created", "cli:init"),
    ]


def test_second_init_is_idempotent_and_keeps_the_key(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    first = bootstrap.run_init(settings)
    key_before = settings.master_key_file.read_bytes()
    key_inode = settings.master_key_file.stat().st_ino

    second = bootstrap.run_init(settings)

    assert settings.master_key_file.read_bytes() == key_before
    assert settings.master_key_file.stat().st_ino == key_inode
    assert not second.master_key_created
    assert not second.user_created
    assert second.user_id == first.user_id
    assert _rows(settings.db_path, "SELECT count(*) FROM users") == [(1,)]
    assert _rows(settings.db_path, "SELECT count(*) FROM audit_log") == [(2,)]


def test_init_refuses_to_replace_a_lost_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    settings.master_key_file.unlink()

    with pytest.raises(SystemExit) as exit_info:
        bootstrap.init(_env_file(tmp_path, settings))

    assert exit_info.value.code == 1
    assert "master key file is missing" in capsys.readouterr().err
    assert not settings.master_key_file.exists()


def test_init_upgrades_an_older_schema(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    upgrade_to_head(settings.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, revision="0001")

    report = bootstrap.run_init(settings)

    assert report.schema_revision == "0002"


def test_init_command_in_a_real_process(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    env = _env_file(tmp_path, settings)

    runs = [
        subprocess.run(
            [sys.executable, "-m", "guarded_exchange", "init", "--env-file", str(env)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "PYTHONPATH": ""},
        )
        for _ in range(2)
    ]

    assert [r.returncode for r in runs] == [0, 0], [r.stderr for r in runs]
    assert "(created - back it up)" in runs[0].stdout
    assert "(kept existing)" in runs[1].stdout
    assert "(already existed)" in runs[1].stdout


# --- start-up checks ----------------------------------------------------------------------


def _drop_trigger(db: Path, name: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(f"DROP TRIGGER {name}")
        conn.commit()
    finally:
        conn.close()


def _no_database(settings: Settings) -> None:
    return None


def _empty_database(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True)
    sqlite3.connect(settings.db_path).close()


def _older_revision(settings: Settings) -> None:
    upgrade_to_head(settings.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, revision="0001")


def _unknown_revision(settings: Settings) -> None:
    bootstrap.run_init(settings)
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE alembic_version SET version_num = '9999'")
    conn.commit()
    conn.close()


def _without_update_trigger(settings: Settings) -> None:
    bootstrap.run_init(settings)
    _drop_trigger(settings.db_path, "audit_log_no_update")


def _with_neutered_delete_trigger(settings: Settings) -> None:
    bootstrap.run_init(settings)
    _drop_trigger(settings.db_path, "audit_log_no_delete")
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log WHEN 0 "
        "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END"
    )
    conn.commit()
    conn.close()


BROKEN_STATES = [
    pytest.param(_no_database, "database not found", id="no-database"),
    pytest.param(_empty_database, "schema is not initialised", id="no-migrations"),
    pytest.param(_older_revision, "at revision '0001', this version needs '0002'", id="older-revision"),
    pytest.param(_unknown_revision, "revision '9999' is unknown", id="newer-revision"),
    pytest.param(_without_update_trigger, "audit_log BEFORE UPDATE", id="no-update-trigger"),
    pytest.param(_with_neutered_delete_trigger, "audit_log BEFORE DELETE", id="neutered-trigger"),
]


@pytest.mark.parametrize(("break_storage", "reason"), BROKEN_STATES)
def test_storage_check_explains_why_it_refuses(tmp_path: Path, break_storage: object, reason: str) -> None:
    settings = make_settings(tmp_path)
    break_storage(settings)  # type: ignore[operator]

    with pytest.raises(StorageNotReady, match=reason.replace("(", r"\(")):
        check_storage(settings.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)


@pytest.mark.parametrize(("break_storage", "reason"), BROKEN_STATES)
def test_server_does_not_start_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], break_storage: object, reason: str
) -> None:
    settings = make_settings(tmp_path)
    break_storage(settings)  # type: ignore[operator]

    with pytest.raises(SystemExit) as exit_info:
        bootstrap.serve("stdio", _env_file(tmp_path, settings))

    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "guarded-exchange: cannot start:" in err
    assert reason in err


def test_server_check_never_creates_or_migrates_the_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    _older_revision(settings)

    with pytest.raises(StorageNotReady):
        check_storage(settings.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    with pytest.raises(StorageNotReady):
        check_storage(tmp_path / "missing.db", busy_timeout_ms=BUSY_TIMEOUT_MS)

    assert _rows(settings.db_path, "SELECT version_num FROM alembic_version") == [("0001",)]
    assert not (tmp_path / "missing.db").exists()


def test_server_without_its_local_user_does_not_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    env = _env_file(tmp_path, settings)
    env.write_text(env.read_text() + "GX_LOCAL_USER=alice\n")

    with pytest.raises(SystemExit) as exit_info:
        bootstrap.serve("stdio", env)

    assert exit_info.value.code == 2
    assert "local user 'alice' does not exist" in capsys.readouterr().err


def test_server_refuses_in_a_real_process_without_touching_stdout(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "guarded_exchange", "serve", "--env-file", str(_env_file(tmp_path, settings))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONPATH": ""},
        stdin=subprocess.DEVNULL,
    )

    assert result.returncode == 2
    assert result.stdout == ""  # stdout belongs to the MCP protocol
    assert "database not found" in result.stderr
    assert "guarded-exchange init" in result.stderr


@pytest.mark.parametrize("version", [(3, 34, 1), (2, 99, 99)])
def test_too_old_sqlite_is_refused(version: tuple[int, int, int]) -> None:
    with pytest.raises(StorageNotReady, match=r"3\.35\.0 or newer"):
        check_sqlite_version(version)


def test_current_sqlite_is_accepted() -> None:
    check_sqlite_version()
    check_sqlite_version((3, 35, 0))


# --- append-only journal (FR-37) ----------------------------------------------------------


@pytest.mark.req("FR-37")
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET actor = 'someone else'",
        "UPDATE audit_log SET payload = '{}' WHERE seq = 1",
        "DELETE FROM audit_log WHERE seq = 2",
        "DELETE FROM audit_log",
    ],
)
def test_update_or_delete_of_the_journal_is_rejected_by_the_database(tmp_path: Path, statement: str) -> None:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    before = _rows(settings.db_path, "SELECT * FROM audit_log ORDER BY seq")

    conn = sqlite3.connect(settings.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="audit_log is append-only"):
            conn.execute(statement)
    finally:
        conn.close()

    assert _rows(settings.db_path, "SELECT * FROM audit_log ORDER BY seq") == before


@pytest.mark.req("FR-37")
def test_journal_still_accepts_inserts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)

    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO audit_log (chain_id, seq, ts, event_type, actor, payload) "
            "VALUES ('system', 3, 0, 'x', 'test', '{}')"
        )
        conn.commit()
    finally:
        conn.close()

    assert _rows(settings.db_path, "SELECT count(*) FROM audit_log") == [(3,)]
