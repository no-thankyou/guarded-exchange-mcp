"""In-memory fakes of application ports (no mocks of everything, ENGINEERING.md)."""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.read_market import GetCandles, GetInstrumentRules, GetOrderbook, GetTicker
from guarded_exchange.domain.audit import AuditEvent, AuditRecord, canonical_json
from guarded_exchange.domain.errors import NotFound
from guarded_exchange.domain.instruments import Category, Instrument, InstrumentRules, InstrumentStatus
from guarded_exchange.domain.market import Candle, CandleInterval, Candles, Orderbook, OrderbookLevel, Ticker
from guarded_exchange.domain.users import User
from guarded_exchange.interface.mcp.server import UseCases


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


def use_cases_on(exchange: FakeExchange, clock: "FakeClock | None" = None) -> UseCases:
    """The MCP use cases wired to a fake exchange, as the composition root wires the real one."""
    rules = InstrumentRulesCache(exchange, clock or FakeClock())
    return UseCases(
        get_ticker=GetTicker(exchange),
        get_orderbook=GetOrderbook(exchange),
        get_candles=GetCandles(exchange),
        get_instrument_rules=GetInstrumentRules(rules),
    )


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
class _State:
    users: dict[str, User] = field(default_factory=dict)
    audit: dict[str, list[AuditRecord]] = field(default_factory=dict)

    def copy(self) -> "_State":
        return _State(dict(self.users), {chain: list(records) for chain, records in self.audit.items()})


class InMemoryUserRepository:
    def __init__(self, state: _State, *, writable: bool) -> None:
        self._state = state
        self._writable = writable

    async def add(self, user: User) -> None:
        if not self._writable:
            raise ReadOnlyViolation("users.add needs a write unit of work")
        if user.id in self._state.users or any(u.name == user.name for u in self._state.users.values()):
            raise ValueError(f"duplicate user {user.id}/{user.name}")
        self._state.users[user.id] = user

    async def get(self, user_id: str) -> User | None:
        return self._state.users.get(user_id)

    async def get_by_name(self, name: str) -> User | None:
        return next((u for u in self._state.users.values() if u.name == name), None)


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


class InMemoryUnitOfWork:
    def __init__(self, state: _State, *, write: bool) -> None:
        self.users = InMemoryUserRepository(state, writable=write)
        self.audit = InMemoryAuditLogRepository(state, writable=write)


class InMemoryDatabase:
    """``UnitOfWorkFactory`` with transaction semantics: commit on success, discard on error."""

    def __init__(self) -> None:
        self.state = _State()
        self.transactions: list[bool] = []  # write flag of every unit of work opened

    @asynccontextmanager
    async def __call__(self, *, write: bool) -> AsyncIterator[InMemoryUnitOfWork]:
        self.transactions.append(write)
        working = self.state.copy()
        yield InMemoryUnitOfWork(working, write=write)
        if write:
            self.state = working

    def all_audit(self) -> list[AuditRecord]:
        return [r for chain in self.state.audit.values() for r in chain]
