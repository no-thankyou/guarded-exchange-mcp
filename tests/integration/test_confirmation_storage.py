"""Pending confirmations on a real SQLite database and the CLI end to end (tickets 016, 017; SPEC FR-20,
FR-22, FR-24, FR-42; PLAN ADR-005, D-3): migration 0009, the repository's CAS, the CLI channel."""

import logging
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from guarded_exchange import bootstrap
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.confirmation import ConfirmationStatus, DecisionChannel, intact, new_pending
from guarded_exchange.domain.risk import RiskDirection
from guarded_exchange.domain.users import Environment, new_user
from guarded_exchange.infrastructure.clock import SystemClock
from guarded_exchange.infrastructure.db.uow import SqlUnitOfWorkFactory
from guarded_exchange.interface.cli.main import (
    AdminCommands,
    ConfirmCommands,
    KeyCommands,
    PolicyCommands,
    build_cli,
)
from tests.fakes import FakeClock
from tests.integration.conftest import BUSY_TIMEOUT_MS, make_settings
from tests.integration.test_policy_storage import _local_user_id

pytestmark = pytest.mark.req("FR-20", "FR-22", "FR-42")

S = ConfirmationStatus


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _pending(pending_id: str, user_id: str, clock: FakeClock, key: str | None = None):  # type: ignore[no-untyped-def]
    return new_pending(
        pending_id=pending_id,
        user_id=user_id,
        credential_id="cred-1",
        environment=Environment.TESTNET,
        tool=ActionKind.PLACE_ORDER,
        action={"symbol": "BTCUSDT", "qty": Decimal("0.10")},
        risk=RiskDirection.INCREASES,
        shown={"order": {"qty": Decimal("0.1"), "origin": "external"}},
        now=clock.now(),
        ttl_s=120,
        idempotency_key=key,
    )


async def test_a_pending_round_trips_and_its_digests_survive_the_json_column(
    uow: SqlUnitOfWorkFactory,
) -> None:
    clock = FakeClock()
    async with uow(write=True) as tx:
        await tx.users.add(new_user("u-1", "alice", clock.now()))
        await tx.confirmations.add(_pending("p-1", "u-1", clock, key="k-1"))

    async with uow(write=False) as tx:
        stored = await tx.confirmations.get("p-1")
        by_key = await tx.confirmations.find_by_key("u-1", "k-1", clock.now() - timedelta(hours=1))
        open_one = await tx.confirmations.find_open_by_digest("u-1", stored.action_digest, clock.now())  # type: ignore[union-attr]
        slots = await tx.confirmations.count_slots("u-1", clock.now())

    assert stored is not None
    assert stored.action == {"symbol": "BTCUSDT", "qty": "0.1"}  # "0.10" is stored canonically
    assert intact(stored)  # FR-22: the recorded digests still match what was read back
    assert (by_key, open_one) == (stored, stored)
    assert slots == 1


async def test_only_one_of_two_decisions_wins_and_an_expired_one_cannot_be_approved(
    uow: SqlUnitOfWorkFactory,
) -> None:
    clock = FakeClock()
    async with uow(write=True) as tx:
        await tx.users.add(new_user("u-1", "alice", clock.now()))
        await tx.confirmations.add(_pending("p-1", "u-1", clock))
        await tx.confirmations.add(_pending("p-2", "u-1", clock))

    pending = frozenset({S.PENDING})
    async with uow(write=True) as tx:
        first = await tx.confirmations.transition(
            "p-1",
            expected=pending,
            to=S.APPROVED,
            at=clock.now(),
            live_at=clock.now(),
            channel=DecisionChannel.CLI,
        )
    async with uow(write=True) as tx:
        second = await tx.confirmations.transition(
            "p-1", expected=pending, to=S.APPROVED, at=clock.now(), live_at=clock.now()
        )
        late = clock.now() + timedelta(seconds=121)
        expired = await tx.confirmations.transition(
            "p-2", expected=pending, to=S.APPROVED, at=late, live_at=late
        )
    async with uow(write=False) as tx:
        approved = await tx.confirmations.get("p-1")
        slots_after_ttl = await tx.confirmations.count_slots("u-1", late)

    assert (first, second, expired) == (True, False, False)
    assert approved is not None
    assert (approved.status, approved.approved_at, approved.channel) == (
        S.APPROVED,
        clock.now(),
        DecisionChannel.CLI,
    )
    assert slots_after_ttl == 1  # the expired pending holds no slot; the approved one does


