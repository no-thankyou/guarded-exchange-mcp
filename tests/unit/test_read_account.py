"""Account read use cases on fakes: the caller's own key only, clear refusals, FR-3 limits."""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from guarded_exchange.application.credentials import ReadCredential
from guarded_exchange.application.read_account import GetBalances, GetOpenOrders, GetPositions
from guarded_exchange.domain.account import MAX_ACCOUNT_ITEMS, PositionSide
from guarded_exchange.domain.credentials import (
    EXCHANGE_BYBIT,
    ApiKeyPair,
    CredentialStatus,
    CredentialUnreadable,
    KeyPermissions,
    SealedSecret,
)
from guarded_exchange.domain.errors import (
    ExchangeCredentialsInvalid,
    InvalidParams,
    PermissionDenied,
    RateLimited,
)
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.users import Environment, User, UserStatus
from tests.fakes import (
    FakeAccount,
    FakeCipher,
    FakeClock,
    InMemoryCredentialRepository,
    InMemoryDatabase,
    make_order,
    make_position,
)

pytestmark = pytest.mark.req("FR-2")

KEYS_A = ApiKeyPair(api_key="ALICEKEY01234567", api_secret="alice-secret-abcdefghijklmnopqrstuv")  # noqa: S106
KEYS_B = ApiKeyPair(api_key="BOBKEY0123456789", api_secret="bob-secret-abcdefghijklmnopqrstuvwx")  # noqa: S106
TRADING = frozenset({"ContractTrade.Order", "ContractTrade.Position", "Spot.SpotTrade"})
READ_ONLY = KeyPermissions(read_only=True, permissions=frozenset(), expires_at=None, ip_restricted=True)
TRADE = KeyPermissions(read_only=False, permissions=TRADING, expires_at=None, ip_restricted=True)


@dataclass
class World:
    db: InMemoryDatabase = field(default_factory=InMemoryDatabase)
    cipher: FakeCipher = field(default_factory=FakeCipher)
    clock: FakeClock = field(default_factory=FakeClock)
    account: FakeAccount = field(default_factory=FakeAccount)
    environment: Environment = Environment.TESTNET

    def read_key(self) -> ReadCredential:
        return ReadCredential(
            self.db, self.cipher, self.clock, exchange=EXCHANGE_BYBIT, environment=self.environment
        )

    def balances(self) -> GetBalances:
        return GetBalances(self.read_key(), self.account)

    def positions(self) -> GetPositions:
        return GetPositions(self.read_key(), self.account)

    def orders(self) -> GetOpenOrders:
        return GetOpenOrders(self.read_key(), self.account)

    def user_with_key(
        self, name: str, keys: ApiKeyPair, permissions: KeyPermissions | None = READ_ONLY
    ) -> User:
        user = self.db.add_user(name)
        self.db.add_credential(user, self.cipher, keys, permissions=permissions)
        return user


@pytest.fixture
def world() -> World:
    return World()


@pytest.mark.req("FR-33")
async def test_each_user_reads_with_their_own_key_only(world: World) -> None:
    alice = world.user_with_key("alice", KEYS_A)
    bob = world.user_with_key("bob", KEYS_B)

    await world.balances()(alice.id)
    await world.balances()(bob.id)

    assert [call[1] for call in world.account.calls] == [KEYS_A, KEYS_B]


@pytest.mark.req("FR-11", "FR-49")
@pytest.mark.parametrize(
    "permissions",
    [READ_ONLY, TRADE, None],
    ids=["read-only-key", "trading-key", "attested-never-verified"],
)
async def test_any_usable_key_reads_the_account(world: World, permissions: KeyPermissions | None) -> None:
    user = world.user_with_key("alice", KEYS_A, permissions)

    balances = await world.balances()(user.id)

    assert balances == world.account.balances


@pytest.mark.req("FR-49")
async def test_grown_permissions_block_trading_but_not_reading(world: World) -> None:
    grown = KeyPermissions(
        read_only=False, permissions=TRADING | {"Wallet.AccountTransfer"}, expires_at=None, ip_restricted=True
    )
    user = world.user_with_key("alice", KEYS_A, grown)

    await world.balances()(user.id)

    assert world.account.calls == [("get_balances", KEYS_A)]


@pytest.mark.req("FR-28")
async def test_without_a_key_the_agent_is_told_to_have_the_user_add_one_outside_the_chat(
    world: World,
) -> None:
    user = world.db.add_user("alice")

    with pytest.raises(ExchangeCredentialsInvalid) as err:
        await world.balances()(user.id)

    assert err.value.details == {"reason": "no_key"}
    assert err.value.next_step is not None
    assert "keys add" in err.value.next_step
    assert "outside the chat" in err.value.next_step
    assert world.account.calls == []


@pytest.mark.req("FR-29")
async def test_a_deleted_key_is_not_used(world: World) -> None:
    user = world.db.add_user("alice")
    world.db.add_credential(
        user, world.cipher, KEYS_A, permissions=READ_ONLY, status=CredentialStatus.DELETED
    )

    with pytest.raises(ExchangeCredentialsInvalid) as err:
        await world.positions()(user.id, None, 20)

    assert err.value.details == {"reason": "no_key"}
    assert world.account.calls == []


@pytest.mark.req("FR-28")
async def test_an_expired_key_is_refused_before_the_exchange(world: World) -> None:
    expiring = KeyPermissions(
        read_only=True,
        permissions=frozenset(),
        expires_at=world.clock.now() + timedelta(minutes=1),
        ip_restricted=True,
    )
    user = world.user_with_key("alice", KEYS_A, expiring)
    world.clock.advance(timedelta(minutes=1))

    with pytest.raises(ExchangeCredentialsInvalid) as err:
        await world.orders()(user.id, "spot", None, 20)

    assert err.value.details == {"reason": "expired"}
    assert world.account.calls == []


