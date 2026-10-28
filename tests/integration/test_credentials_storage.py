"""Exchange keys on real SQLite with real AES-GCM (ticket 007): AAD binding, secrets never stored or
logged in clear, the composition root's key commands, and the master key check of ``serve``."""

import copy
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest

from guarded_exchange import bootstrap
from guarded_exchange.application.credentials import (
    AddCredential,
    RecheckCredential,
    RequireTradingCredential,
)
from guarded_exchange.config import Settings
from guarded_exchange.domain.credentials import EXCHANGE_BYBIT, ApiKeyPair, KeyPermissions
from guarded_exchange.domain.errors import ExchangeCredentialsInvalid
from guarded_exchange.domain.users import Environment, new_user
from guarded_exchange.infrastructure.clock import SystemClock, UuidGenerator
from guarded_exchange.infrastructure.crypto import AesGcmCredentialCipher, Keyring
from guarded_exchange.infrastructure.db.uow import SqlUnitOfWorkFactory
from tests.conftest import load_fixture
from tests.fakes import FakeKeyReader
from tests.integration.conftest import BUSY_TIMEOUT_MS, make_settings

TRADING = frozenset({"ContractTrade.Order", "ContractTrade.Position", "Spot.SpotTrade"})
OK = KeyPermissions(read_only=False, permissions=TRADING, expires_at=None, ip_restricted=False)
ALICE_KEYS = ApiKeyPair(api_key="ALICEKEY01234567", api_secret="alice-secret-0123456789abcdefghij")  # noqa: S106 - test value
BOB_KEYS = ApiKeyPair(api_key="BOBKEY0123456789X", api_secret="bob-secret-0123456789abcdefghijkl")  # noqa: S106 - test value


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _rows(db: Path, sql: str, *params: object) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.mark.req("FR-31")
async def test_a_ciphertext_copied_into_another_users_row_does_not_decrypt(
    uow: SqlUnitOfWorkFactory, db_path: Path
) -> None:
    async with uow(write=True) as tx:
        for name in ("alice", "bob"):
            await tx.users.add(new_user(f"u-{name}", name, SystemClock().now()))
    cipher = AesGcmCredentialCipher(Keyring.single(bytes(range(32))))
    reader, clock = FakeKeyReader(permissions=OK), SystemClock()
    scope: dict[str, Any] = {"exchange": EXCHANGE_BYBIT, "environment": Environment.TESTNET}
    add = AddCredential(uow, cipher, reader, clock, UuidGenerator(), **scope)
    alice = await add("alice", ALICE_KEYS)
    bob = await add("bob", BOB_KEYS)
    require = RequireTradingCredential(
        uow, cipher, RecheckCredential(uow, cipher, reader, clock), clock, **scope
    )
    assert (await require("u-bob")).keys == BOB_KEYS

    conn = sqlite3.connect(db_path)  # an attacker with write access to the database file
    conn.execute(
        "UPDATE exchange_credentials SET (ciphertext, nonce) = "
        "(SELECT ciphertext, nonce FROM exchange_credentials WHERE id = ?) WHERE id = ?",
        (alice.credential.id, bob.credential.id),
    )
    conn.commit()
    conn.close()

    with pytest.raises(ExchangeCredentialsInvalid, match="cannot be decrypted"):
        await require("u-bob")
    assert (await require("u-alice")).keys == ALICE_KEYS


@pytest.mark.req("FR-29")
async def test_the_database_allows_one_active_key_per_user_and_environment(
    uow: SqlUnitOfWorkFactory, db_path: Path
) -> None:
    async with uow(write=True) as tx:
        await tx.users.add(new_user("u-alice", "alice", SystemClock().now()))
    insert = (
        "INSERT INTO exchange_credentials (id, user_id, exchange, environment, ciphertext, nonce, "
        "key_version, admin_attested, status, created_at, deleted_at) "
        "VALUES (?, 'u-alice', 'bybit', ?, x'00', ?, 1, 0, ?, 0, ?)"
    )
    nonce = b"\x00" * 12
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(insert, ("c-1", "testnet", nonce, "active", None))
        conn.execute(insert, ("c-2", "testnet", nonce, "deleted", 1))  # deleted ones do not count
        conn.execute(insert, ("c-3", "mainnet", nonce, "active", None))  # another environment (D-12)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(insert, ("c-4", "testnet", nonce, "active", None))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(insert, ("c-5", "testnet", b"\x00" * 8, "deleted", 1))  # nonce must be 96 bits
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(insert, ("c-6", "testnet", nonce, "deleted", None))  # deleted needs deleted_at
    finally:
        conn.close()


