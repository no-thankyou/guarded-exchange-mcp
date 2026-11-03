"""Idempotency records in real SQLite and FR-27 reconnaissance through the Bybit adapter (ticket 012:
FR-26, FR-27, FR-33, ADR-014, D-10, D-20).

"Two clients" are two engines on one database file, as two MCP client processes of one user would be.
"""

import json
import os
import sqlite3
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest

from guarded_exchange.application.credentials import RecheckCredential, RequireTradingCredential
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.submit_action import OrderResult, ResultSource, SubmitPlaceOrder
from guarded_exchange.domain.audit import JsonValue
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT, Credential, CredentialStatus
from guarded_exchange.domain.errors import ExchangeRejected, OutcomeUnknown
from guarded_exchange.domain.idempotency import IdempotencyRecord, KeyKind, RecordStatus, new_claim
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.orders import FillStatus
from guarded_exchange.domain.policy import NO_OVERRIDES, PolicyRecord
from guarded_exchange.domain.users import Environment, new_user
from guarded_exchange.infrastructure.bybit.client import BybitClient, RetryPolicy
from guarded_exchange.infrastructure.bybit.http import make_http_client
from guarded_exchange.infrastructure.clock import SystemClock
from guarded_exchange.infrastructure.crypto import AesGcmCredentialCipher, Keyring
from guarded_exchange.infrastructure.db.uow import SqlUnitOfWorkFactory, create_sqlite_async_engine
from tests.conftest import load_fixture
from tests.fakes import FakeKeyReader, SequentialIds
from tests.integration.conftest import BUSY_TIMEOUT_MS
from tests.integration.test_bybit_trading import (
    CLIENT_ID,
    EMPTY_ORDERS,
    KEYS,
    MARKET_BUY,
    NOW_MS,
    TRADING_KEY,
    Stub,
    client,
    ioc_buy,
    trading_ceiling,
)

pytestmark = pytest.mark.req("FR-26")

AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
KEY = "0b6f4d7e-1c2a-4f7b-9a1e-5d3c2b1a0f9e"
FOUND = "bybit/spike004/linear_order_ioc_first_read.json"

Handler = Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]]


