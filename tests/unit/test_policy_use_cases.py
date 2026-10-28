"""Policy use cases on in-memory fakes: read, admin ceiling, user tightening, access decisions."""

from datetime import timedelta
from decimal import Decimal

import pytest

from guarded_exchange.application.policy import (
    ADMIN_ACTOR,
    USER_ACTOR,
    AuthorizeAccess,
    ReadPolicy,
    SetCeiling,
    TightenPolicy,
)
from guarded_exchange.application.ports import UnitOfWorkFactory
from guarded_exchange.domain.errors import InvalidParams, NotFound, PermissionDenied
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import (
    NO_OVERRIDES,
    InstrumentRef,
    Permission,
    PolicyPatch,
    Profile,
    default_ceiling,
)
from guarded_exchange.domain.users import Environment, UserStatus
from tests.fakes import FakeClock, InMemoryDatabase

BTC = InstrumentRef(Category.LINEAR, "BTCUSDT")
ETH = InstrumentRef(Category.LINEAR, "ETHUSDT")
TOOLS = {"get_ticker": Permission.READ, "get_my_policy": Permission.READ, "place_order": Permission.TRADE}
GRANT_TRADING = PolicyPatch(profile=Profile.TRADE, allowlist=frozenset({BTC, ETH}))


@pytest.fixture
def db() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _uow(db: InMemoryDatabase) -> UnitOfWorkFactory:
    return db  # the fake must satisfy the port structurally


@pytest.mark.req("FR-45", "FR-18")
@pytest.mark.parametrize("environment", list(Environment))
async def test_unconfigured_user_gets_the_new_user_defaults(
    db: InMemoryDatabase, environment: Environment
) -> None:
    user = db.add_user("alice", environment=environment)

    view = await ReadPolicy(_uow(db)).by_name("alice")

    assert view.user == user
    assert view.ceiling_is_default
    assert view.ceiling == default_ceiling(environment)
    assert view.overrides == NO_OVERRIDES
    assert view.effective.profile is Profile.READ_ONLY
    assert view.effective.allowlist == frozenset()
    assert view.effective.environment is environment
    assert db.transactions == [False]


async def test_unknown_user_is_not_found(db: InMemoryDatabase, clock: FakeClock) -> None:
    for call in (
        ReadPolicy(_uow(db)).by_name("ghost"),
        SetCeiling(_uow(db), clock)("ghost", GRANT_TRADING),
        TightenPolicy(_uow(db), clock)("ghost", PolicyPatch(max_pending=1)),
    ):
        with pytest.raises(NotFound):
            await call


@pytest.mark.req("FR-12", "FR-36")
async def test_admin_sets_the_ceiling_under_the_user_lock_and_journals_it(
    db: InMemoryDatabase, clock: FakeClock
) -> None:
    user = db.add_user("alice")

    view = await SetCeiling(_uow(db), clock)("alice", GRANT_TRADING)

    assert not view.ceiling_is_default
    assert view.effective.profile is Profile.TRADE
    assert view.effective.allowlist == {BTC, ETH}
    assert view.effective.max_order_notional_usdt == Decimal(1000)  # untouched fields keep the default
    assert db.transactions == [True]
    assert db.locked_users == [user.id]
    record = db.state.policies[user.id]
    assert record.ceiling == view.ceiling
    assert record.updated_at == clock.now()
    [entry] = db.state.audit[user.id]  # the user's own chain: they can read it (FR-38)
    assert (entry.event_type, entry.actor, entry.ts) == ("policy_ceiling_set", ADMIN_ACTOR, clock.now())
    assert entry.payload["changes"] == {"allowlist": ["linear:BTCUSDT", "linear:ETHUSDT"], "profile": "trade"}
    assert entry.payload["ceiling"]["profile"] == "trade"


@pytest.mark.req("FR-12", "FR-36")
async def test_user_tightens_and_the_change_is_journaled(db: InMemoryDatabase, clock: FakeClock) -> None:
    user = db.add_user("alice")
    await SetCeiling(_uow(db), clock)("alice", GRANT_TRADING)
    clock.advance(timedelta(minutes=1))

    view = await TightenPolicy(_uow(db), clock)(
        "alice", PolicyPatch(max_order_notional_usdt=Decimal(100), allowlist=frozenset({BTC}))
    )

    assert view.effective.max_order_notional_usdt == Decimal(100)
    assert view.effective.allowlist == {BTC}
    assert view.ceiling.max_order_notional_usdt == Decimal(1000)  # the ceiling itself is unchanged
    assert db.locked_users == [user.id, user.id]
    tightened = db.state.audit[user.id][-1]
    assert (tightened.seq, tightened.event_type, tightened.actor) == (2, "policy_tightened", USER_ACTOR)
    assert tightened.payload["changes"] == {"allowlist": ["linear:BTCUSDT"], "max_order_notional_usdt": "100"}
    assert tightened.payload["effective"]["max_order_notional_usdt"] == "100"


@pytest.mark.req("FR-12")
async def test_tightening_keeps_a_default_ceiling_default(db: InMemoryDatabase, clock: FakeClock) -> None:
    user = db.add_user("alice")

    view = await TightenPolicy(_uow(db), clock)("alice", PolicyPatch(max_pending=2))

    assert view.ceiling_is_default
    assert db.state.policies[user.id].ceiling is None  # code defaults still apply, not a frozen copy
    assert view.effective.max_pending == 2


