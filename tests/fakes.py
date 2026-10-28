"""In-memory fakes of application ports (no mocks of everything, ENGINEERING.md)."""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from guarded_exchange.domain.access_tokens import TOKEN_PREFIX, AccessToken
from guarded_exchange.domain.audit import AuditEvent, AuditRecord, canonical_json
from guarded_exchange.domain.credentials import (
    ApiKeyPair,
    Credential,
    CredentialBinding,
    CredentialStatus,
    CredentialUnreadable,
    KeyPermissions,
    SealedSecret,
)
from guarded_exchange.domain.errors import NotFound
from guarded_exchange.domain.instruments import Category, Instrument, InstrumentRules, InstrumentStatus
from guarded_exchange.domain.market import Candle, CandleInterval, Candles, Orderbook, OrderbookLevel, Ticker
from guarded_exchange.domain.policy import PolicyRecord
from guarded_exchange.domain.users import Environment, User, UserNameTaken, UserStatus


def make_ticker(symbol: str = "BTCUSDT", category: Category = Category.LINEAR) -> Ticker:
    linear = category is Category.LINEAR
    return Ticker(
        symbol=symbol,
        category=category,
        last_price=Decimal("80571.10"),
        bid_price=Decimal("80571.10"),
        ask_price=Decimal("80571.20"),
        high_24h=Decimal("80800.00"),
        low_24h=Decimal("79458.90"),
        change_24h_pct=Decimal("0.6848"),
        volume_24h_base=Decimal("43556.0370"),
        turnover_24h_quote=Decimal("3486773303.0305"),
        observed_at=datetime(2026, 10, 27, 18, 53, 43, 449000, tzinfo=UTC),
        mark_price=Decimal("80571.10") if linear else None,
        index_price=Decimal("80603.97") if linear else None,
        funding_rate_pct=Decimal("0.01") if linear else None,
        next_funding_at=datetime(2026, 10, 28, 0, 0, tzinfo=UTC) if linear else None,
    )


def make_rules(
    symbol: str = "BTCUSDT",
    category: Category = Category.LINEAR,
    *,
    base: str = "BTC",
    quote: str = "USDT",
    status: InstrumentStatus = InstrumentStatus.TRADING,
) -> InstrumentRules:
    linear = category is Category.LINEAR
    return InstrumentRules(
        instrument=Instrument(symbol=symbol, category=category, base=base, quote=quote),
        status=status,
        tick_size=Decimal("0.10") if linear else Decimal("0.01"),
        qty_step=Decimal("0.001") if linear else Decimal("0.00001"),
        min_qty=Decimal("0.001") if linear else Decimal("0.00001"),
        min_notional=Decimal(5),
        max_leverage=Decimal("100.00") if linear else None,
    )


_BOOK_AT = datetime(2026, 10, 28, 8, 59, 5, 159000, tzinfo=UTC)


def make_orderbook(
    symbol: str = "BTCUSDT", category: Category = Category.LINEAR, *, levels: int = 5
) -> Orderbook:
    """``levels`` per side around 80000, one tick (0.1) apart."""
    tick = Decimal("0.1")
    return Orderbook(
        symbol=symbol,
        category=category,
        bids=tuple(OrderbookLevel(Decimal("80000.0") - i * tick, Decimal("0.5")) for i in range(levels)),
        asks=tuple(OrderbookLevel(Decimal("80000.1") + i * tick, Decimal("0.25")) for i in range(levels)),
        observed_at=_BOOK_AT,
    )


def make_candles(
    symbol: str = "BTCUSDT",
    category: Category = Category.LINEAR,
    interval: CandleInterval = CandleInterval.H1,
    *,
    count: int = 3,
) -> Candles:
    """``count`` hourly candles ending at 2026-10-28 08:00 UTC, oldest first."""
    first = datetime(2026, 10, 28, 8, 0, tzinfo=UTC) - timedelta(hours=count - 1)
    return Candles(
        symbol=symbol,
        category=category,
        interval=interval,
        candles=tuple(
            Candle(
                start_at=first + timedelta(hours=i),
                open=Decimal(80000 + i),
                high=Decimal(80010 + i),
                low=Decimal(79990 + i),
                close=Decimal(80001 + i),
                volume_base=Decimal("12.5"),
                turnover_quote=Decimal("1000000.5"),
            )
            for i in range(count)
        ),
    )


