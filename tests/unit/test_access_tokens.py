"""Bearer tokens: domain rules, issuing, revoking, authenticating (ticket 026, PLAN ADR-004)."""

import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

from guarded_exchange.application.admin import (
    CreateUser,
    IssueAccessToken,
    ListUsers,
    RevokeAccessToken,
)
from guarded_exchange.application.auth import AuthenticateBearerToken
from guarded_exchange.application.policy import ADMIN_ACTOR
from guarded_exchange.domain.access_tokens import (
    MAX_TOKEN_LENGTH,
    TOKEN_PREFIX,
    AccessToken,
    hash_token,
    is_well_formed,
    token_matches,
)
from guarded_exchange.domain.audit import SYSTEM_CHAIN
from guarded_exchange.domain.errors import InvalidParams, NotFound, PermissionDenied
from guarded_exchange.domain.users import Environment, UserStatus
from guarded_exchange.infrastructure.tokens import SecretTokenGenerator
from tests.fakes import (
    FakeClock,
    InMemoryAccessTokenRepository,
    InMemoryDatabase,
    SequentialIds,
    SequentialTokens,
)

GOOD = TOKEN_PREFIX + "A" * 43


# --- domain ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "A" * 47,  # no prefix
        TOKEN_PREFIX + "A" * 42,  # too short
        TOKEN_PREFIX + "A" * (MAX_TOKEN_LENGTH - len(TOKEN_PREFIX) + 1),  # too long
        TOKEN_PREFIX + "A" * 42 + "=",  # outside the URL-safe alphabet
        GOOD + "\n",
        " " + GOOD,
    ],
)
def test_malformed_tokens_are_refused_before_hashing(raw: str) -> None:
    assert not is_well_formed(raw)


def test_well_formed_and_hash_is_sha256_hex() -> None:
    assert is_well_formed(GOOD)
    assert hash_token(GOOD) == hashlib.sha256(GOOD.encode()).hexdigest()
    assert len(hash_token(GOOD)) == 64


def test_token_matches_only_its_own_secret() -> None:
    clock = FakeClock()
    stored = AccessToken("t", "u", hash_token(GOOD), clock.now())

    assert token_matches(stored, GOOD)
    assert not token_matches(stored, GOOD[:-1] + "B")


def test_system_generator_makes_distinct_well_formed_256_bit_tokens() -> None:
    generator = SecretTokenGenerator()
    tokens = {generator.new_token() for _ in range(50)}

    assert len(tokens) == 50
    assert all(is_well_formed(t) and len(t) == len(TOKEN_PREFIX) + 43 for t in tokens)


# --- users -------------------------------------------------------------------------------


@pytest.mark.req("FR-29", "FR-45")
async def test_create_user_is_active_on_testnet_and_journaled() -> None:
    db, clock = InMemoryDatabase(), FakeClock()

    user = await CreateUser(db, clock, SequentialIds("user"))("Alice")

    assert (user.id, user.name, user.status, user.environment) == (
        "user-0001",
        "alice",
        UserStatus.ACTIVE,
        Environment.TESTNET,
    )
    assert db.state.users[user.id] == user
    [record] = db.state.audit[SYSTEM_CHAIN]
    assert (record.event_type, record.actor) == ("user_created", ADMIN_ACTOR)
    assert record.payload["user_id"] == user.id


async def test_create_user_refuses_a_duplicate_and_writes_nothing() -> None:
    db = InMemoryDatabase()
    db.add_user("alice")

    with pytest.raises(InvalidParams, match="already exists"):
        await CreateUser(db, FakeClock(), SequentialIds())("alice")

    assert db.all_audit() == []


async def test_list_users_by_name_in_a_read_transaction() -> None:
    db = InMemoryDatabase()
    db.add_user("bob")
    db.add_user("alice")

    users = await ListUsers(db)()

    assert [u.name for u in users] == ["alice", "bob"]
    assert db.transactions == [False]


# --- issue and revoke --------------------------------------------------------------------


@pytest.mark.req("FR-29")
async def test_issue_stores_only_the_hash_and_journals_only_the_id() -> None:
    db, clock = InMemoryDatabase(), FakeClock()
    user = db.add_user("alice")

    issued = await IssueAccessToken(db, clock, SequentialIds("tok"), SequentialTokens())("alice")

    stored = db.state.access_tokens[issued.token_id]
    assert stored == AccessToken(issued.token_id, user.id, hash_token(issued.secret), clock.now())
    assert issued.secret not in repr(stored)
    [record] = db.state.audit[user.id]
    assert (record.event_type, record.actor, dict(record.payload)) == (
        "access_token_issued",
        ADMIN_ACTOR,
        {"token_id": issued.token_id},
    )
    assert issued.secret not in repr(db.all_audit())


