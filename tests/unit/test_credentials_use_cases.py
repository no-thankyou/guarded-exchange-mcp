"""Use cases for exchange keys on in-memory fakes: add/replace/delete, FR-49 branches, re-check."""

from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest

from guarded_exchange.application.credentials import (
    AddCredential,
    DeleteCredential,
    ListCredentials,
    RecheckActiveCredentials,
    RecheckCredential,
    RecheckOutcome,
    ReplaceCredential,
    RequireTradingCredential,
)
from guarded_exchange.application.policy import AuthorizeAccess, ReadPolicy, SetCeiling
from guarded_exchange.domain.audit import AuditRecord
from guarded_exchange.domain.credentials import (
    EXCHANGE_BYBIT,
    ApiKeyPair,
    Credential,
    CredentialStatus,
    KeyPermissions,
)
from guarded_exchange.domain.errors import (
    CredentialsPermissionsChanged,
    ExchangeCredentialsInvalid,
    ExchangeUnavailable,
    InvalidParams,
    NotFound,
    PermissionDenied,
    RateLimited,
)
from guarded_exchange.domain.policy import Permission, PolicyPatch, Profile
from guarded_exchange.domain.users import Environment, UserStatus
from tests.fakes import (
    FakeCipher,
    FakeClock,
    FakeKeyReader,
    InMemoryCredentialRepository,
    InMemoryDatabase,
    SequentialIds,
)

KEYS = ApiKeyPair(api_key="UNITKEY0123456789", api_secret="unit-secret-abcdefghijklmnopqrstuvwxyz")  # noqa: S106 - test value
TRADING = frozenset({"ContractTrade.Order", "ContractTrade.Position", "Spot.SpotTrade"})
OK = KeyPermissions(read_only=False, permissions=TRADING, expires_at=None, ip_restricted=True)
READ_ONLY = KeyPermissions(read_only=True, permissions=TRADING, expires_at=None, ip_restricted=True)
WITH_TRANSFER = KeyPermissions(
    read_only=False, permissions=TRADING | {"Wallet.AccountTransfer"}, expires_at=None, ip_restricted=True
)


@dataclass
class World:
    db: InMemoryDatabase
    clock: FakeClock
    reader: FakeKeyReader
    cipher: FakeCipher
    environment: Environment = Environment.TESTNET
    ids: SequentialIds = field(default_factory=lambda: SequentialIds("cred"))

    def scope(self) -> dict[str, object]:
        return {"exchange": EXCHANGE_BYBIT, "environment": self.environment}

    def add(self) -> AddCredential:
        return AddCredential(
            self.db,
            self.cipher,
            self.reader,
            self.clock,
            self.ids,
            **self.scope(),  # type: ignore[arg-type]
        )

    def replace(self) -> ReplaceCredential:
        return ReplaceCredential(
            self.db,
            self.cipher,
            self.reader,
            self.clock,
            self.ids,
            **self.scope(),  # type: ignore[arg-type]
        )

    def delete(self) -> DeleteCredential:
        return DeleteCredential(self.db, self.clock, **self.scope())  # type: ignore[arg-type]

    def recheck(self) -> RecheckCredential:
        return RecheckCredential(self.db, self.cipher, self.reader, self.clock)

    def require(self) -> RequireTradingCredential:
        return RequireTradingCredential(self.db, self.cipher, self.recheck(), self.clock, **self.scope())  # type: ignore[arg-type]

    def journal(self) -> list[AuditRecord]:
        return self.db.all_audit()


@pytest.fixture
def world() -> World:
    db = InMemoryDatabase()
    db.add_user("local")
    return World(db=db, clock=FakeClock(), reader=FakeKeyReader(permissions=OK), cipher=FakeCipher())


def _no_secret_in(records: list[AuditRecord]) -> None:
    text = repr([(r.event_type, r.actor, dict(r.payload)) for r in records])
    assert KEYS.api_key not in text
    assert KEYS.api_secret not in text