@dataclass
class FakeExchange:
    """``ExchangeClient`` with configurable market data and an optional forced failure.

    ``calls`` records ``(method, symbol, category, *args)`` of every call; ``get_ticker`` calls are
    recorded as ``(symbol, category)`` for the tests written before the other methods existed.
    """

    tickers: dict[tuple[str, Category], Ticker] = field(default_factory=dict)
    orderbooks: dict[tuple[str, Category], Orderbook] = field(default_factory=dict)
    candles: dict[tuple[str, Category], Candles] = field(default_factory=dict)
    rules: dict[tuple[str, Category], InstrumentRules] = field(default_factory=dict)
    fail_with: Exception | None = None
    calls: list[tuple[object, ...]] = field(default_factory=list)

    async def get_ticker(self, symbol: str, category: Category) -> Ticker:
        self.calls.append((symbol, category))
        return self._lookup(self.tickers, symbol, category)

    async def get_orderbook(self, symbol: str, category: Category, depth: int) -> Orderbook:
        self.calls.append(("get_orderbook", symbol, category, depth))
        return self._lookup(self.orderbooks, symbol, category)

    async def get_candles(
        self, symbol: str, category: Category, interval: CandleInterval, limit: int
    ) -> Candles:
        self.calls.append(("get_candles", symbol, category, interval, limit))
        return self._lookup(self.candles, symbol, category)

    async def get_instrument_rules(self, symbol: str, category: Category) -> InstrumentRules:
        self.calls.append(("get_instrument_rules", symbol, category))
        return self._lookup(self.rules, symbol, category)

    def _lookup[T](self, table: dict[tuple[str, Category], T], symbol: str, category: Category) -> T:
        if self.fail_with is not None:
            raise self.fail_with
        try:
            return table[(symbol, category)]
        except KeyError:
            raise NotFound(f"no {category.value} instrument {symbol}") from None


# --- storage, clock, ids ---------------------------------------------------------------------


@dataclass
class FakeClock:
    """``Clock`` that stands still until moved. Starts at a whole millisecond, like the real one."""

    current: datetime = field(default_factory=lambda: datetime(2026, 10, 27, 12, 0, 0, 123000, tzinfo=UTC))

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@dataclass
class SequentialIds:
    prefix: str = "id"
    issued: int = 0

    def new_id(self) -> str:
        self.issued += 1
        return f"{self.prefix}-{self.issued:04d}"


@dataclass
class SequentialTokens:
    """Well-formed, predictable token secrets for tests."""

    issued: int = 0

    def new_token(self) -> str:
        self.issued += 1
        return f"{TOKEN_PREFIX}{self.issued:043d}"


@dataclass
class FakeMasterKeyStore:
    present: bool = False
    created: int = 0

    def exists(self) -> bool:
        return self.present

    def create(self) -> None:
        if self.present:
            raise RuntimeError("master key exists")
        self.present = True
        self.created += 1


class ReadOnlyViolation(RuntimeError):
    pass


@dataclass
class FakeKeyReader:
    """``KeyPermissionsReader``: returns ``permissions`` or raises ``fail_with``; records the keys asked."""

    permissions: KeyPermissions | None = None
    fail_with: Exception | None = None
    calls: list[ApiKeyPair] = field(default_factory=list)

    async def get_key_permissions(self, keys: ApiKeyPair) -> KeyPermissions:
        self.calls.append(keys)
        if self.fail_with is not None:
            raise self.fail_with
        assert self.permissions is not None, "configure FakeKeyReader.permissions"
        return self.permissions


class FakeCipher:
    """``CredentialCipher`` without cryptography: the binding must match, like AES-GCM's AAD."""

    def __init__(self) -> None:
        self._sealed: dict[bytes, tuple[CredentialBinding, ApiKeyPair]] = {}

    def seal(self, keys: ApiKeyPair, binding: CredentialBinding) -> SealedSecret:
        token = f"sealed-{len(self._sealed) + 1}".encode()
        self._sealed[token] = (binding, keys)
        return SealedSecret(ciphertext=token, nonce=b"n" * 12, key_version=1)

    def open(self, sealed: SealedSecret, binding: CredentialBinding) -> ApiKeyPair:
        stored = self._sealed.get(sealed.ciphertext)
        if stored is None or stored[0] != binding:
            raise CredentialUnreadable()
        return stored[1]


