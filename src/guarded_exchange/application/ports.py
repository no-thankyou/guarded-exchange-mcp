"""Ports the application layer depends on. Adapters live in ``infrastructure``.

The exchange port speaks domain language only (NFR-5, PLAN ADR-002/D-26): no Bybit field
names, codes or URLs. Implementations must raise ``guarded_exchange.domain.errors``
exceptions, never transport-specific ones.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from guarded_exchange.domain.audit import AuditEvent, AuditRecord
from guarded_exchange.domain.instruments import Category, InstrumentRules
from guarded_exchange.domain.market import CandleInterval, Candles, Orderbook, Ticker
from guarded_exchange.domain.users import User


class ExchangeClient(Protocol):
    """Read and (later) trading access to one exchange account or public market data."""

    async def get_ticker(self, symbol: str, category: Category) -> Ticker:
        """Return the current ticker.

        Raises:
            NotFound: the instrument does not exist in this category.
            InvalidParams: the exchange rejected the parameters.
            ExchangeUnavailable: the exchange could not be reached or failed transiently.
            ExchangeRejected: any other refusal by the exchange.
        """
        ...

    async def get_orderbook(self, symbol: str, category: Category, depth: int) -> Orderbook:
        """Return at least the best ``depth`` levels per side (callers cut to exactly ``depth``).

        Raises the same exceptions as ``get_ticker``.
        """
        ...

    async def get_candles(
        self, symbol: str, category: Category, interval: CandleInterval, limit: int
    ) -> Candles:
        """Return up to ``limit`` most recent candles, oldest first. No trades yet -> empty series.

        Raises the same exceptions as ``get_ticker``.
        """
        ...

    async def get_instrument_rules(self, symbol: str, category: Category) -> InstrumentRules:
        """Return the trading rules of one instrument, whatever its quote currency.

        Raises the same exceptions as ``get_ticker``; an instrument the adapter cannot
        represent in the domain (e.g. a dated future in ``linear``) raises ``InvalidParams``.
        """
        ...


class Clock(Protocol):
    """The only source of "now" for use cases and repositories (PLAN D-10), never the database."""

    def now(self) -> datetime:
        """Timezone-aware UTC time truncated to whole milliseconds (the storage precision)."""
        ...


class IdGenerator(Protocol):
    def new_id(self) -> str:
        """A new unique, unguessable identifier (at least 122 random bits)."""
        ...


class UserRepository(Protocol):
    async def add(self, user: User) -> None:
        """Insert a new user. A duplicate id or name is a programming error (raises)."""
        ...

    async def get(self, user_id: str) -> User | None: ...

    async def get_by_name(self, name: str) -> User | None: ...


class AuditLogRepository(Protocol):
    """Append-only journal (FR-37): there is deliberately no update or delete."""

    async def append(self, chain_id: str, event: AuditEvent, at: datetime) -> AuditRecord:
        """Append ``event`` to ``chain_id`` with the next ``seq`` (gapless, from 1)."""
        ...

    async def list(self, chain_id: str, *, after_seq: int = 0, limit: int = 100) -> Sequence[AuditRecord]:
        """Records of one chain with ``seq > after_seq`` in ``seq`` order, at most ``limit``."""
        ...


class MasterKeyStore(Protocol):
    """Where the local master key lives (PLAN ADR-009). Never exposes the key to use cases."""

    def exists(self) -> bool:
        """``True`` if a usable key is present. An unsafe or corrupt key raises instead."""
        ...

    def create(self) -> None:
        """Generate and persist a new key atomically. Never overwrites an existing one (raises)."""
        ...


class UnitOfWork(Protocol):
    """Repositories bound to one database transaction."""

    @property
    def users(self) -> UserRepository: ...

    @property
    def audit(self) -> AuditLogRepository: ...


class UnitOfWorkFactory(Protocol):
    """Opens a transaction: committed when the block exits normally, rolled back on exception.

    ``write=True`` takes the write lock at the start (SQLite ``BEGIN IMMEDIATE``), so a
    read-then-write transaction cannot fail half-way on lock upgrade; ``write=False`` is a
    read-only snapshot and any write through it raises.
    """

    def __call__(self, *, write: bool) -> AbstractAsyncContextManager[UnitOfWork]: ...