@pytest.mark.req("FR-30", "FR-31", "FR-49")
async def test_add_stores_an_allowed_key_encrypted_and_journals_it_without_the_secret(world: World) -> None:
    stored = await world.add()("local", KEYS)

    credential = stored.credential
    assert (credential.id, credential.user_id, credential.status) == (
        "cred-0001",
        "user-local",
        CredentialStatus.ACTIVE,
    )
    assert credential.permissions == OK
    assert credential.permissions_checked_at == world.clock.now()
    assert not credential.admin_attested
    assert stored.assessment.profile is Profile.TRADE
    assert world.reader.calls == [KEYS]
    assert world.cipher.open(world.db.state.credentials["cred-0001"][1], credential.binding) == KEYS
    [record] = world.journal()
    assert (record.event_type, record.actor, record.chain_id) == (
        "credential_added",
        "cli:user",
        "user-local",
    )
    assert record.payload["credential_id"] == "cred-0001"
    assert record.payload["profile"] == "trade"
    assert record.payload["permissions"]["permissions"] == sorted(TRADING)
    _no_secret_in(world.journal())


@pytest.mark.req("FR-49", "NFR-1")
@pytest.mark.parametrize("extra", ["Wallet.AccountTransfer", "Wallet.Withdraw", "Unknown.Thing"])
async def test_a_key_with_a_permission_outside_the_allowlist_is_rejected_and_not_stored(
    world: World, extra: str
) -> None:
    world.reader.permissions = KeyPermissions(
        read_only=False, permissions=TRADING | {extra}, expires_at=None, ip_restricted=True
    )

    with pytest.raises(ExchangeCredentialsInvalid, match="does not accept"):
        await world.add()("local", KEYS)

    assert world.db.state.credentials == {}
    [record] = world.journal()
    assert record.event_type == "credential_rejected"
    assert record.payload["reason"] == "permissions_not_allowed"
    assert record.payload["permissions"] == [extra]
    _no_secret_in(world.journal())


@pytest.mark.req("FR-49", "FR-11")
async def test_a_read_only_key_is_accepted_and_caps_the_profile_at_read_only(world: World) -> None:
    await SetCeiling(world.db, world.clock)("local", PolicyPatch(profile=Profile.TRADE))
    authorize = AuthorizeAccess(world.db)
    await authorize.require("user-local", Permission.TRADE)  # trade before the key arrives
    world.reader.permissions = READ_ONLY

    stored = await world.add()("local", KEYS)

    assert stored.assessment.profile is Profile.READ_ONLY
    with pytest.raises(PermissionDenied):
        await authorize.require("user-local", Permission.TRADE)
    await authorize.require("user-local", Permission.READ)
    view = await ReadPolicy(world.db).for_principal("user-local")
    assert view.effective.profile is Profile.READ_ONLY
    assert view.ceiling.profile is Profile.TRADE
    assert view.key_read_only
    with pytest.raises(PermissionDenied, match="read-only"):
        await world.require()("user-local")


@pytest.mark.req("FR-49")
async def test_unverifiable_permissions_on_testnet_without_attestation_are_refused(world: World) -> None:
    world.reader.fail_with = ExchangeUnavailable("the exchange is unavailable: HTTP 404")

    with pytest.raises(ExchangeCredentialsInvalid, match="cannot be checked"):
        await world.add()("local", KEYS)

    assert world.db.state.credentials == {}
    [record] = world.journal()
    assert record.event_type == "credential_rejected"
    assert record.payload["reason"] == "permissions_unverifiable"
    assert record.payload["admin_attestation_offered"] is False


@pytest.mark.req("FR-49", "FR-36")
async def test_unverifiable_permissions_on_testnet_with_attestation_are_accepted_and_journaled(
    world: World,
) -> None:
    world.reader.fail_with = ExchangeUnavailable("the exchange is unavailable: HTTP 404")

    stored = await world.add()("local", KEYS, admin_attested=True)

    assert stored.credential.admin_attested
    assert stored.credential.permissions is None
    assert stored.credential.permissions_checked_at is None
    assert stored.assessment.profile is Profile.TRADE
    [record] = world.journal()
    assert (record.event_type, record.actor) == ("credential_added", "cli:admin")
    assert record.payload["admin_attested"] is True
    assert record.payload["attested_by"] == "cli:admin"
    assert record.payload["attestation"] == "no withdrawal or transfer permission"
    assert record.payload["permissions"] is None


@pytest.mark.req("FR-49")
@pytest.mark.parametrize("attested", [False, True])
async def test_unverifiable_permissions_on_mainnet_are_always_refused(attested: bool) -> None:
    db = InMemoryDatabase()
    db.add_user("pro", environment=Environment.MAINNET)
    world = World(
        db=db,
        clock=FakeClock(),
        reader=FakeKeyReader(fail_with=ExchangeUnavailable("down")),
        cipher=FakeCipher(),
        environment=Environment.MAINNET,
    )

    with pytest.raises(ExchangeCredentialsInvalid, match="mainnet"):
        await world.add()("pro", KEYS, admin_attested=attested)

    assert db.state.credentials == {}