@dataclass
class _State:
    users: dict[str, User] = field(default_factory=dict)
    audit: dict[str, list[AuditRecord]] = field(default_factory=dict)
    policies: dict[str, PolicyRecord] = field(default_factory=dict)
    access_tokens: dict[str, AccessToken] = field(default_factory=dict)
    credentials: dict[str, tuple[Credential, SealedSecret]] = field(default_factory=dict)

    def copy(self) -> "_State":
        return _State(
            dict(self.users),
            {chain: list(records) for chain, records in self.audit.items()},
            dict(self.policies),
            dict(self.access_tokens),
            dict(self.credentials),
        )


class InMemoryUserRepository:
    def __init__(self, state: _State, *, writable: bool) -> None:
        self._state = state
        self._writable = writable

    async def add(self, user: User) -> None:
        if not self._writable:
            raise ReadOnlyViolation("users.add needs a write unit of work")
        if any(u.name == user.name for u in self._state.users.values()):
            raise UserNameTaken(user.name)
        if user.id in self._state.users:
            raise ValueError(f"duplicate user id {user.id}")
        self._state.users[user.id] = user

    async def get(self, user_id: str) -> User | None:
        return self._state.users.get(user_id)

    async def get_by_name(self, name: str) -> User | None:
        return next((u for u in self._state.users.values() if u.name == name), None)

    async def list(self) -> Sequence[User]:
        return sorted(self._state.users.values(), key=lambda u: u.name)


class InMemoryAccessTokenRepository:
    def __init__(self, state: _State, *, writable: bool) -> None:
        self._state = state
        self._writable = writable

    async def add(self, token: AccessToken) -> None:
        if not self._writable:
            raise ReadOnlyViolation("access_tokens.add needs a write unit of work")
        if token.id in self._state.access_tokens or any(
            t.token_hash == token.token_hash for t in self._state.access_tokens.values()
        ):
            raise ValueError(f"duplicate token {token.id}")
        if token.user_id not in self._state.users:
            raise ValueError(f"no user {token.user_id}")  # the SQL table has a foreign key
        self._state.access_tokens[token.id] = token

    async def get(self, token_id: str) -> AccessToken | None:
        return self._state.access_tokens.get(token_id)

    async def get_by_hash(self, token_hash: str) -> AccessToken | None:
        return next((t for t in self._state.access_tokens.values() if t.token_hash == token_hash), None)

    async def revoke(self, token_id: str, at: datetime) -> bool:
        if not self._writable:
            raise ReadOnlyViolation("access_tokens.revoke needs a write unit of work")
        token = self._state.access_tokens.get(token_id)
        if token is None or token.revoked_at is not None:
            return False
        self._state.access_tokens[token_id] = replace(token, revoked_at=at)
        return True


class InMemoryAuditLogRepository:
    def __init__(self, state: _State, *, writable: bool) -> None:
        self._state = state
        self._writable = writable

    async def append(self, chain_id: str, event: AuditEvent, at: datetime) -> AuditRecord:
        if not self._writable:
            raise ReadOnlyViolation("audit.append needs a write unit of work")
        chain = self._state.audit.setdefault(chain_id, [])
        record = AuditRecord(
            chain_id=chain_id,
            seq=len(chain) + 1,
            ts=at,
            event_type=str(event.event_type),
            actor=event.actor,
            payload=json.loads(canonical_json(event.payload)),
        )
        chain.append(record)
        return record

    async def list(self, chain_id: str, *, after_seq: int = 0, limit: int = 100) -> Sequence[AuditRecord]:
        return [r for r in self._state.audit.get(chain_id, []) if r.seq > after_seq][:limit]


class InMemoryPolicyRepository:
    def __init__(self, state: _State, *, writable: bool) -> None:
        self._state = state
        self._writable = writable

    async def get(self, user_id: str) -> PolicyRecord | None:
        return self._state.policies.get(user_id)

    async def save(self, user_id: str, record: PolicyRecord) -> None:
        if not self._writable:
            raise ReadOnlyViolation("policies.save needs a write unit of work")
        if user_id not in self._state.users:
            raise ValueError(f"no user {user_id}")  # the SQL table has a foreign key
        self._state.policies[user_id] = record


