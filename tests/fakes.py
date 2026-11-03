"""In-memory fakes of application ports (no mocks of everything, ENGINEERING.md)."""

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anyio
import anyio.lowlevel

from guarded_exchange.application.ports import CredentialCipher
from guarded_exchange.domain.access_tokens import TOKEN_PREFIX, AccessToken
from guarded_exchange.domain.account import (
    AccountBalances,
    CoinBalance,
    Order,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    Page,
    Position,
    PositionSide,
    QtyUnit,
    SymbolPosition,
    TimeInForce,
    UntrustedText,
)
from guarded_exchange.domain.audit import AuditEvent, AuditRecord, JsonValue, canonical_json
from guarded_exchange.domain.credentials import (
    EXCHANGE_BYBIT,
    ApiKeyPair,
    Credential,
    CredentialBinding,
    CredentialStatus,
    CredentialUnreadable,
    KeyPermissions,
    SealedSecret,
)
from guarded_exchange.domain.errors import NotFound
from guarded_exchange.domain.idempotency import IdempotencyRecord, KeyKind, RecordStatus
from guarded_exchange.domain.instruments import Category, Instrument, InstrumentRules, InstrumentStatus
from guarded_exchange.domain.market import Candle, CandleInterval, Candles, Orderbook, OrderbookLevel, Ticker
from guarded_exchange.domain.orders import OrderFill, OrderRequest, PlacedOrder, PriceBand
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


LISTED_BY_DEFAULT = frozenset({InstrumentStatus.TRADING, InstrumentStatus.PENDING_OPEN})


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
    listed_statuses: frozenset[InstrumentStatus] = frozenset(InstrumentStatus)
    """Statuses found without ``any_status``; Bybit lists only ``LISTED_BY_DEFAULT`` that way."""

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

    async def get_instrument_rules(
        self, symbol: str, category: Category, *, any_status: bool = False
    ) -> InstrumentRules:
        self.calls.append(
            ("get_instrument_rules", symbol, category, *(("any_status",) if any_status else ()))
        )
        rules = self._lookup(self.rules, symbol, category)
        if not any_status and rules.status not in self.listed_statuses:
            raise NotFound(f"no {category.value} instrument {symbol}")  # like Bybit without `status`
        return rules

    def _lookup[T](self, table: dict[tuple[str, Category], T], symbol: str, category: Category) -> T:
        if self.fail_with is not None:
            raise self.fail_with
        try:
            return table[(symbol, category)]
        except KeyError:
            raise NotFound(f"no {category.value} instrument {symbol}") from None


# --- account ---------------------------------------------------------------------------------

_ACCOUNT_AT = datetime(2026, 10, 28, 6, 26, 22, 702000, tzinfo=UTC)


def make_balances() -> AccountBalances:
    return AccountBalances(
        total_equity_usd=Decimal("4992.27725481"),
        total_wallet_balance_usd=Decimal("4992.20644097"),
        total_available_usd=Decimal("4992.20644097"),
        coins=(
            CoinBalance(
                coin="BTC",
                wallet_balance=Decimal("0.00000087"),
                equity=Decimal("0.00000087"),
                locked=Decimal(0),
                unrealised_pnl=Decimal(0),
                usd_value=Decimal("0.07081384"),
            ),
            CoinBalance(
                coin="USDT",
                wallet_balance=Decimal("4999.73103618"),
                equity=Decimal("4999.73103618"),
                locked=Decimal(0),
                unrealised_pnl=Decimal(0),
                usd_value=Decimal("4992.20644097"),
            ),
        ),
        observed_at=_ACCOUNT_AT,
    )


def make_position(symbol: str = "BTCUSDT", side: PositionSide = PositionSide.LONG) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        size=Decimal("0.010"),
        entry_price=Decimal("80938.4"),
        mark_price=Decimal("81020.1"),
        liquidation_price=Decimal("73101.2"),
        unrealised_pnl=Decimal("0.817"),
        position_value=Decimal("809.384"),
        leverage=Decimal(10),
        take_profit=None,
        stop_loss=Decimal("78510.0"),
        updated_at=_ACCOUNT_AT,
    )


def make_order(
    order_id: str = "fa7b555a-dad9-444c-8f67-e4fc7572ac65",
    *,
    category: Category = Category.LINEAR,
    client_order_id: str | None = "gx-0123456789abcdef0123456789abcdef",
    origin: OrderOrigin = OrderOrigin.SYSTEM,
) -> Order:
    return Order(
        order_id=order_id,
        symbol="BTCUSDT",
        category=category,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        status=OrderStatus.NEW,
        qty=Decimal("0.001"),
        qty_unit=QtyUnit.BASE,
        filled_qty=Decimal(0),
        price=Decimal("68797.6"),
        avg_fill_price=None,
        time_in_force=TimeInForce.GTC,
        reduce_only=False,
        conditional=None,
        trigger_price=None,
        take_profit=None,
        stop_loss=None,
        created_at=_ACCOUNT_AT,
        updated_at=_ACCOUNT_AT,
        origin=origin,
        client_order_id=None if client_order_id is None else UntrustedText(client_order_id),
    )