@pytest.mark.req("FR-49")
async def test_attestation_never_overrides_what_the_exchange_reports(world: World) -> None:
    world.reader.permissions = WITH_TRANSFER
    with pytest.raises(ExchangeCredentialsInvalid):
        await world.add()("local", KEYS, admin_attested=True)

    world.reader.permissions = None
    world.reader.fail_with = ExchangeCredentialsInvalid("the exchange did not accept the API key")
    with pytest.raises(ExchangeCredentialsInvalid, match="did not accept"):
        await world.add()("local", KEYS, admin_attested=True)

    assert world.db.state.credentials == {}
    assert [r.event_type for r in world.journal()] == ["credential_rejected", "credential_rejected"]


@pytest.mark.req("FR-49")
async def test_a_verified_key_is_stored_as_verified_even_if_attestation_was_offered(world: World) -> None:
    stored = await world.add()("local", KEYS, admin_attested=True)

    assert not stored.credential.admin_attested
    assert stored.credential.permissions == OK
    assert world.journal()[0].actor == "cli:user"


@pytest.mark.req("FR-30", "FR-29")
async def test_add_replace_and_delete_keep_one_active_key(world: World) -> None:
    with pytest.raises(NotFound):
        await world.replace()("local", KEYS)
    first = await world.add()("local", KEYS)
    with pytest.raises(InvalidParams, match="already has an active"):
        await world.add()("local", KEYS)

    second = await world.replace()("local", KEYS)

    assert second.replaced_id == first.credential.id
    stored = {c.id: c.status for c, _ in world.db.state.credentials.values()}
    assert stored == {
        first.credential.id: CredentialStatus.DELETED,
        second.credential.id: CredentialStatus.ACTIVE,
    }
    deleted = await world.delete()("local")
    assert deleted.id == second.credential.id
    assert deleted.deleted_at == world.clock.now()
    with pytest.raises(NotFound):
        await world.delete()("local")
    with pytest.raises(ExchangeCredentialsInvalid, match="no exchange key"):
        await world.require()("user-local")
    events = [(r.event_type, r.payload.get("replaced_credential_id")) for r in world.journal()]
    assert events == [
        ("credential_added", None),
        ("credential_replaced", first.credential.id),
        ("credential_deleted", None),
    ]
    user, listed = await ListCredentials(world.db)("local")
    assert user.name == "local"
    assert [c.status for c in listed] == [CredentialStatus.DELETED, CredentialStatus.DELETED]
    _no_secret_in(world.journal())


@pytest.mark.req("FR-30")
async def test_keys_are_refused_for_unknown_revoked_or_other_environment_users(world: World) -> None:
    world.db.add_user("gone", status=UserStatus.REVOKED)
    world.db.add_user("pro", environment=Environment.MAINNET)

    with pytest.raises(NotFound):
        await world.add()("nobody", KEYS)
    with pytest.raises(PermissionDenied, match="revoked"):
        await world.add()("gone", KEYS)
    with pytest.raises(PermissionDenied, match="configured for the testnet") as err:
        await world.add()("pro", KEYS)

    assert err.value.details == {"reason": "environment_mismatch"}
    assert world.reader.calls == []  # refused before the key went anywhere
    assert world.db.state.credentials == {}


@pytest.mark.req("FR-49")
async def test_recheck_after_an_hour_sees_grown_permissions_and_blocks_trading(world: World) -> None:
    await world.add()("local", KEYS)
    world.reader.calls.clear()
    trading = await world.require()("user-local")
    assert trading.keys == KEYS
    assert world.reader.calls == []  # checked less than an hour ago: no re-check

    world.reader.permissions = WITH_TRANSFER
    world.clock.advance(timedelta(minutes=59))
    await world.require()("user-local")  # still within the hour
    assert world.reader.calls == []

    world.clock.advance(timedelta(minutes=1))
    with pytest.raises(CredentialsPermissionsChanged) as err:
        await world.require()("user-local")

    assert err.value.code == "credentials_permissions_changed"
    assert world.reader.calls == [KEYS]
    changed = world.journal()[-1]
    assert (changed.event_type, changed.actor) == (
        "credential_permissions_changed",
        "system:permissions-recheck",
    )
    assert changed.payload["blocked"] is True
    assert changed.payload["disallowed"] == ["Wallet.AccountTransfer"]
    # the next action is refused from the stored answer, without asking the exchange again
    world.reader.calls.clear()
    with pytest.raises(CredentialsPermissionsChanged):
        await world.require()("user-local")
    assert world.reader.calls == []