@pytest.mark.req("FR-29")
async def test_a_key_whose_secret_is_gone_is_reported_as_replaced(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = world.user_with_key("alice", KEYS_A)

    async def vanished(self: InMemoryCredentialRepository, credential_id: str) -> SealedSecret | None:
        return None

    monkeypatch.setattr(InMemoryCredentialRepository, "get_sealed_active", vanished)

    with pytest.raises(ExchangeCredentialsInvalid) as err:
        await world.balances()(user.id)

    assert err.value.details == {"reason": "key_replaced_or_deleted"}
    assert world.account.calls == []


@pytest.mark.req("FR-33")
async def test_a_key_bound_to_another_user_does_not_decrypt(world: World) -> None:
    alice = world.user_with_key("alice", KEYS_A)
    bob = world.db.add_user("bob")
    alice_credential, alice_sealed = next(iter(world.db.state.credentials.values()))
    # a copied row: bob's credential with alice's ciphertext
    world.db.state.credentials["cred-bob"] = (
        world.db.add_credential(bob, world.cipher, KEYS_B, permissions=READ_ONLY, credential_id="cred-bob"),
        alice_sealed,
    )
    assert alice_credential.user_id == alice.id

    with pytest.raises(CredentialUnreadable):
        await world.balances()(bob.id)

    assert world.account.calls == []


@pytest.mark.req("FR-33")
@pytest.mark.parametrize("who", ["nobody", "unknown", "revoked"])
async def test_unknown_or_revoked_callers_get_the_same_refusal(world: World, who: str) -> None:
    revoked = world.db.add_user("revoked", status=UserStatus.REVOKED)
    world.db.add_credential(revoked, world.cipher, KEYS_A, permissions=READ_ONLY)
    user_id = {"nobody": None, "unknown": "user-ghost", "revoked": revoked.id}[who]

    with pytest.raises(PermissionDenied) as err:
        await world.balances()(user_id)

    assert err.value.message == "access denied for this caller"
    assert world.account.calls == []


async def test_a_user_of_another_environment_is_refused(world: World) -> None:
    user = world.db.add_user("alice", environment=Environment.MAINNET)

    with pytest.raises(PermissionDenied) as err:
        await world.balances()(user.id)

    assert err.value.details == {"reason": "environment_mismatch"}


@pytest.mark.req("FR-3")
@pytest.mark.parametrize(
    ("symbol", "limit"),
    [("BTC/USDT", 20), (None, 0), (None, MAX_ACCOUNT_ITEMS + 1), (None, True)],
    ids=["bad-symbol", "zero", "too-many", "bool"],
)
async def test_bad_arguments_are_refused_before_keys_or_exchange(
    world: World, symbol: str | None, limit: int
) -> None:
    user = world.user_with_key("alice", KEYS_A)
    world.db.transactions.clear()

    with pytest.raises(InvalidParams):
        await world.positions()(user.id, symbol, limit)

    assert world.db.transactions == []
    assert world.account.calls == []


async def test_an_unknown_category_is_invalid_params(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)

    with pytest.raises(InvalidParams):
        await world.orders()(user.id, "options", None, 20)


async def test_the_symbol_is_normalised_and_passed_to_the_exchange(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)
    world.account.positions = (make_position("BTCUSDT"), make_position("ETHUSDT", PositionSide.SHORT))

    page = await world.positions()(user.id, " btcusdt ", 5)

    assert [p.symbol for p in page.items] == ["BTCUSDT"]
    assert world.account.calls == [("get_positions", KEYS_A, "BTCUSDT", 5)]


@pytest.mark.req("FR-3")
async def test_never_more_than_limit_items_whatever_the_adapter_returns(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)
    world.account.orders = {Category.LINEAR: tuple(make_order(f"order-{i}") for i in range(5))}

    page = await world.orders()(user.id, "linear", None, 2)

    assert [o.order_id for o in page.items] == ["order-0", "order-1"]
    assert page.has_more is True
    assert world.account.calls == [("get_open_orders", KEYS_A, Category.LINEAR, None, 2)]


@pytest.mark.req("FR-2")
async def test_reads_open_only_read_transactions_and_call_the_exchange_after_them(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)
    world.db.transactions.clear()
    world.account.db = world.db  # the fake notes every exchange call made inside a unit of work

    await world.balances()(user.id)
    await world.positions()(user.id, None, 20)
    await world.orders()(user.id, "spot", None, 20)

    assert len(world.account.calls) == 3
    assert world.account.calls_in_transaction == 0  # D-10: no network I/O inside a transaction
    assert world.db.transactions == [False, False, False]  # no write lock for a read
    assert world.db.locked_users == []


def test_the_transaction_probe_of_the_fake_detects_a_call_inside_a_unit_of_work() -> None:
    world = World()
    world.account.db = world.db

    async def call_inside() -> None:
        async with world.db(write=False):
            await world.account.get_balances(KEYS_A)

    asyncio.run(call_inside())

    assert world.account.calls_in_transaction == 1


@pytest.mark.req("FR-35")
async def test_exchange_errors_reach_the_caller_unchanged(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)
    world.account.fail_with = RateLimited("the exchange rate limit was hit", retry_after_s=1.5)

    with pytest.raises(RateLimited) as err:
        await world.balances()(user.id)

    assert err.value.retry_after_s == 1.5