@pytest.fixture
async def second_uow(db_path: Path) -> AsyncIterator[SqlUnitOfWorkFactory]:
    """Another connection pool on the same file: a second client process of the same user."""
    engine = create_sqlite_async_engine(db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    try:
        yield SqlUnitOfWorkFactory(engine)
    finally:
        await engine.dispose()


async def add_user(uow: SqlUnitOfWorkFactory, user_id: str) -> None:
    async with uow(write=True) as tx:
        await tx.users.add(new_user(user_id, user_id, AT))


def claim_for(user_id: str, action_id: str, *, key: str = KEY, now: datetime = AT) -> IdempotencyRecord:
    return new_claim(
        user_id=user_id, kind=KeyKind.EXPLICIT, key=key, request_hash="a" * 64, action_id=action_id, now=now
    )


# --- the repository (ADR-014: one atomic statement, fenced changes) -------------------------------


async def test_concurrent_claims_of_one_key_from_two_clients_have_exactly_one_winner(
    uow: SqlUnitOfWorkFactory, second_uow: SqlUnitOfWorkFactory
) -> None:
    await add_user(uow, "u-1")
    held: list[tuple[str, str]] = []

    async def claim(factory: SqlUnitOfWorkFactory, action_id: str) -> None:
        async with factory(write=True) as tx:
            record = await tx.idempotency.claim(claim_for("u-1", action_id), AT)
        held.append((action_id, record.action_id))

    async with anyio.create_task_group() as tasks:
        for n in range(10):
            tasks.start_soon(claim, uow if n % 2 else second_uow, f"a-{n}")

    winners = {holder for _, holder in held}
    assert len(winners) == 1  # everyone sees the same holder
    assert sum(1 for mine, holder in held if mine == holder) == 1


async def test_a_live_record_is_kept_an_expired_one_is_taken_over(uow: SqlUnitOfWorkFactory) -> None:
    await add_user(uow, "u-1")
    first = replace(claim_for("u-1", "a-1"), expires_at=AT + timedelta(seconds=30))
    async with uow(write=True) as tx:
        await tx.idempotency.claim(first, AT)
        live = await tx.idempotency.claim(claim_for("u-1", "a-2"), AT + timedelta(seconds=29))
        taken = await tx.idempotency.claim(claim_for("u-1", "a-3"), AT + timedelta(seconds=30))

    assert live.action_id == "a-1"
    assert (taken.action_id, taken.status, taken.result) == ("a-3", RecordStatus.IN_PROGRESS, None)


async def test_changes_are_fenced_by_the_holder_and_answers_round_trip(uow: SqlUnitOfWorkFactory) -> None:
    await add_user(uow, "u-1")
    mine = claim_for("u-1", "a-1")
    other = replace(mine, action_id="a-2")
    async with uow(write=True) as tx:
        await tx.idempotency.claim(mine, AT)
        assert await tx.idempotency.mark_sent(other) is False
        assert await tx.idempotency.finish(other, RecordStatus.COMPLETED, {"x": "1"}, AT) is False
        assert await tx.idempotency.release(other) is False
        assert await tx.idempotency.mark_sent(mine) is True
        assert await tx.idempotency.mark_sent(mine) is False  # only from in_progress
        answer: dict[str, JsonValue] = {
            "kind": "error",
            "code": "limit_exceeded",
            "details": {"allowed": "0.10"},
        }
        assert await tx.idempotency.finish(mine, RecordStatus.COMPLETED, answer, AT + timedelta(hours=24))
        stored = await tx.idempotency.claim(claim_for("u-1", "a-3"), AT)

    assert (stored.action_id, stored.status) == ("a-1", RecordStatus.COMPLETED)
    assert stored.result == {"kind": "error", "code": "limit_exceeded", "details": {"allowed": "0.10"}}
    async with uow(write=True) as tx:
        assert await tx.idempotency.release(mine) is True
        assert (await tx.idempotency.claim(claim_for("u-1", "a-4"), AT)).action_id == "a-4"


@pytest.mark.req("FR-33")
async def test_the_same_key_of_two_users_is_two_records(uow: SqlUnitOfWorkFactory) -> None:
    await add_user(uow, "u-1")
    await add_user(uow, "u-2")
    async with uow(write=True) as tx:
        one = await tx.idempotency.claim(claim_for("u-1", "a-1"), AT)
        two = await tx.idempotency.claim(claim_for("u-2", "a-2"), AT)
        again = await tx.idempotency.claim(claim_for("u-1", "a-3"), AT)
        released = await tx.idempotency.release(two)
        still = await tx.idempotency.claim(claim_for("u-1", "a-4"), AT)

    assert (one.action_id, two.action_id) == ("a-1", "a-2")
    assert (again.action_id, again.user_id) == ("a-1", "u-1")  # never the other user's record
    assert released is True
    assert still.action_id == "a-1"


def test_an_answered_record_must_carry_its_answer(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (id, name, status, environment, created_at) "
            "VALUES ('u', 'u', 'active', 'testnet', 0)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO idempotency_records VALUES ('u','explicit','k',?, 'a','completed',NULL,0,1)",
                ("a" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO idempotency_records VALUES ('u','explicit','k',?, 'a','in_progress','{}',0,1)",
                ("a" * 64,),
            )
    finally:
        conn.close()


# --- the use case with the Bybit adapter and real SQLite ---------------------------------------


@dataclass
class Trader:
    user_id: str
    stub: Stub
    db_path: Path
    cipher: AesGcmCredentialCipher

    def place_order(
        self, uow: SqlUnitOfWorkFactory, handler: Handler | Stub | None = None
    ) -> SubmitPlaceOrder:
        """One client of the trader: its own pool on the database, the same exchange account."""
        clock = SystemClock()
        bybit = client(self.stub) if handler is None else _client(handler)
        scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
        return SubmitPlaceOrder(
            uow,
            RequireTradingCredential(
                uow, self.cipher, RecheckCredential(uow, self.cipher, FakeKeyReader(), clock), clock, **scope
            ),
            InstrumentRulesCache(bybit, clock),
            market=bybit,
            trading=bybit,
            clock=clock,
            ids=SequentialIds(f"{id(uow):x}"[-12:]),
            reconnaissance_delay_s=0,
            **scope,
        )

    def record(self) -> tuple[str, str]:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT status, action_id FROM idempotency_records "
                "WHERE user_id = ? AND key_kind = 'explicit'",
                (self.user_id,),
            ).fetchone()
        finally:
            conn.close()
        return (str(row[0]), str(row[1]))

    def outcomes(self) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT payload FROM audit_log "
                "WHERE chain_id = ? AND event_type = 'order_outcome' ORDER BY seq",
                (self.user_id,),
            ).fetchall()
        finally:
            conn.close()
        return [json.loads(raw) for (raw,) in rows]