@pytest.mark.req("FR-49")
async def test_permissions_back_inside_the_allowlist_unblock_trading_after_a_recheck(world: World) -> None:
    await world.add()("local", KEYS)
    world.reader.permissions = WITH_TRANSFER
    world.clock.advance(timedelta(hours=1))
    with pytest.raises(CredentialsPermissionsChanged):
        await world.require()("user-local")

    world.reader.permissions = OK
    world.clock.advance(timedelta(hours=1))
    await world.require()("user-local")

    assert world.journal()[-1].payload["blocked"] is False


@pytest.mark.req("FR-49", "NFR-1")
async def test_a_stale_key_that_cannot_be_rechecked_blocks_trading(world: World) -> None:
    await world.add()("local", KEYS)
    world.reader.fail_with = ExchangeUnavailable("timeout")
    world.clock.advance(timedelta(hours=2))

    with pytest.raises(ExchangeUnavailable, match="could not be re-checked") as caught:
        await world.require()("user-local")
    assert caught.value.details == {"reason": "permissions_recheck_failed"}


@pytest.mark.req("FR-49", "NFR-1")
async def test_a_rate_limited_recheck_blocks_trading_without_calling_the_exchange_down(world: World) -> None:
    await world.add()("local", KEYS)
    world.reader.fail_with = RateLimited("the exchange rate limit was hit", retry_after_s=7.0)
    world.clock.advance(timedelta(hours=2))

    with pytest.raises(RateLimited, match="could not be re-checked") as caught:  # fail closed
        await world.require()("user-local")
    assert caught.value.code == "rate_limited"
    assert caught.value.retry_after_s == 7.0
    assert caught.value.details == {"reason": "permissions_recheck_failed"}
    assert caught.value.next_step is not None
    assert "retry_after_s" in caught.value.next_step
    assert "down" not in caught.value.next_step


@pytest.mark.req("FR-49")
async def test_an_attested_key_keeps_working_while_the_permissions_stay_unverifiable(world: World) -> None:
    world.reader.fail_with = ExchangeUnavailable("HTTP 404")
    await world.add()("local", KEYS, admin_attested=True)

    trading = await world.require()("user-local")
    assert trading.credential.admin_attested

    # once the exchange can answer, its answer counts
    world.reader.fail_with = None
    world.reader.permissions = WITH_TRANSFER
    with pytest.raises(CredentialsPermissionsChanged):
        await world.require()("user-local")


@pytest.mark.req("FR-49")
async def test_recheck_of_all_active_keys_reports_each_outcome(world: World) -> None:
    world.db.add_user("bob")
    world.db.add_user("carol")
    await world.add()("local", KEYS)
    await world.add()("bob", KEYS)
    world.reader.permissions = READ_ONLY
    await world.add()("carol", KEYS)
    recheck_all = RecheckActiveCredentials(world.db, world.recheck(), world.clock, **world.scope())  # type: ignore[arg-type]

    world.reader.permissions = OK
    first = await recheck_all()
    world.reader.permissions = WITH_TRANSFER
    second = await recheck_all()
    world.reader.fail_with = ExchangeUnavailable("down")
    third = await recheck_all()

    assert [o.status for o in first] == ["ok", "ok", "ok"]  # carol's key became read-write
    assert {o.status for o in second} == {"blocked"}
    assert third == [
        RecheckOutcome(o.credential_id, o.user_id, "failed", "exchange_unavailable") for o in second
    ]
    world.reader.fail_with = None
    world.reader.permissions = READ_ONLY
    assert {o.status for o in await recheck_all()} == {"read_only"}