def test_the_status_column_rejects_unknown_states(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    insert = (
        "INSERT INTO pending_confirmations (id, user_id, credential_id, environment, tool, action, "
        "action_digest, terms_digest, risk, shown, status, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    row = ("p", "u", "c", "testnet", "place_order", "{}", "a" * 64, "b" * 64, "increases", "{}")
    try:
        conn.execute(
            "INSERT INTO users (id, name, status, environment, created_at) "
            "VALUES ('u', 'n', 'active', 'testnet', 0)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(insert, (*row, "approved-by-agent", 0, 1))
    finally:
        conn.close()


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


async def _park(tmp_path: Path, pending_id: str) -> None:
    from guarded_exchange.infrastructure.db.uow import create_sqlite_async_engine  # noqa: PLC0415

    settings = make_settings(tmp_path)
    engine = create_sqlite_async_engine(settings.db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    try:
        async with SqlUnitOfWorkFactory(engine)(write=True) as tx:
            await tx.confirmations.add(
                _pending(pending_id, _local_user_id(tmp_path), FakeClock(current=SystemClock().now()))
            )
    finally:
        await engine.dispose()


def _stored_status(tmp_path: Path, pending_id: str) -> str:
    conn = sqlite3.connect(make_settings(tmp_path).db_path)
    try:
        (status,) = conn.execute(
            "SELECT status FROM pending_confirmations WHERE id = ?", (pending_id,)
        ).fetchone()
    finally:
        conn.close()
    return str(status)


def test_the_cli_lists_shows_external_orders_and_approves_only_when_the_admin_enabled_it(
    env_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anyio.run(_park, tmp_path, "p-1")
    cli = CliRunner()
    env = ["--env-file", str(env_file)]

    commands = ConfirmCommands(
        list=bootstrap.list_confirmations,
        show=bootstrap.show_confirmation,
        approve=bootstrap.approve_confirmation,
        reject=bootstrap.reject_confirmation,
    )
    policy, admin, keys = _app_commands()
    app = build_cli(bootstrap.serve, bootstrap.init, policy, admin, keys, commands)

    listed = cli.invoke(app, ["confirm", "list", *env])
    disabled = cli.invoke(app, ["confirm", "approve", "p-1", "--yes", *env])
    still = _stored_status(tmp_path, "p-1")
    monkeypatch.setenv("GX_CONFIRM_CLI_ENABLED", "true")
    approved = cli.invoke(app, ["confirm", "approve", "p-1", "--yes", *env])
    again = cli.invoke(app, ["confirm", "approve", "p-1", "--yes", *env])

    assert listed.exit_code == 0, listed.output
    assert "place_order on testnet (risk: increases)" in listed.stdout
    assert "EXTERNAL order" in listed.stdout  # FR-21: affected external orders are named
    assert disabled.exit_code == 1
    assert "not enabled" in disabled.stderr
    assert still == "pending"
    assert approved.exit_code == 0, approved.output
    assert _stored_status(tmp_path, "p-1") == "approved"
    assert again.exit_code == 1
    assert "not waiting for a decision" in again.stderr


def _app_commands() -> tuple[PolicyCommands, AdminCommands, KeyCommands]:
    return (
        PolicyCommands(
            show=bootstrap.show_policy,
            tighten=bootstrap.tighten_policy,
            set_ceiling=bootstrap.set_ceiling,
            reset_overrides=bootstrap.reset_overrides,
            kill_switch=bootstrap.set_kill_switch,
        ),
        AdminCommands(
            create_user=bootstrap.create_user,
            list_users=bootstrap.list_users,
            issue_token=bootstrap.issue_token,
            revoke_token=bootstrap.revoke_token,
        ),
        KeyCommands(
            add=bootstrap.add_key,
            replace=bootstrap.replace_key,
            delete=bootstrap.delete_key,
            show=bootstrap.show_keys,
        ),
    )