@dataclass
class FakeAccount:
    """``AccountReader`` returning configured pages; ``calls`` records ``(method, keys, *args)``.

    With ``db`` set, ``calls_in_transaction`` counts calls made while a unit of work of that
    database was open: talking to the exchange inside a transaction breaks D-10.
    """

    balances: AccountBalances = field(default_factory=make_balances)
    positions: tuple[Position, ...] = ()
    orders: dict[Category, tuple[Order, ...]] = field(default_factory=dict)
    omitted: int = 0
    """Reported as ``Page.omitted`` of positions and orders, as the adapter does for unsupported rows."""
    fail_with: Exception | None = None
    calls: list[tuple[object, ...]] = field(default_factory=list)
    db: "InMemoryDatabase | None" = None
    calls_in_transaction: int = 0

    async def get_balances(self, keys: ApiKeyPair) -> AccountBalances:
        self.calls.append(("get_balances", keys))
        self._maybe_fail()
        return self.balances

    async def get_positions(self, keys: ApiKeyPair, *, symbol: str | None, limit: int) -> Page[Position]:
        self.calls.append(("get_positions", keys, symbol, limit))
        self._maybe_fail()
        found = tuple(p for p in self.positions if symbol is None or p.symbol == symbol)
        return Page(items=found, has_more=False, omitted=self.omitted)  # not cut: the use case must cut

    async def get_open_orders(
        self, keys: ApiKeyPair, category: Category, *, symbol: str | None, limit: int
    ) -> Page[Order]:
        self.calls.append(("get_open_orders", keys, category, symbol, limit))
        self._maybe_fail()
        found = tuple(o for o in self.orders.get(category, ()) if symbol is None or o.symbol == symbol)
        return Page(items=found, has_more=False, omitted=self.omitted)

    def _maybe_fail(self) -> None:
        if self.db is not None and self.db.open_transactions:
            self.calls_in_transaction += 1
        if self.fail_with is not None:
            raise self.fail_with


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
    idempotency: dict[tuple[str, KeyKind, str], IdempotencyRecord] = field(default_factory=dict)

    def copy(self) -> "_State":
        return _State(
            dict(self.users),
            {chain: list(records) for chain, records in self.audit.items()},
            dict(self.policies),
            dict(self.access_tokens),
            dict(self.credentials),
            dict(self.idempotency),
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
    def __init__(
        self,
        state: _State,
        *,
        writable: bool,
        held: set[str] | None = None,
        appends: "list[tuple[str, str, bool]] | None" = None,
    ) -> None:
        self._state = state
        self._writable = writable
        self._held = held if held is not None else set()
        self._appends = appends if appends is not None else []

    async def append(self, chain_id: str, event: AuditEvent, at: datetime) -> AuditRecord:
        if not self._writable:
            raise ReadOnlyViolation("audit.append needs a write unit of work")
        # (chain, event type, was lock_user(chain) taken earlier in this same transaction)
        self._appends.append((chain_id, str(event.event_type), chain_id in self._held))
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


class InMemoryIdempotencyRepository:
    """Same semantics as the SQL repository: claim takes over only an expired record, every change is
    conditional on the holder's ``action_id``; results round-trip through canonical JSON."""

    def __init__(self, state: _State, *, writable: bool) -> None:
        self._state = state
        self._writable = writable

    def _write(self, operation: str) -> None:
        if not self._writable:
            raise ReadOnlyViolation(f"idempotency.{operation} needs a write unit of work")

    @staticmethod
    def _key(record: IdempotencyRecord) -> tuple[str, KeyKind, str]:
        return (record.user_id, record.kind, record.key)

    def _held(self, record: IdempotencyRecord) -> IdempotencyRecord | None:
        current = self._state.idempotency.get(self._key(record))
        return current if current is not None and current.action_id == record.action_id else None

    async def claim(self, record: IdempotencyRecord, now: datetime) -> IdempotencyRecord:
        self._write("claim")
        if record.user_id not in self._state.users:
            raise ValueError(f"no user {record.user_id}")  # the SQL table has a foreign key
        current = self._state.idempotency.get(self._key(record))
        if current is None or current.expires_at <= now:
            self._state.idempotency[self._key(record)] = replace(record, result=None)
            current = record
        for key, other in list(self._state.idempotency.items()):  # housekeeping, as the SQL one does
            if other.user_id == record.user_id and other.expires_at <= now:
                del self._state.idempotency[key]
        return current

    async def mark_sent(self, record: IdempotencyRecord) -> bool:
        self._write("mark_sent")
        held = self._held(record)
        if held is None or held.status is not RecordStatus.IN_PROGRESS:
            return False
        self._state.idempotency[self._key(record)] = replace(held, status=RecordStatus.SENT)
        return True

    async def finish(
        self,
        record: IdempotencyRecord,
        status: RecordStatus,
        result: Mapping[str, JsonValue],
        expires_at: datetime,
    ) -> bool:
        self._write("finish")
        held = self._held(record)
        if held is None:
            return False
        stored = json.loads(canonical_json(result))
        self._state.idempotency[self._key(record)] = replace(
            held, status=status, result=stored, expires_at=expires_at
        )
        return True

    async def release(self, record: IdempotencyRecord) -> bool:
        self._write("release")
        if self._held(record) is None:
            return False
        del self._state.idempotency[self._key(record)]
        return True


class InMemoryUnitOfWork:
    def __init__(
        self,
        state: _State,
        *,
        write: bool,
        locks: list[str],
        appends: "list[tuple[str, str, bool]] | None" = None,
    ) -> None:
        self._held: set[str] = set()  # users locked in this transaction
        self.users = InMemoryUserRepository(state, writable=write)
        self.audit = InMemoryAuditLogRepository(state, writable=write, held=self._held, appends=appends)
        self.policies = InMemoryPolicyRepository(state, writable=write)
        self.access_tokens = InMemoryAccessTokenRepository(state, writable=write)
        self.credentials = InMemoryCredentialRepository(state, writable=write)
        self.idempotency = InMemoryIdempotencyRepository(state, writable=write)
        self._write = write
        self._locks = locks

    async def lock_user(self, user_id: str) -> None:
        if not self._write:
            raise ReadOnlyViolation(f"lock_user({user_id!r}) needs a write unit of work")
        self._locks.append(user_id)
        self._held.add(user_id)


class InMemoryDatabase:
    """``UnitOfWorkFactory`` with transaction semantics: commit on success, discard on error."""

    def __init__(self) -> None:
        self.state = _State()
        self.transactions: list[bool] = []  # write flag of every unit of work opened
        self.locked_users: list[str] = []  # every lock_user call, in order
        self.open_transactions = 0  # > 0 while a unit of work is open (D-10: no network I/O then)
        self.audit_appends: list[tuple[str, str, bool]] = []
        """Every journal append: (chain, event type, lock_user(chain) held in that transaction)."""
        self.fail_writes: Exception | None = None
        """If set, opening a write unit of work raises it (e.g. ``database is locked``)."""
        self.checkpoint = False
        """Yield to the event loop on opening a transaction and after a commit, as a real driver does
        (cancellation points: a commit may complete and its ``await`` still be cancelled)."""
        self.after_commit: Callable[[], None] | None = None
        """Runs right after a write unit of work committed, before the checkpoint after it."""
        self._writer = anyio.Lock()
        """One writer at a time, as SQLite's ``BEGIN IMMEDIATE``: concurrent read-modify-write of the
        copied state would otherwise lose updates."""

    @asynccontextmanager
    async def __call__(self, *, write: bool) -> AsyncIterator[InMemoryUnitOfWork]:
        if not write:
            async with self._transaction(write=False) as uow:
                yield uow
            return
        async with self._writer, self._transaction(write=True) as uow:
            yield uow

    @asynccontextmanager
    async def _transaction(self, *, write: bool) -> AsyncIterator[InMemoryUnitOfWork]:
        if self.checkpoint:
            await anyio.lowlevel.checkpoint()
        if write and self.fail_writes is not None:
            raise self.fail_writes
        self.transactions.append(write)
        working = self.state.copy()
        self.open_transactions += 1
        try:
            yield InMemoryUnitOfWork(
                working, write=write, locks=self.locked_users, appends=self.audit_appends
            )
        finally:
            self.open_transactions -= 1
        if write:
            self.state = working
            if self.after_commit is not None:
                self.after_commit()
            if self.checkpoint:
                await anyio.lowlevel.checkpoint()

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

    def add_credential(
        self,
        user: User,
        cipher: CredentialCipher,
        keys: ApiKeyPair,
        *,
        permissions: KeyPermissions | None,
        status: CredentialStatus = CredentialStatus.ACTIVE,
        credential_id: str | None = None,
    ) -> Credential:
        """Seed a stored key directly (as ``keys add`` would), sealed with ``cipher``."""
        at = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        credential = Credential(
            id=credential_id or f"cred-{user.name}-{len(self.state.credentials) + 1}",
            user_id=user.id,
            exchange=EXCHANGE_BYBIT,
            environment=user.environment,
            status=status,
            permissions=permissions,
            permissions_checked_at=None if permissions is None else at,
            admin_attested=permissions is None,
            created_at=at,
            deleted_at=at if status is CredentialStatus.DELETED else None,
        )
        self.state.credentials[credential.id] = (credential, cipher.seal(keys, credential.binding))
        return credential


# --- trading ---------------------------------------------------------------------------------

BAND = PriceBand(buy_max=Decimal("81747.70"), sell_min=Decimal("80128.90"))


@dataclass
class FakeTrading:
    """``TradingExchange``: records orders; ``fills`` answers ``get_order`` by client order id.

    ``place_error`` makes ``place_order`` raise (after recording the request). With ``db`` set,
    ``calls_in_transaction`` counts calls made while a unit of work was open (D-10), and
    ``audit_at_send`` keeps the journal event types committed when each order was sent.
    """

    bands: dict[tuple[str, Category], PriceBand] = field(default_factory=dict)
    positions: dict[str, SymbolPosition] = field(default_factory=dict)
    fills: dict[str, OrderFill] = field(default_factory=dict)
    history: dict[str, OrderFill] = field(default_factory=dict)
    """What ``find_order`` finds in the order history (after ``fills``, the open/recent list)."""
    find_error: Exception | None = None
    hold_find: "anyio.Event | None" = None
    """If set, ``find_order`` waits for it: a reconnaissance that does not answer."""
    hold_band: "anyio.Event | None" = None
    """If set, ``get_price_band`` waits for it: a call stuck before its decision."""
    in_band: "anyio.Event" = field(default_factory=anyio.Event)
    """Set when ``get_price_band`` has been entered."""
    place_error: Exception | None = None
    read_error: Exception | None = None
    position_error: Exception | None = None
    sent: list[OrderRequest] = field(default_factory=list)
    calls: list[tuple[object, ...]] = field(default_factory=list)
    db: "InMemoryDatabase | None" = None
    calls_in_transaction: int = 0
    audit_at_send: list[list[str]] = field(default_factory=list)
    hold_place: "anyio.Event | None" = None
    """If set, ``place_order`` waits for it (after recording the request): a slow exchange."""
    placing: "anyio.Event" = field(default_factory=anyio.Event)
    """Set when ``place_order`` has been entered."""
    after_place: Callable[[], None] | None = None
    """Runs after an accepted ``place_order``: something that breaks right after the exchange said yes."""
    during_place: Callable[[], None] | None = None
    """Runs inside ``place_order`` before its answer (``place_error`` too): breaks while it is in flight."""
    before_band: Callable[[], None] | None = None
    """Runs inside ``get_price_band``: something that happens between the checks and the decision."""

    def client_order_id(self, action_id: str) -> str:
        return f"gx-{action_id}"

    async def get_price_band(self, symbol: str, category: Category) -> PriceBand:
        self._call("get_price_band", symbol, category)
        self.in_band.set()
        if self.hold_band is not None:
            hold, self.hold_band = self.hold_band, None  # only the first call waits
            await hold.wait()
        if self.before_band is not None:
            self.before_band()
        return self.bands.get((symbol, category), BAND)

    async def get_symbol_position(self, keys: ApiKeyPair, symbol: str) -> SymbolPosition:
        self._call("get_symbol_position", symbol)
        if self.position_error is not None:
            raise self.position_error
        return self.positions.get(symbol, SymbolPosition(symbol=symbol, position=None, leverage=Decimal(10)))

    async def place_order(self, keys: ApiKeyPair, order: OrderRequest) -> PlacedOrder:
        self._call("place_order", order.client_order_id)
        self.sent.append(order)
        if self.db is not None:
            self.audit_at_send.append([r.event_type for r in self.db.all_audit()])
        self.placing.set()
        if self.hold_place is not None:
            await self.hold_place.wait()
        if self.during_place is not None:
            self.during_place()
        if self.place_error is not None:
            raise self.place_error
        if self.after_place is not None:
            self.after_place()
        return PlacedOrder(order_id=f"order-{len(self.sent)}")

    async def get_order(
        self, keys: ApiKeyPair, category: Category, symbol: str, client_order_id: str
    ) -> OrderFill | None:
        self._call("get_order", category, symbol, client_order_id)
        if self.read_error is not None:
            raise self.read_error
        return self.fills.get(client_order_id)

    async def find_order(
        self, keys: ApiKeyPair, category: Category, symbol: str, client_order_id: str
    ) -> OrderFill | None:
        self._call("find_order", category, symbol, client_order_id)
        if self.hold_find is not None:
            await self.hold_find.wait()
        if self.find_error is not None:
            raise self.find_error
        return self.fills.get(client_order_id) or self.history.get(client_order_id)

    def _call(self, *call: object) -> None:
        self.calls.append(call)
        if self.db is not None and self.db.open_transactions:
            self.calls_in_transaction += 1