async def test_issue_for_unknown_or_revoked_user_is_refused() -> None:
    db = InMemoryDatabase()
    db.add_user("gone", status=UserStatus.REVOKED)
    issue = IssueAccessToken(db, FakeClock(), SequentialIds(), SequentialTokens())

    with pytest.raises(NotFound):
        await issue("nobody")
    with pytest.raises(PermissionDenied, match="revoked"):
        await issue("gone")

    assert db.state.access_tokens == {}
    assert db.all_audit() == []


async def test_issue_refuses_a_malformed_secret_from_a_broken_generator() -> None:
    class Broken:
        def new_token(self) -> str:
            return "short"

    db = InMemoryDatabase()
    db.add_user("alice")

    with pytest.raises(RuntimeError, match="malformed"):
        await IssueAccessToken(db, FakeClock(), SequentialIds(), Broken())("alice")

    assert db.state.access_tokens == {}


@pytest.mark.req("FR-29")
async def test_revoke_is_journaled_once_and_idempotent() -> None:
    db, clock = InMemoryDatabase(), FakeClock()
    user = db.add_user("alice")
    issued = await IssueAccessToken(db, clock, SequentialIds("tok"), SequentialTokens())("alice")
    clock.advance(timedelta(minutes=1))
    revoke = RevokeAccessToken(db, clock)

    first = await revoke(issued.token_id)
    clock.advance(timedelta(minutes=1))
    second = await revoke(issued.token_id)

    assert not first.already_revoked
    assert first.token.revoked_at == clock.now() - timedelta(minutes=1)
    assert second.already_revoked
    assert second.token.revoked_at == first.token.revoked_at
    assert [r.event_type for r in db.state.audit[user.id]] == ["access_token_issued", "access_token_revoked"]


async def test_revoke_unknown_token_is_not_found() -> None:
    with pytest.raises(NotFound):
        await RevokeAccessToken(InMemoryDatabase(), FakeClock())("nope")


# --- authenticate ------------------------------------------------------------------------


@pytest.mark.req("FR-29", "FR-33")
async def test_authenticate_returns_the_owner_of_a_valid_token() -> None:
    db = InMemoryDatabase()
    user = db.add_user("alice")
    issued = await IssueAccessToken(db, FakeClock(), SequentialIds(), SequentialTokens())("alice")

    assert await AuthenticateBearerToken(db).authenticate(issued.secret) == user.id


@pytest.mark.req("FR-29")
async def test_authenticate_refuses_revoked_token_and_revoked_user() -> None:
    db, clock = InMemoryDatabase(), FakeClock()
    db.add_user("alice")
    db.add_user("bob")
    tokens = IssueAccessToken(db, clock, SequentialIds(), SequentialTokens())
    revoked = await tokens("alice")
    of_bob = await tokens("bob")
    await RevokeAccessToken(db, clock)(revoked.token_id)
    authenticate = AuthenticateBearerToken(db)
    assert await authenticate.authenticate(of_bob.secret) == "user-bob"

    db.state.users["user-bob"] = replace(db.state.users["user-bob"], status=UserStatus.REVOKED)

    assert await authenticate.authenticate(revoked.secret) is None
    assert await authenticate.authenticate(of_bob.secret) is None  # checked on every call


@pytest.mark.req("FR-29")
async def test_authenticate_refuses_unknown_and_malformed_without_a_lookup() -> None:
    db = InMemoryDatabase()
    db.add_user("alice")
    authenticate = AuthenticateBearerToken(db)

    assert await authenticate.authenticate(GOOD) is None  # well-formed, never issued
    lookups = len(db.transactions)
    assert await authenticate.authenticate("not-a-token") is None
    assert await authenticate.authenticate(TOKEN_PREFIX + "x" * 10_000) is None
    assert len(db.transactions) == lookups  # refused before touching storage


async def test_authenticate_compares_the_found_row_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth: a row returned for the hash is compared again in constant time."""
    db, clock = InMemoryDatabase(), FakeClock()
    user = db.add_user("alice")
    wrong_row = AccessToken("t", user.id, hash_token(TOKEN_PREFIX + "B" * 43), clock.now())

    async def any_row(self: InMemoryAccessTokenRepository, token_hash: str) -> AccessToken:
        return wrong_row

    monkeypatch.setattr(InMemoryAccessTokenRepository, "get_by_hash", any_row)

    assert await AuthenticateBearerToken(db).authenticate(GOOD) is None