def _client(handler: Handler | Stub) -> BybitClient:
    http = make_http_client(
        "https://api-demo.bybit.com",
        connect_timeout_s=1,
        read_timeout_s=1,
        transport=httpx2.MockTransport(handler),
    )
    return BybitClient(http, retry=RetryPolicy(max_attempts=2, base_delay_s=0), now_ms=lambda: NOW_MS)


async def trader(uow: SqlUnitOfWorkFactory, db_path: Path) -> Trader:
    clock, cipher = SystemClock(), AesGcmCredentialCipher(Keyring.single(os.urandom(32)))
    user = new_user("trader-1", "local", clock.now())
    credential = Credential(
        id="cred-1",
        user_id=user.id,
        exchange=EXCHANGE_BYBIT,
        environment=Environment.TESTNET,
        status=CredentialStatus.ACTIVE,
        permissions=TRADING_KEY,
        permissions_checked_at=clock.now(),
        admin_attested=False,
        created_at=clock.now(),
    )
    async with uow(write=True) as tx:
        await tx.users.add(user)
        await tx.policies.save(
            user.id, PolicyRecord(ceiling=trading_ceiling(), overrides=NO_OVERRIDES, updated_at=clock.now())
        )
        await tx.credentials.add(credential, cipher.seal(KEYS, credential.binding))
    stub = Stub(override={"/v5/order/realtime": EMPTY_ORDERS, "/v5/order/history": EMPTY_ORDERS})
    return Trader(user.id, stub, db_path, cipher)


KEYED_BUY = replace(MARKET_BUY, idempotency_key=KEY)