# --- the composition root: key commands with a mocked exchange ------------------------------


def _env_file(tmp_path: Path, settings: Settings, *extra: str) -> Path:
    env = tmp_path / "gx.env"
    lines = [
        f"GX_DB_PATH={settings.db_path}",
        f"GX_MASTER_KEY_FILE={settings.master_key_file}",
        f"GX_SQLITE_BUSY_TIMEOUT_MS={BUSY_TIMEOUT_MS}",
        "GX_LOG_LEVEL=DEBUG",  # the most verbose level: every library logs whatever it logs
        *extra,
    ]
    env.write_text("\n".join(lines) + "\n")
    return env


class QueryApi:
    """A fake ``query-api`` that, like the real one, echoes the key and its secret in the response."""

    def __init__(self) -> None:
        self.permissions: dict[str, list[str]] = {
            "ContractTrade": ["Order", "Position"],
            "Spot": ["SpotTrade"],
        }
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        body = copy.deepcopy(load_fixture("bybit/spike004/demo_query_api.json"))
        api_key = request.headers["X-BAPI-API-KEY"]
        secret = next(k.api_secret for k in (ALICE_KEYS, BOB_KEYS) if k.api_key == api_key)
        body["result"].update(apiKey=api_key, secret=secret, permissions=self.permissions, ips=[])
        return httpx2.Response(200, json=body)


@pytest.mark.req("FR-31", "FR-30", "FR-49")
def test_secrets_appear_in_no_log_no_output_no_journal_and_no_database_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    env = _env_file(tmp_path, settings)
    exchange = QueryApi()
    transport = httpx2.MockTransport(exchange)

    added = bootstrap.add_key(env, None, ALICE_KEYS, False, transport=transport)
    exchange.permissions = {**exchange.permissions, "Wallet": ["AccountTransfer"]}
    with pytest.raises(SystemExit) as refused:
        bootstrap.replace_key(env, None, BOB_KEYS, False, transport=transport)
    exchange.permissions = {"ContractTrade": ["Order", "Position"]}
    replaced = bootstrap.replace_key(env, None, BOB_KEYS, False, transport=transport)
    _, listed = bootstrap.show_keys(env, None)

    async def recheck() -> None:
        async with bootstrap.open_storage(settings) as uow:
            await bootstrap.recheck_credentials_on_start(
                settings, uow, bootstrap.load_keyring(settings), transport=transport
            )

    anyio.run(recheck)
    deleted = bootstrap.delete_key(env, None)

    assert refused.value.code == 1
    assert added.assessment.profile.value == "trade"
    assert replaced.replaced_id == added.credential.id
    assert [c.id for c in listed] == [added.credential.id, replaced.credential.id]
    assert deleted.id == replaced.credential.id
    assert len(exchange.requests) == 4  # add, refused replace, replace, start-up re-check
    out, err = capsys.readouterr()
    assert "Wallet.AccountTransfer" in err  # the refusal says why ...
    assert '"event": "credential_rechecked"' in err
    assert "/v5/user/query-api" in err  # library logs (httpx2 request lines) were captured too
    secrets = [ALICE_KEYS.api_key, ALICE_KEYS.api_secret, BOB_KEYS.api_key, BOB_KEYS.api_secret]
    files = [p for p in settings.db_path.parent.iterdir() if p.is_file()]  # db, -wal, -shm
    assert settings.db_path in files
    journal = _rows(
        settings.db_path, "SELECT event_type, actor, payload FROM audit_log ORDER BY chain_id, seq"
    )
    assert [e for e, *_ in journal if e.startswith("credential_")] == [
        "credential_added",
        "credential_rejected",
        "credential_replaced",
        "credential_deleted",
    ]
    for secret in secrets:  # ... and nothing leaks anywhere
        assert secret not in out
        assert secret not in err
        assert secret not in repr(journal)
        for path in files:
            assert secret.encode() not in path.read_bytes(), path.name
    stored = _rows(settings.db_path, "SELECT ciphertext, nonce, key_version FROM exchange_credentials")
    assert len(stored) == 2
    assert all(len(nonce) == 12 and version == 1 for _, nonce, version in stored)


