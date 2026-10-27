"""Use cases on in-memory fakes: ``InitializeInstallation`` and ``ResolveLocalPrincipal``."""

from dataclasses import replace
from datetime import timedelta

import pytest

from guarded_exchange.application.admin import InitializeInstallation, MasterKeyMissing
from guarded_exchange.application.auth import PrincipalUnavailable, ResolveLocalPrincipal
from guarded_exchange.application.ports import Clock, IdGenerator, MasterKeyStore, UnitOfWorkFactory
from guarded_exchange.domain.audit import SYSTEM_CHAIN, AuditEvent
from guarded_exchange.domain.errors import InvalidParams
from guarded_exchange.domain.users import Environment, UserStatus
from tests.fakes import FakeClock, FakeMasterKeyStore, InMemoryDatabase, ReadOnlyViolation, SequentialIds

pytestmark = pytest.mark.req("FR-36", "FR-45", "NFR-1")


def _init(
    db: InMemoryDatabase, clock: FakeClock, key: FakeMasterKeyStore, ids: SequentialIds | None = None
) -> InitializeInstallation:
    # The fakes must satisfy the ports structurally.
    uow: UnitOfWorkFactory = db
    clock_port: Clock = clock
    ids_port: IdGenerator = ids or SequentialIds("user")
    key_port: MasterKeyStore = key
    return InitializeInstallation(uow, clock_port, ids_port, key_port)


async def test_first_init_creates_key_user_and_journal_in_one_write_transaction() -> None:
    db, clock, key = InMemoryDatabase(), FakeClock(), FakeMasterKeyStore()

    result = await _init(db, clock, key)("local")

    assert result.first_init
    assert result.user_created
    assert result.master_key_created
    assert key.created == 1
    assert db.transactions == [True]
    user = db.state.users[result.user.id]
    assert user.name == "local"
    assert user.environment is Environment.TESTNET  # FR-45
    assert user.status is UserStatus.ACTIVE
    assert user.created_at == clock.now()  # "now" comes from the Clock port
    records = db.state.audit[SYSTEM_CHAIN]
    assert [(r.seq, r.event_type) for r in records] == [(1, "installation_initialized"), (2, "user_created")]
    assert all(r.ts == clock.now() and r.actor == "cli:init" for r in records)
    assert records[0].payload == {"local_user": "local", "master_key_created": True}
    assert records[1].payload == {
        "user_id": user.id,
        "name": "local",
        "status": "active",
        "environment": "testnet",
    }


async def test_rerun_changes_nothing() -> None:
    db, clock, key = InMemoryDatabase(), FakeClock(), FakeMasterKeyStore()
    first = await _init(db, clock, key)("local")
    clock.advance(timedelta(hours=1))

    second = await _init(db, clock, key)("local")

    assert second.user == first.user
    assert not (second.first_init)
    assert not (second.user_created)
    assert not (second.master_key_created)
    assert key.created == 1
    assert len(db.state.users) == 1
    assert len(db.all_audit()) == 2


async def test_existing_key_is_reused_on_first_init() -> None:
    db, key = InMemoryDatabase(), FakeMasterKeyStore(present=True)

    result = await _init(db, FakeClock(), key)("local")

    assert result.first_init
    assert not result.master_key_created
    assert key.created == 0
    assert db.state.audit[SYSTEM_CHAIN][0].payload["master_key_created"] is False


async def test_lost_key_of_an_initialised_installation_is_never_regenerated() -> None:
    db, clock, key = InMemoryDatabase(), FakeClock(), FakeMasterKeyStore()
    await _init(db, clock, key)("local")
    key.present = False  # the key file was deleted

    with pytest.raises(MasterKeyMissing):
        await _init(db, clock, key)("local")

    assert key.created == 1
    assert len(db.all_audit()) == 2


async def test_another_local_user_name_on_an_initialised_installation_adds_only_that_user() -> None:
    db, clock, key, ids = InMemoryDatabase(), FakeClock(), FakeMasterKeyStore(), SequentialIds()
    await _init(db, clock, key, ids)("local")

    result = await _init(db, clock, key, ids)("alice")

    assert result.user_created
    assert not result.first_init
    assert [r.event_type for r in db.state.audit[SYSTEM_CHAIN]] == [
        "installation_initialized",
        "user_created",
        "user_created",
    ]


async def test_failure_inside_the_transaction_leaves_no_partial_state() -> None:
    db, key = InMemoryDatabase(), FakeMasterKeyStore()

    class BrokenIds:
        def new_id(self) -> str:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await InitializeInstallation(db, FakeClock(), BrokenIds(), key)("local")

    assert db.state.users == {}
    assert db.all_audit() == []


async def test_invalid_user_name_is_rejected_before_any_change() -> None:
    db, key = InMemoryDatabase(), FakeMasterKeyStore()

    with pytest.raises(InvalidParams):
        await _init(db, FakeClock(), key)("not valid")

    assert key.created == 0
    assert db.transactions == []


async def test_local_principal_is_the_configured_user() -> None:
    db = InMemoryDatabase()
    created = await _init(db, FakeClock(), FakeMasterKeyStore())("local")

    principal = await ResolveLocalPrincipal(db)("LOCAL")

    assert principal == created.user
    assert db.transactions[-1] is False  # read unit of work


async def test_missing_local_user_fails_closed() -> None:
    with pytest.raises(PrincipalUnavailable, match="guarded-exchange init"):
        await ResolveLocalPrincipal(InMemoryDatabase())("local")


async def test_revoked_local_user_fails_closed() -> None:
    db = InMemoryDatabase()
    created = await _init(db, FakeClock(), FakeMasterKeyStore())("local")
    db.state.users[created.user.id] = replace(created.user, status=UserStatus.REVOKED)

    with pytest.raises(PrincipalUnavailable, match="revoked"):
        await ResolveLocalPrincipal(db)("local")


async def test_fake_read_unit_of_work_rejects_writes() -> None:
    db = InMemoryDatabase()

    async with db(write=False) as uow:
        with pytest.raises(ReadOnlyViolation):
            await uow.audit.append(SYSTEM_CHAIN, AuditEvent("x", "test"), FakeClock().now())
