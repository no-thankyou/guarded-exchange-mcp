"""The kill switch on a real SQLite database (ticket 015; SPEC FR-16, FR-36, FR-44): migration 0008,
the repository, the use case under ``lock_user`` and the CLI end to end."""

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from guarded_exchange import bootstrap
from guarded_exchange.application.kill_switch import SetKillSwitch
from guarded_exchange.application.policy import ReadPolicy
from guarded_exchange.domain.kill_switch import KillSwitch, KillSwitchActor
from guarded_exchange.domain.users import new_user
from guarded_exchange.infrastructure.db.uow import SqlUnitOfWorkFactory
from tests.fakes import FakeClock
from tests.integration.conftest import BUSY_TIMEOUT_MS, make_settings
from tests.integration.test_policy_storage import _app, _audit_rows, _local_user_id

pytestmark = pytest.mark.req("FR-16")


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """CLI commands reconfigure the root logger onto the captured stderr; undo it."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    path = tmp_path / "gx.env"
    path.write_text(
        f"GX_DB_PATH={settings.db_path}\nGX_MASTER_KEY_FILE={settings.master_key_file}\n"
        f"GX_SQLITE_BUSY_TIMEOUT_MS={BUSY_TIMEOUT_MS}\n"
    )
    return path


async def test_the_switch_is_stored_with_the_user_and_reaches_the_effective_policy(
    uow: SqlUnitOfWorkFactory,
) -> None:
    clock = FakeClock()
    async with uow(write=True) as tx:
        await tx.users.add(new_user("u-1", "alice", clock.now()))

    change = await SetKillSwitch(uow, clock)("alice", on=True, by=KillSwitchActor.ADMIN)
    async with uow(write=False) as tx:
        user = await tx.users.get("u-1")
    view = await ReadPolicy(uow).by_name("alice")

    assert change.after is KillSwitch.ADMIN
    assert user is not None
    assert user.kill_switch is KillSwitch.ADMIN
    assert view.effective.kill_switch is KillSwitch.ADMIN


def test_a_new_user_starts_with_the_switch_off_and_the_column_rejects_unknown_states(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, name, status, environment, created_at) VALUES ('u', 'n', 'active', "
            "'testnet', 0)"
        )
        (state,) = conn.execute("SELECT kill_switch FROM users WHERE id = 'u'").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute("UPDATE users SET kill_switch = 'agent' WHERE id = 'u'")
    finally:
        conn.close()

    assert state == "none"


@pytest.mark.req("FR-36", "FR-44")
def test_cli_user_and_admin_kill_switch_on_a_real_installation(env_file: Path, tmp_path: Path) -> None:
    app, cli = _app(), CliRunner()
    env = ["--env-file", str(env_file)]

    user_on = cli.invoke(app, ["kill-switch", "on", *env])
    shown = cli.invoke(app, ["policy", "show", *env])
    admin_on = cli.invoke(app, ["admin", "kill-switch", "on", "--user", "local", *env])
    user_off = cli.invoke(app, ["kill-switch", "off", "--yes", *env])
    admin_off = cli.invoke(app, ["admin", "kill-switch", "off", "--user", "local", "--yes", *env])

    assert user_on.exit_code == 0, user_on.output
    assert "kill switch ON, turned on by the user" in user_on.stdout
    assert "kill switch: ON" in shown.stdout
    assert admin_on.exit_code == 0, admin_on.output
    assert user_off.exit_code == 1
    assert "only the admin can turn it off" in user_off.stderr
    assert "Traceback" not in user_off.output
    assert admin_off.exit_code == 0, admin_off.output
    assert "kill switch off" in admin_off.stdout
    rows = _audit_rows(make_settings(tmp_path).db_path, _local_user_id(tmp_path))
    assert [(row[1], row[2], row[3]) for row in rows if row[1].startswith("kill_switch")] == [
        ("kill_switch_on", "cli:user", {"before": "none", "after": "user"}),
        ("kill_switch_on", "cli:admin", {"before": "user", "after": "admin"}),
        ("kill_switch_off", "cli:admin", {"before": "admin", "after": "none"}),
    ]
