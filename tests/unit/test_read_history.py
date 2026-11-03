"""History use cases on fakes: the caller's own key, checked period, cursor that keeps the query."""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from guarded_exchange.application.credentials import ReadCredential
from guarded_exchange.application.read_history import GetOrderHistory, GetTrades
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT, ApiKeyPair, KeyPermissions
from guarded_exchange.domain.errors import ExchangeCredentialsInvalid, InvalidParams, RateLimited
from guarded_exchange.domain.history import HistoryPage, HistoryPeriod
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.users import Environment, User
from tests.fakes import FakeCipher, FakeClock, InMemoryDatabase
from tests.fakes_history import FakeHistory, make_closed_order, make_trade

pytestmark = pytest.mark.req("FR-2", "FR-3")

KEYS_A = ApiKeyPair(api_key="ALICEKEY01234567", api_secret="alice-secret-abcdefghijklmnopqrstuv")  # noqa: S106
KEYS_B = ApiKeyPair(api_key="BOBKEY0123456789", api_secret="bob-secret-abcdefghijklmnopqrstuvwx")  # noqa: S106
READ_ONLY = KeyPermissions(read_only=True, permissions=frozenset(), expires_at=None, ip_restricted=True)
DAY = timedelta(days=1)


@dataclass
class World:
    db: InMemoryDatabase = field(default_factory=InMemoryDatabase)
    cipher: FakeCipher = field(default_factory=FakeCipher)
    clock: FakeClock = field(default_factory=FakeClock)
    history: FakeHistory = field(default_factory=FakeHistory)

    def read_key(self) -> ReadCredential:
        return ReadCredential(
            self.db, self.cipher, self.clock, exchange=EXCHANGE_BYBIT, environment=Environment.TESTNET
        )

    def orders(self) -> GetOrderHistory:
        return GetOrderHistory(self.read_key(), self.history, self.clock)

    def trades(self) -> GetTrades:
        return GetTrades(self.read_key(), self.history, self.clock)

    def user_with_key(self, name: str, keys: ApiKeyPair) -> User:
        user = self.db.add_user(name)
        self.db.add_credential(user, self.cipher, keys, permissions=READ_ONLY)
        return user


@pytest.fixture
def world() -> World:
    return World()


@pytest.mark.req("FR-33")
async def test_each_user_reads_history_with_their_own_key_only(world: World) -> None:
    alice = world.user_with_key("alice", KEYS_A)
    bob = world.user_with_key("bob", KEYS_B)

    await world.orders()(alice.id, "linear", limit=5)
    await world.trades()(bob.id, "spot", limit=5)

    assert [(call[0], call[1]) for call in world.history.calls] == [
        ("get_order_history", KEYS_A),
        ("get_trades", KEYS_B),
    ]


async def test_the_default_period_symbol_and_limit_reach_the_port(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)
    now = world.clock.now()

    page = await world.orders()(user.id, " Linear ", symbol="btcusdt", limit=7)

    [(_, _, category, symbol, period, token, limit)] = world.history.calls
    assert (category, symbol, token, limit) == (Category.LINEAR, "BTCUSDT", None, 7)
    assert period == HistoryPeriod(start=now - 7 * DAY, end=now)
    assert page.period == period
    assert page.next_cursor is None


async def test_the_next_page_is_read_with_the_first_pages_period_and_symbol(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)
    world.history.orders = {
        None: HistoryPage(items=(make_closed_order("order-1"),), next_exchange_cursor="tok%3A1", omitted=1),
        "tok%3A1": HistoryPage(items=(make_closed_order("order-2"),), next_exchange_cursor=None),
    }
    first = await world.orders()(user.id, "linear", symbol="ETHUSDT", limit=1)
    world.clock.advance(timedelta(hours=3))  # "the last 7 days" would now be another window

    assert first.next_cursor is not None
    assert first.omitted == 1
    second = await world.orders()(user.id, "linear", cursor=first.next_cursor, limit=1)

    first_call, second_call = world.history.calls
    assert second_call[3:6] == ("ETHUSDT", first_call[4], "tok%3A1")
    assert [o.order_id for o in second.items] == ["order-2"]
    assert second.period == first.period
    assert second.next_cursor is None


