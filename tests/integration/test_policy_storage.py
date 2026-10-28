"""Policies on a real SQLite database: repository, journal entries and the CLI end to end."""

import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import typer
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from guarded_exchange import bootstrap
from guarded_exchange.application.policy import SetCeiling, TightenPolicy
from guarded_exchange.application.ports import UnitOfWorkFactory
from guarded_exchange.domain.errors import PermissionDenied
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import (
    NO_OVERRIDES,
    InstrumentRef,
    PolicyPatch,
    PolicyRecord,
    Profile,
    default_ceiling,
)
from guarded_exchange.domain.users import Environment, new_user
from guarded_exchange.infrastructure.db.repositories import ReadOnlyUnitOfWorkError
from guarded_exchange.interface.cli.main import PolicyCommands, build_cli
from tests.fakes import FakeClock
from tests.integration.conftest import BUSY_TIMEOUT_MS, make_settings

BTC = InstrumentRef(Category.LINEAR, "BTCUSDT")


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """CLI commands reconfigure the root logger onto the captured stderr; undo it."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


async def _add_user(uow: UnitOfWorkFactory, clock: FakeClock, name: str = "alice") -> str:
    async with uow(write=True) as tx:
        user = new_user(f"id-{name}", name, clock.now())
        await tx.users.add(user)
    return user.id


def _audit_rows(db_path: Path, chain_id: str) -> list[tuple[int, str, str, dict[str, object]]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT seq, event_type, actor, payload FROM audit_log WHERE chain_id = ? ORDER BY seq",
            (chain_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(seq, event, actor, json.loads(payload)) for seq, event, actor, payload in rows]


async def test_policy_record_round_trips_exactly(uow: UnitOfWorkFactory) -> None:
    clock = FakeClock()
    user_id = await _add_user(uow, clock)
    ceiling = default_ceiling(Environment.TESTNET)
    record = PolicyRecord(
        ceiling=ceiling,
        overrides=PolicyPatch(
            max_order_notional_usdt=Decimal("0.123456789012345678901234567890123"),
            confirm_above_notional_usdt=None,
        ),
        updated_at=clock.now(),
    )

    async with uow(write=True) as tx:
        await tx.lock_user(user_id)
        await tx.policies.save(user_id, record)
    async with uow(write=False) as tx:
        stored = await tx.policies.get(user_id)
        missing = await tx.policies.get("nobody")

    assert stored == record
    assert stored is not None
    assert str(stored.overrides.max_order_notional_usdt) == "0.123456789012345678901234567890123"  # no float
    assert missing is None


async def test_save_replaces_the_row_and_a_null_ceiling_means_default(uow: UnitOfWorkFactory) -> None:
    clock = FakeClock()
    user_id = await _add_user(uow, clock)
    first = PolicyRecord(
        ceiling=default_ceiling(Environment.TESTNET), overrides=NO_OVERRIDES, updated_at=clock.now()
    )
    clock.advance(timedelta(seconds=5))
    second = PolicyRecord(ceiling=None, overrides=PolicyPatch(max_pending=1), updated_at=clock.now())

    async with uow(write=True) as tx:
        await tx.policies.save(user_id, first)
    async with uow(write=True) as tx:
        await tx.policies.save(user_id, second)
    async with uow(write=False) as tx:
        assert await tx.policies.get(user_id) == second


async def test_policy_needs_an_existing_user_and_a_write_unit_of_work(uow: UnitOfWorkFactory) -> None:
    record = PolicyRecord(ceiling=None, overrides=NO_OVERRIDES, updated_at=FakeClock().now())

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        async with uow(write=True) as tx:
            await tx.policies.save("nobody", record)
    async with uow(write=False) as tx:
        with pytest.raises(ReadOnlyUnitOfWorkError):
            await tx.policies.save("nobody", record)
        with pytest.raises(RuntimeError, match="write unit of work"):
            await tx.lock_user("nobody")


@pytest.mark.req("FR-36", "FR-12")
async def test_policy_changes_are_written_to_the_users_journal(uow: UnitOfWorkFactory, db_path: Path) -> None:
    clock = FakeClock()
    user_id = await _add_user(uow, clock)

    await SetCeiling(uow, clock)("alice", PolicyPatch(profile=Profile.TRADE, allowlist=frozenset({BTC})))
    await TightenPolicy(uow, clock)("alice", PolicyPatch(max_leverage=Decimal(2)))
    with pytest.raises(PermissionDenied):
        await TightenPolicy(uow, clock)("alice", PolicyPatch(max_leverage=Decimal(5)))

    rows = _audit_rows(db_path, user_id)
    assert [(seq, event, actor) for seq, event, actor, _ in rows] == [
        (1, "policy_ceiling_set", "cli:admin"),
        (2, "policy_tightened", "cli:user"),
    ]  # the refused attempt changed nothing and wrote nothing
    assert rows[0][3]["changes"] == {"allowlist": ["linear:BTCUSDT"], "profile": "trade"}
    assert rows[1][3]["changes"] == {"max_leverage": "2"}
    async with uow(write=False) as tx:
        stored = await tx.policies.get(user_id)
    assert stored is not None
    assert stored.overrides == PolicyPatch(max_leverage=Decimal(2))


# --- CLI through the real composition root ------------------------------------------------


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


def _cli() -> CliRunner:
    return CliRunner()


def _app() -> typer.Typer:
    commands = PolicyCommands(
        show=bootstrap.show_policy, tighten=bootstrap.tighten_policy, set_ceiling=bootstrap.set_ceiling
    )
    return build_cli(bootstrap.serve, bootstrap.init, commands)


@pytest.mark.req("FR-12", "FR-36", "FR-45")
def test_cli_show_set_ceiling_and_tighten_on_a_real_installation(env_file: Path, tmp_path: Path) -> None:
    app = _app()
    env = ["--env-file", str(env_file)]

    shown = _cli().invoke(app, ["policy", "show", *env])
    granted = _cli().invoke(
        app,
        ["admin", "policy", "set-ceiling", "--profile", "trade", "--allow", "linear:BTCUSDT", *env],
    )
    tightened = _cli().invoke(app, ["policy", "tighten", "--max-leverage", "3", "--no-trust-client-ui", *env])
    weakened = _cli().invoke(app, ["policy", "tighten", "--max-leverage", "5", *env])

    assert shown.exit_code == 0, shown.output
    assert "ceiling (default)" in shown.output
    assert "profile" in shown.output
    assert "read_only" in shown.output
    assert granted.exit_code == 0, granted.output
    assert "ceiling (set by admin)" in granted.output
    assert "linear:BTCUSDT" in granted.output
    assert tightened.exit_code == 0, tightened.output
    leverage_row = next(line for line in tightened.output.splitlines() if line.startswith("max_leverage"))
    assert leverage_row.split() == ["max_leverage", "10", "3", "3"]
    assert weakened.exit_code == 1
    assert "max_leverage" in weakened.stderr
    events = [row[1] for row in _audit_rows(make_settings(tmp_path).db_path, _local_user_id(tmp_path))]
    assert events[-2:] == ["policy_ceiling_set", "policy_tightened"]


def _local_user_id(tmp_path: Path) -> str:
    conn = sqlite3.connect(make_settings(tmp_path).db_path)
    try:
        (user_id,) = conn.execute("SELECT id FROM users WHERE name = 'local'").fetchone()
    finally:
        conn.close()
    return str(user_id)


def test_cli_reports_bad_values_and_unknown_users_without_a_traceback(env_file: Path) -> None:
    app = _app()
    env = ["--env-file", str(env_file)]

    bad_value = _cli().invoke(app, ["policy", "tighten", "--slippage-pct", "abc", *env])
    out_of_bounds = _cli().invoke(
        app,
        ["admin", "policy", "set-ceiling", "--confirmation-ttl-s", "900", *env],
    )
    unknown = _cli().invoke(app, ["policy", "show", "--user", "ghost", *env])
    nothing = _cli().invoke(app, ["policy", "tighten", *env])

    assert bad_value.exit_code == 2
    assert "slippage_pct" in bad_value.stderr
    assert out_of_bounds.exit_code == 2
    assert "confirmation_ttl_s" in out_of_bounds.stderr
    assert unknown.exit_code == 1
    assert "'ghost' does not exist" in unknown.stderr
    assert nothing.exit_code == 1
    assert "nothing to change" in nothing.stderr
    for result in (bad_value, out_of_bounds, unknown, nothing):
        assert "Traceback" not in result.output


def test_cli_refuses_an_uninitialised_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    env = tmp_path / "gx.env"
    env.write_text(f"GX_DB_PATH={settings.db_path}\nGX_MASTER_KEY_FILE={settings.master_key_file}\n")

    result = _cli().invoke(_app(), ["policy", "show", "--env-file", str(env)])

    assert result.exit_code == 1
    assert "guarded-exchange init" in result.stderr
    assert not settings.db_path.exists()