@pytest.mark.req("FR-49", "NFR-1")
def test_key_commands_refuse_without_a_usable_master_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    settings.master_key_file.chmod(0o644)
    exchange = QueryApi()

    with pytest.raises(SystemExit) as exit_info:
        bootstrap.add_key(
            _env_file(tmp_path, settings), None, ALICE_KEYS, False, transport=httpx2.MockTransport(exchange)
        )

    assert exit_info.value.code == 1
    assert "cannot use the master key" in capsys.readouterr().err
    assert exchange.requests == []  # nothing was sent anywhere


# --- serve refuses to start without a usable master key (ticket 005 review m5) --------------


def _missing(settings: Settings) -> None:
    settings.master_key_file.unlink()


def _too_open(settings: Settings) -> None:
    settings.master_key_file.chmod(0o644)


def _corrupt(settings: Settings) -> None:
    settings.master_key_file.write_bytes(b"definitely not 32 base64url bytes\n")


def _symlink(settings: Settings) -> None:
    real = settings.master_key_file.with_name("real.key")
    settings.master_key_file.rename(real)
    settings.master_key_file.symlink_to(real)


@pytest.mark.req("NFR-1")
@pytest.mark.parametrize(
    ("break_key", "reason"),
    [
        pytest.param(_missing, "master key not found", id="missing"),
        pytest.param(_too_open, "accessible by group/others (mode 644)", id="mode-644"),
        pytest.param(_corrupt, "is corrupt", id="corrupt"),
        pytest.param(_symlink, "cannot be opened safely", id="symlink"),
    ],
)
def test_serve_does_not_start_without_a_usable_master_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], break_key: Any, reason: str
) -> None:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    break_key(settings)

    with pytest.raises(SystemExit) as exit_info:
        bootstrap.serve("stdio", _env_file(tmp_path, settings))

    assert exit_info.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""  # stdout belongs to the MCP protocol
    assert "guarded-exchange: cannot start:" in err
    assert reason in err
    assert "Traceback" not in err


@pytest.mark.req("NFR-1")
@pytest.mark.parametrize(
    "line",
    ["GX_BYBIT_BASE_URL=https://evil.example", "GX_BYBIT_BASE_URL=http://api-demo.bybit.com"],
)
def test_serve_refuses_an_unknown_or_plain_http_exchange_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], line: str
) -> None:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)

    with pytest.raises(SystemExit) as exit_info:
        bootstrap.serve("stdio", _env_file(tmp_path, settings, line))

    assert exit_info.value.code == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "guarded-exchange: configuration error:" in err
    assert "GX_BYBIT_BASE_URL must be one of" in err


@pytest.mark.req("FR-29", "FR-30")
def test_keys_show_and_delete_work_without_a_usable_master_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)
    env = _env_file(tmp_path, settings)
    added = bootstrap.add_key(env, None, ALICE_KEYS, False, transport=httpx2.MockTransport(QueryApi()))
    settings.master_key_file.write_bytes(b"broken\n")  # e.g. a bad restore: delete must still work

    _, listed = bootstrap.show_keys(env, None)
    deleted = bootstrap.delete_key(env, None)

    assert [c.id for c in listed] == [added.credential.id]
    assert deleted.id == added.credential.id
    assert _rows(settings.db_path, "SELECT status FROM exchange_credentials") == [("deleted",)]


@pytest.mark.req("NFR-1")
@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_failing_server_still_lets_the_start_up_recheck_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    bootstrap.run_init(settings)

    class Crash(Exception):
        pass

    async def crash(*_: Any, **__: Any) -> None:
        raise Crash

    monkeypatch.setattr(bootstrap, "serve_http", crash)
    rechecked: list[bool] = []
    original = bootstrap.recheck_credentials_on_start

    async def recording(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        rechecked.append(True)

    monkeypatch.setattr(bootstrap, "recheck_credentials_on_start", recording)

    with pytest.raises(BaseException) as raised:  # noqa: PT011 - a task group may wrap it
        bootstrap.serve("http", _env_file(tmp_path, settings))

    assert (
        raised.group_contains(Crash) if isinstance(raised.value, BaseExceptionGroup) else raised.type is Crash
    )
    assert rechecked == [True]  # the background re-check ran to its end instead of being cut off