class InMemoryCredentialRepository:
    def __init__(self, state: _State, *, writable: bool) -> None:
        self._state = state
        self._writable = writable

    def _write(self, operation: str) -> None:
        if not self._writable:
            raise ReadOnlyViolation(f"credentials.{operation} needs a write unit of work")

    async def add(self, credential: Credential, sealed: SealedSecret) -> None:
        self._write("add")
        if credential.user_id not in self._state.users:
            raise ValueError(f"no user {credential.user_id}")  # the SQL table has a foreign key
        if credential.is_active and await self.get_active(
            credential.user_id, credential.exchange, credential.environment
        ):
            raise ValueError("a second active credential violates the unique index")  # D-12
        self._state.credentials[credential.id] = (credential, sealed)

    async def get(self, credential_id: str) -> Credential | None:
        stored = self._state.credentials.get(credential_id)
        return None if stored is None else stored[0]

    async def get_active(self, user_id: str, exchange: str, environment: Environment) -> Credential | None:
        return next(
            (
                c
                for c, _ in self._state.credentials.values()
                if c.user_id == user_id
                and c.exchange == exchange
                and c.environment is environment
                and c.is_active
            ),
            None,
        )

    async def list_for_user(self, user_id: str) -> Sequence[Credential]:
        return [c for c, _ in self._state.credentials.values() if c.user_id == user_id]

    async def list_active(self, exchange: str, environment: Environment) -> Sequence[Credential]:
        return [
            c
            for c, _ in self._state.credentials.values()
            if c.exchange == exchange and c.environment is environment and c.is_active
        ]

    async def get_sealed(self, credential_id: str) -> SealedSecret | None:
        stored = self._state.credentials.get(credential_id)
        return None if stored is None else stored[1]

    async def get_sealed_active(self, credential_id: str) -> SealedSecret | None:
        stored = self._state.credentials.get(credential_id)
        return None if stored is None or not stored[0].is_active else stored[1]

    async def update_permissions(
        self, credential_id: str, permissions: KeyPermissions, checked_at: datetime
    ) -> None:
        self._write("update_permissions")
        credential, sealed = self._state.credentials[credential_id]
        updated = replace(credential, permissions=permissions, permissions_checked_at=checked_at)
        self._state.credentials[credential_id] = (updated, sealed)

    async def mark_deleted(self, credential_id: str, at: datetime) -> bool:
        self._write("mark_deleted")
        credential, sealed = self._state.credentials[credential_id]
        if not credential.is_active:
            return False
        deleted = replace(credential, status=CredentialStatus.DELETED, deleted_at=at)
        self._state.credentials[credential_id] = (deleted, sealed)
        return True


class InMemoryUnitOfWork:
    def __init__(self, state: _State, *, write: bool, locks: list[str]) -> None:
        self.users = InMemoryUserRepository(state, writable=write)
        self.audit = InMemoryAuditLogRepository(state, writable=write)
        self.policies = InMemoryPolicyRepository(state, writable=write)
        self.access_tokens = InMemoryAccessTokenRepository(state, writable=write)
        self.credentials = InMemoryCredentialRepository(state, writable=write)
        self._write = write
        self._locks = locks

    async def lock_user(self, user_id: str) -> None:
        if not self._write:
            raise ReadOnlyViolation(f"lock_user({user_id!r}) needs a write unit of work")
        self._locks.append(user_id)


class InMemoryDatabase:
    """``UnitOfWorkFactory`` with transaction semantics: commit on success, discard on error."""

    def __init__(self) -> None:
        self.state = _State()
        self.transactions: list[bool] = []  # write flag of every unit of work opened
        self.locked_users: list[str] = []  # every lock_user call, in order

    @asynccontextmanager
    async def __call__(self, *, write: bool) -> AsyncIterator[InMemoryUnitOfWork]:
        self.transactions.append(write)
        working = self.state.copy()
        yield InMemoryUnitOfWork(working, write=write, locks=self.locked_users)
        if write:
            self.state = working

    def all_audit(self) -> list[AuditRecord]:
        return [r for chain in self.state.audit.values() for r in chain]

    def add_user(
        self,
        name: str = "local",
        *,
        environment: Environment = Environment.TESTNET,
        status: UserStatus = UserStatus.ACTIVE,
    ) -> User:
        """Seed a user directly (as ``init`` would), without journal records."""
        user = User(
            id=f"user-{name}",
            name=name,
            status=status,
            environment=environment,
            created_at=datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
        )
        self.state.users[user.id] = user
        return user