async def test_trades_page_like_orders(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)
    world.history.trades = {
        None: HistoryPage(items=(make_trade("t-1"),), next_exchange_cursor="132766%3A2"),
        "132766%3A2": HistoryPage(items=(make_trade("t-2"),), next_exchange_cursor=None),
    }

    first = await world.trades()(user.id, "linear", limit=1)
    assert first.next_cursor is not None
    second = await world.trades()(user.id, "linear", cursor=first.next_cursor, limit=1)

    assert [t.trade_id for t in first.items + second.items] == ["t-1", "t-2"]
    assert world.history.calls[1][5] == "132766%3A2"


def _args(**overrides: Any) -> dict[str, Any]:
    return {"limit": 5, **overrides}


@pytest.mark.req("FR-28")
@pytest.mark.parametrize(
    ("category", "args", "field_name"),
    [
        ("linear", {"start": "period"}, "end_time"),
        ("linear", {"start": "too-old"}, "start_time"),
        ("options", {}, "category"),
        ("linear", {"symbol": "BTC/USDT"}, "symbol"),
        ("linear", {"limit": 0}, "limit"),
        ("linear", {"limit": 51}, "limit"),
        ("linear", {"cursor": "garbage"}, "cursor"),
    ],
    ids=["eight-days", "older-than-two-years", "category", "symbol", "zero", "too-many", "cursor"],
)
async def test_bad_input_is_invalid_params_before_the_key_or_the_exchange(
    world: World, category: str, args: dict[str, Any], field_name: str
) -> None:
    user = world.user_with_key("alice", KEYS_A)
    now = world.clock.now()
    if args.get("start") == "period":
        args = {"start": now - 10 * DAY, "end": now - 2 * DAY}
    elif args.get("start") == "too-old":
        args = {"start": now - 731 * DAY}

    for read in (world.orders(), world.trades()):
        with pytest.raises(InvalidParams) as refused:
            await read(user.id, category, **_args(**args))
        assert refused.value.details["field"] == field_name

    assert world.history.calls == []


async def test_a_cursor_with_other_filters_or_another_category_is_refused(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)
    world.history.orders = {None: HistoryPage(items=(make_closed_order(),), next_exchange_cursor="tok")}
    cursor = (await world.orders()(user.id, "linear", limit=1)).next_cursor
    assert cursor is not None
    now = world.clock.now()

    for extra in ({"symbol": "BTCUSDT"}, {"start": now - DAY}, {"end": now}):
        with pytest.raises(InvalidParams) as refused:
            await world.orders()(user.id, "linear", cursor=cursor, **_args(**extra))
        assert refused.value.details == {"field": "cursor", "reason": "cursor_with_filters"}
    with pytest.raises(InvalidParams) as other_category:
        await world.orders()(user.id, "spot", cursor=cursor, limit=1)
    with pytest.raises(InvalidParams) as other_tool:
        await world.trades()(user.id, "linear", cursor=cursor, limit=1)

    assert other_category.value.next_step == "Pass category=linear with this cursor."
    assert other_tool.value.details == {"field": "cursor", "reason": "invalid_cursor"}
    assert len(world.history.calls) == 1


async def test_a_cursor_that_aged_past_the_two_year_horizon_is_refused(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)
    now = world.clock.now()
    world.history.orders = {None: HistoryPage(items=(make_closed_order(),), next_exchange_cursor="tok")}
    cursor = (await world.orders()(user.id, "linear", start=now - 729 * DAY, limit=1)).next_cursor
    assert cursor is not None
    world.clock.advance(2 * DAY)

    with pytest.raises(InvalidParams) as refused:
        await world.orders()(user.id, "linear", cursor=cursor, limit=1)

    assert refused.value.details == {"field": "start_time", "reason": "period"}


@pytest.mark.req("FR-28")
async def test_without_a_key_history_is_exchange_credentials_invalid(world: World) -> None:
    user = world.db.add_user("alice")

    with pytest.raises(ExchangeCredentialsInvalid) as refused:
        await world.trades()(user.id, "spot", limit=5)

    assert refused.value.details == {"reason": "no_key"}
    assert world.history.calls == []


async def test_exchange_errors_pass_through(world: World) -> None:
    user = world.user_with_key("alice", KEYS_A)
    world.history.fail_with = RateLimited("slow down", retry_after_s=2.0)

    with pytest.raises(RateLimited):
        await world.orders()(user.id, "linear", limit=5)