@pytest.mark.req("FR-49", "FR-35")
async def test_an_exchange_rate_limit_while_adding_means_cannot_verify_now_not_rejected(world: World) -> None:
    world.reader.fail_with = RateLimited("the exchange rate limit was hit", retry_after_s=1.0)

    with pytest.raises(ExchangeCredentialsInvalid, match="cannot be checked") as err:
        await world.add()("local", KEYS)

    assert err.value.details["reason"] == "permissions_unverifiable"  # "try again later"
    assert world.db.state.credentials == {}
    stored = await world.add()("local", KEYS, admin_attested=True)  # the test-environment branch
    assert stored.credential.admin_attested


@pytest.mark.req("FR-49", "FR-35")
async def test_an_exchange_rate_limit_during_the_recheck_is_failed_not_blocked(world: World) -> None:
    await world.add()("local", KEYS)
    recheck_all = RecheckActiveCredentials(world.db, world.recheck(), world.clock, **world.scope())  # type: ignore[arg-type]
    world.reader.fail_with = RateLimited("the exchange rate limit was hit", retry_after_s=1.0)

    [outcome] = await recheck_all()

    assert (outcome.status, outcome.reason) == ("failed", "rate_limited")


@pytest.mark.req("FR-31")
async def test_a_ciphertext_bound_to_another_user_is_not_usable(world: World) -> None:
    world.db.add_user("bob")
    await world.add()("local", KEYS)
    bob = await world.add()(
        "bob",
        ApiKeyPair(api_key="BOBKEY0123456789", api_secret="bob-secret-0123456789abc"),  # noqa: S106 - test value
    )
    local_cred, local_sealed = next(
        (c, s) for c, s in world.db.state.credentials.values() if c.user_id == "user-local"
    )
    world.db.state.credentials[bob.credential.id] = (bob.credential, local_sealed)  # copy A's secret into B

    with pytest.raises(ExchangeCredentialsInvalid, match="cannot be decrypted"):
        await world.require()("user-bob")
    assert (await world.require()("user-local")).credential.id == local_cred.id


# --- review round 1: deleted/replaced keys are never handed out (M1), attestation scope (m1) ----


@pytest.mark.req("FR-29", "FR-49")
async def test_a_key_deleted_during_the_recheck_is_not_handed_out(world: World) -> None:
    await world.add()("local", KEYS)
    world.clock.advance(timedelta(hours=2))
    delete = world.delete()
    ask_exchange = world.reader.get_key_permissions

    async def delete_while_asking(keys: ApiKeyPair) -> KeyPermissions:
        await delete("local")  # `keys delete` runs while the exchange is being asked
        return await ask_exchange(keys)

    world.reader.get_key_permissions = delete_while_asking  # type: ignore[method-assign]

    with pytest.raises(ExchangeCredentialsInvalid) as err:
        await world.require()("user-local")

    assert err.value.details == {"reason": "key_replaced_or_deleted"}
    assert [r.event_type for r in world.journal()] == ["credential_added", "credential_deleted"]


@pytest.mark.req("FR-29", "FR-49")
async def test_a_key_replaced_between_the_check_and_decryption_is_not_handed_out(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.add()("local", KEYS)  # fresh: no re-check, the race is check -> decrypt
    original = InMemoryCredentialRepository.get_active

    async def get_then_replaced(
        self: InMemoryCredentialRepository, user_id: str, exchange: str, environment: Environment
    ) -> Credential | None:
        found = await original(self, user_id, exchange, environment)
        if found is not None:  # a concurrent `keys replace` commits right after this read
            _, sealed = world.db.state.credentials[found.id]
            gone = replace(found, status=CredentialStatus.DELETED, deleted_at=world.clock.now())
            world.db.state.credentials[found.id] = (gone, sealed)
        return found

    monkeypatch.setattr(InMemoryCredentialRepository, "get_active", get_then_replaced)

    with pytest.raises(ExchangeCredentialsInvalid) as err:
        await world.require()("user-local")

    assert err.value.details == {"reason": "key_replaced_or_deleted"}


@pytest.mark.req("FR-49", "NFR-1")
async def test_an_attested_key_verified_once_is_not_exempt_when_a_later_recheck_fails(world: World) -> None:
    world.reader.fail_with = ExchangeUnavailable("HTTP 404")
    await world.add()("local", KEYS, admin_attested=True)
    world.reader.fail_with = None
    await world.require()("user-local")  # the exchange answered: permissions are now verified

    world.clock.advance(timedelta(hours=2))
    world.reader.fail_with = ExchangeUnavailable("timeout")

    with pytest.raises(ExchangeUnavailable, match="could not be re-checked"):
        await world.require()("user-local")