@pytest.mark.req("FR-12", "FR-44")
@pytest.mark.parametrize(
    ("change", "field"),
    [
        (PolicyPatch(profile=Profile.TRADE), "profile"),  # read-only user cannot grant itself trading
        (PolicyPatch(max_leverage=Decimal(20)), "max_leverage"),
        (PolicyPatch(trust_client_ui=True, max_pending=1), "trust_client_ui"),
    ],
)
async def test_weakening_is_refused_with_the_field_and_changes_nothing(
    db: InMemoryDatabase, clock: FakeClock, change: PolicyPatch, field: str
) -> None:
    user = db.add_user("alice")
    await TightenPolicy(_uow(db), clock)("alice", PolicyPatch(trust_client_ui=False))
    before = (dict(db.state.policies), list(db.state.audit[user.id]))

    with pytest.raises(PermissionDenied) as caught:
        await TightenPolicy(_uow(db), clock)("alice", change)

    assert caught.value.details == {"field": field}
    assert (dict(db.state.policies), list(db.state.audit[user.id])) == before


@pytest.mark.req("FR-12")
async def test_user_cannot_undo_their_own_tightening(db: InMemoryDatabase, clock: FakeClock) -> None:
    db.add_user("alice")
    tighten = TightenPolicy(_uow(db), clock)
    await tighten("alice", PolicyPatch(max_order_notional_usdt=Decimal(100)))

    with pytest.raises(PermissionDenied):
        await tighten("alice", PolicyPatch(max_order_notional_usdt=Decimal(500)))


@pytest.mark.req("FR-12")
async def test_admin_lowering_the_ceiling_below_the_users_setting_wins(
    db: InMemoryDatabase, clock: FakeClock
) -> None:
    db.add_user("alice")
    await SetCeiling(_uow(db), clock)("alice", GRANT_TRADING)
    await TightenPolicy(_uow(db), clock)("alice", PolicyPatch(max_order_notional_usdt=Decimal(500)))

    lowered = await SetCeiling(_uow(db), clock)("alice", PolicyPatch(max_order_notional_usdt=Decimal(200)))
    raised = await SetCeiling(_uow(db), clock)("alice", PolicyPatch(max_order_notional_usdt=Decimal(5000)))

    assert lowered.overrides.max_order_notional_usdt == Decimal(500)  # the user's setting is kept ...
    assert lowered.effective.max_order_notional_usdt == Decimal(200)  # ... but the ceiling applies
    assert raised.effective.max_order_notional_usdt == Decimal(500)  # raising it again: the user's applies


async def test_admin_ceiling_outside_hard_bounds_is_rejected_before_any_write(
    db: InMemoryDatabase, clock: FakeClock
) -> None:
    db.add_user("alice")

    with pytest.raises(InvalidParams):
        await SetCeiling(_uow(db), clock)("alice", PolicyPatch(slippage_pct=Decimal(5)))

    assert db.state.policies == {}
    assert db.all_audit() == []


async def test_empty_change_is_invalid(db: InMemoryDatabase, clock: FakeClock) -> None:
    db.add_user("alice")

    for call in (
        SetCeiling(_uow(db), clock)("alice", PolicyPatch()),
        TightenPolicy(_uow(db), clock)("alice", PolicyPatch()),
    ):
        with pytest.raises(InvalidParams):
            await call
    assert db.transactions == []


async def test_revoked_user_cannot_tighten(db: InMemoryDatabase, clock: FakeClock) -> None:
    db.add_user("alice", status=UserStatus.REVOKED)

    with pytest.raises(PermissionDenied):
        await TightenPolicy(_uow(db), clock)("alice", PolicyPatch(max_pending=1))


# --- the principal's view (MCP) -------------------------------------------------------------


@pytest.mark.req("FR-44")
async def test_principal_reads_their_effective_policy(db: InMemoryDatabase, clock: FakeClock) -> None:
    user = db.add_user("alice")
    await SetCeiling(_uow(db), clock)("alice", GRANT_TRADING)

    view = await ReadPolicy(_uow(db)).for_principal(user.id)

    assert view.effective.profile is Profile.TRADE


@pytest.mark.req("FR-11", "NFR-1")
async def test_unknown_missing_or_revoked_principal_gets_nothing(db: InMemoryDatabase) -> None:
    revoked = db.add_user("bob", status=UserStatus.REVOKED)
    access = AuthorizeAccess(_uow(db))

    for principal in (None, "nobody", revoked.id):
        assert await access.visible(principal, TOOLS) == frozenset()
        with pytest.raises(PermissionDenied) as caught:
            await access.require(principal, Permission.READ)
        assert caught.value.message == "access denied for this caller"  # one answer for all cases
        with pytest.raises(PermissionDenied):
            await ReadPolicy(_uow(db)).for_principal(principal)


@pytest.mark.req("FR-11")
async def test_access_follows_the_profile_and_policy_changes_apply_on_the_next_call(
    db: InMemoryDatabase, clock: FakeClock
) -> None:
    user = db.add_user("alice")
    access = AuthorizeAccess(_uow(db))

    assert await access.visible(user.id, TOOLS) == {"get_ticker", "get_my_policy"}
    with pytest.raises(PermissionDenied):
        await access.require(user.id, Permission.TRADE)

    await SetCeiling(_uow(db), clock)("alice", GRANT_TRADING)
    assert await access.visible(user.id, TOOLS) == set(TOOLS)
    await access.require(user.id, Permission.TRADE)

    await TightenPolicy(_uow(db), clock)("alice", PolicyPatch(profile=Profile.READ_ONLY))
    assert await access.visible(user.id, TOOLS) == {"get_ticker", "get_my_policy"}