async def test_two_clients_with_one_key_send_exactly_one_order(
    uow: SqlUnitOfWorkFactory, second_uow: SqlUnitOfWorkFactory, db_path: Path
) -> None:
    """While client A's order is on its way, client B repeats the call: B is told "in progress" and
    sends nothing; afterwards B gets A's answer."""
    me = await trader(uow, db_path)
    entered, release = anyio.Event(), anyio.Event()

    async def slow_create(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v5/order/create":
            entered.set()
            await release.wait()
        return me.stub(request)

    client_a, client_b = me.place_order(uow, slow_create), me.place_order(second_uow)
    results: list[OrderResult] = []

    async with anyio.create_task_group() as tasks:

        async def first() -> None:
            results.append(await client_a(me.user_id, KEYED_BUY))

        tasks.start_soon(first)
        await entered.wait()
        with pytest.raises(OutcomeUnknown) as in_progress:
            await client_b(me.user_id, KEYED_BUY)
        release.set()

    replay = await client_b(me.user_id, KEYED_BUY)

    assert len(me.stub.posts()) == 1
    assert in_progress.value.details["reason"] == "in_progress"
    assert (results[0].source, replay.source) == (ResultSource.PLACED, ResultSource.REPLAY)
    assert replay.order_id == results[0].order_id
    assert me.record() == ("completed", results[0].action_id)


async def test_two_clients_racing_with_one_key_send_exactly_one_order(
    uow: SqlUnitOfWorkFactory, second_uow: SqlUnitOfWorkFactory, db_path: Path
) -> None:
    """No ordering forced: both calls start together, on two pools of one SQLite file."""
    me = await trader(uow, db_path)
    clients = (me.place_order(uow), me.place_order(second_uow))
    answers: list[str] = []

    async def call(place: SubmitPlaceOrder) -> None:
        try:
            answers.append((await place(me.user_id, KEYED_BUY)).source.value)
        except OutcomeUnknown as exc:
            answers.append(exc.details["reason"])

    async with anyio.create_task_group() as tasks:
        for place in clients:
            tasks.start_soon(call, place)

    assert len(me.stub.posts()) == 1
    assert sorted(answers) in (["in_progress", "placed"], ["placed", "replay"])


@pytest.mark.req("FR-27")
async def test_edge_timeout_after_sending_then_reconnaissance_finds_the_order(
    uow: SqlUnitOfWorkFactory, db_path: Path
) -> None:
    me = await trader(uow, db_path)
    me.stub.override["/v5/order/create"] = httpx2.ReadTimeout("slow")
    me.stub.override["/v5/order/realtime"] = load_fixture(FOUND)  # the exchange did create it

    result = await me.place_order(uow)(me.user_id, KEYED_BUY)

    assert result.source is ResultSource.RECONNAISSANCE
    assert result.fill is not None
    assert (result.fill.status, result.order_id) == (
        FillStatus.FILLED,
        "fa7b555a-dad9-444c-8f67-e4fc7572ac65",
    )
    assert len(me.stub.posts()) == 1  # never resent
    [lookup] = [r for r in me.stub.requests if r.url.path == "/v5/order/realtime"]
    assert lookup.url.params["orderLinkId"] == result.client_order_id
    [outcome] = me.outcomes()
    assert (outcome["status"], outcome["reconnaissance"]) == ("accepted", "found")
    assert me.record()[0] == "completed"


@pytest.mark.req("FR-27")
async def test_an_order_found_only_in_the_history(uow: SqlUnitOfWorkFactory, db_path: Path) -> None:
    me = await trader(uow, db_path)
    me.stub.override["/v5/order/create"] = httpx2.Response(502)
    me.stub.override["/v5/order/history"] = load_fixture(FOUND)

    result = await me.place_order(uow)(me.user_id, KEYED_BUY)

    assert result.source is ResultSource.RECONNAISSANCE
    paths = [r.url.path for r in me.stub.requests if "orderLinkId" in r.url.params]
    assert paths == ["/v5/order/realtime", "/v5/order/history"]  # realtime first, then history


@pytest.mark.req("FR-27")
async def test_edge_timeout_after_sending_and_not_found_anywhere_is_unknown_and_never_resent(
    uow: SqlUnitOfWorkFactory, db_path: Path
) -> None:
    me = await trader(uow, db_path)
    me.stub.override["/v5/order/create"] = httpx2.ReadTimeout("slow")
    place = me.place_order(uow)

    with pytest.raises(OutcomeUnknown) as error:
        await place(me.user_id, KEYED_BUY)
    with pytest.raises(OutcomeUnknown) as repeat:
        await place(me.user_id, KEYED_BUY)

    assert error.value.details["reconnaissance"] == "not_found"
    assert repeat.value.details["replayed"] == "true"
    assert len(me.stub.posts()) == 1
    looked_up = [r.url.path for r in me.stub.requests if "orderLinkId" in r.url.params]
    assert looked_up == ["/v5/order/realtime", "/v5/order/history"]  # once: the repeat looks nothing up
    assert me.outcomes()[0]["status"] == "unknown"
    assert me.record()[0] == "unknown"


# --- the adapter --------------------------------------------------------------------------------


@pytest.mark.req("FR-27")
async def test_find_order_asks_realtime_then_history_by_client_id() -> None:
    stub = Stub(override={"/v5/order/realtime": EMPTY_ORDERS, "/v5/order/history": load_fixture(FOUND)})

    found = await client(stub).find_order(KEYS, Category.LINEAR, "BTCUSDT", CLIENT_ID)

    assert found is not None
    assert found.status is FillStatus.FILLED
    assert [(r.url.path, r.url.params["orderLinkId"]) for r in stub.requests] == [
        ("/v5/order/realtime", CLIENT_ID),
        ("/v5/order/history", CLIENT_ID),
    ]
    nowhere = Stub(override={"/v5/order/realtime": EMPTY_ORDERS, "/v5/order/history": EMPTY_ORDERS})
    assert await client(nowhere).find_order(KEYS, Category.LINEAR, "BTCUSDT", CLIENT_ID) is None


def _ret(code: int, message: str) -> dict[str, Any]:
    return {"retCode": code, "retMsg": message, "result": {}, "time": NOW_MS}


@pytest.mark.req("FR-27", "FR-28")
@pytest.mark.parametrize(
    ("code", "error", "reason"),
    [
        (99999, OutcomeUnknown, "unrecognised_exchange_code"),  # no documented family
        (131001, OutcomeUnknown, "unrecognised_exchange_code"),  # another product's family
        (10014, OutcomeUnknown, "exchange_internal_error"),  # "Invalid duplicate request"
        (110007, ExchangeRejected, None),  # insufficient balance: a documented refusal
        (170131, ExchangeRejected, None),  # spot: insufficient balance
        (10027, ExchangeRejected, None),  # transactions are banned
    ],
)
async def test_an_unrecognised_create_code_is_unknown_and_documented_refusals_stay_definite(
    code: int, error: type[Exception], reason: str | None
) -> None:
    """011 review proposal: unknown by default for a mutating POST, definite only for the documented
    refusal families (the use case then looks the order up instead of reporting "failed")."""
    stub = Stub(override={"/v5/order/create": _ret(code, "something")})

    with pytest.raises(error) as raised:
        await client(stub).place_order(KEYS, ioc_buy())

    if reason is not None:
        assert raised.value.details["reason"] == reason  # type: ignore[attr-defined]
    assert len(stub.posts()) == 1
