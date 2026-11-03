"""Ports the application layer depends on. Adapters live in ``infrastructure``.

The exchange port speaks domain language only (NFR-5, PLAN ADR-002/D-26): no Bybit field
names, codes or URLs. Implementations must raise ``guarded_exchange.domain.errors``
exceptions, never transport-specific ones.
"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from guarded_exchange.domain.access_tokens import AccessToken
from guarded_exchange.domain.account import AccountBalances, Order, Page, Position, SymbolPosition
from guarded_exchange.domain.audit import AuditEvent, AuditRecord, JsonValue
from guarded_exchange.domain.credentials import (
    ApiKeyPair,
    Credential,
    CredentialBinding,
    KeyPermissions,
    SealedSecret,
)
from guarded_exchange.domain.history import HistoryPage, HistoryPeriod, Trade
from guarded_exchange.domain.idempotency import IdempotencyRecord, RecordStatus
from guarded_exchange.domain.instruments import Category, InstrumentRules
from guarded_exchange.domain.market import CandleInterval, Candles, Orderbook, Ticker
from guarded_exchange.domain.order_changes import OrderAmendment
from guarded_exchange.domain.orders import OrderFill, OrderRequest, PlacedOrder, PriceBand
from guarded_exchange.domain.policy import PolicyRecord
from guarded_exchange.domain.users import Environment, User


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

    async def get_instrument_rules(
        self, symbol: str, category: Category, *, any_status: bool = False
    ) -> InstrumentRules:
        """Return the trading rules of one instrument, whatever its quote currency.

        By default only instruments open for trading (or about to open) are found; ``any_status``
        also finds one being delisted or closed, so a risk-reducing order on it gets its real
        status instead of ``NotFound`` (ticket 011).

        Raises the same exceptions as ``get_ticker``; an instrument the adapter cannot
        represent in the domain (e.g. a dated future in ``linear``) raises ``InvalidParams``.
        """
        ...


class TradingExchange(Protocol):
    """Placing orders on one exchange account (FR-4, ADR-010, D-26). Signed calls take the key as an
    argument, like ``AccountReader``.

    The adapter owns the client order id format; the application only passes its ``action_id``.
    Placing is never retried automatically (ADR-002): a possibly-sent request is ``OutcomeUnknown``.
    """

    def client_order_id(self, action_id: str) -> str:
        """The exchange-side client id of the order for ``action_id`` (deterministic, D-19)."""
        ...

    async def get_price_band(self, symbol: str, category: Category) -> PriceBand:
        """Public: the price corridor the exchange accepts new orders in right now.

        Raises the same exceptions as ``ExchangeClient.get_ticker``.
        """
        ...

    async def get_symbol_position(self, keys: ApiKeyPair, symbol: str) -> SymbolPosition:
        """The USDT perpetual position on ``symbol`` (``position=None`` if flat) and its leverage.

        Raises what ``AccountReader`` methods raise, including ``UnsupportedPositionMode``.
        """
        ...

    async def place_order(self, keys: ApiKeyPair, order: OrderRequest) -> PlacedOrder:
        """Send one limit order (GTC, or IOC for a market order).

        Raises:
            OutcomeUnknown: the request may have reached the exchange (timeout after sending, broken
                connection, an exchange-side internal error, a duplicate client id); never retried.
            ExchangeRejected / InvalidParams / ExchangeCredentialsInvalid / RateLimited /
                ExchangeUnavailable: the exchange did not create the order.
        """
        ...

    async def get_order(
        self, keys: ApiKeyPair, category: Category, symbol: str, client_order_id: str
    ) -> OrderFill | None:
        """The order with this client id as the exchange shows it now; ``None`` if not visible yet.

        Raises what ``AccountReader`` methods raise.
        """
        ...

    async def find_order(
        self, keys: ApiKeyPair, category: Category, symbol: str, client_order_id: str
    ) -> OrderFill | None:
        """Reconnaissance after an unknown outcome (FR-27, ADR-014): the order with this client id among
        the open and recent orders, then in the order history. ``None``: not found anywhere, which is
        **not** proof that it was never created (the exchange's lists lag behind).

        Raises what ``AccountReader`` methods raise.
        """
        ...


class OrderChanges(Protocol):
    """Cancelling and amending existing orders of one account (FR-5, ticket 013). Signed calls take the
    key as an argument. Changes are never retried automatically (ADR-002); the exchange applies them
    asynchronously, so an accepted request is confirmed by reading the order back (D-21).
    """

    async def get_order_by_id(
        self, keys: ApiKeyPair, category: Category, symbol: str, order_id: str
    ) -> Order | None:
        """The order with this exchange id: among the open and recent orders, then in the order
        history; ``None`` if not listed anywhere (yet). ``origin`` is set by the adapter (D-26).

        Raises what ``AccountReader`` methods raise, including ``UnsupportedPositionMode``.
        """
        ...

    async def list_cancellable_orders(
        self, keys: ApiKeyPair, category: Category, symbol: str, limit: int
    ) -> Page[Order]:
        """Plain open orders on ``symbol`` (never conditional orders or a position's TP/SL), newest
        first, at most ``limit``. Raises what ``AccountReader.get_open_orders`` raises."""
        ...

    async def cancel_order(self, keys: ApiKeyPair, category: Category, symbol: str, order_id: str) -> None:
        """Ask the exchange to cancel one order; returns when the request was accepted.

        Raises:
            OutcomeUnknown: the request may have reached the exchange (timeout, broken connection, an
                exchange-side error or a "being processed" answer); never retried.
            NotFound: the exchange says the order is not open (filled, cancelled or unknown to it).
            ExchangeRejected / InvalidParams / ExchangeCredentialsInvalid / RateLimited /
                ExchangeUnavailable: the exchange did not act on the request.
        """
        ...

    async def amend_order(self, keys: ApiKeyPair, amendment: OrderAmendment) -> None:
        """Ask the exchange to change one order's quantity and/or price; raises like ``cancel_order``."""
        ...


class AccountReader(Protocol):
    """Signed reads of one exchange account, authenticated with the user's key (FR-2).

    Every method raises:
        ExchangeCredentialsInvalid: the exchange does not accept the key.
        RateLimited: the exchange's rate limit refused the request (``retry_after_s`` set).
        ExchangeUnavailable: network, timeout, 5xx or an unexpected response.
        ExchangeRejected / InvalidParams: any other refusal by the exchange.
    """

    async def get_balances(self, keys: ApiKeyPair) -> AccountBalances:
        """Balances of the trading account (unified account)."""
        ...

    async def get_positions(self, keys: ApiKeyPair, *, symbol: str | None, limit: int) -> Page[Position]:
        """Open USDT perpetual positions (size > 0), at most ``limit``; all USDT ones if no ``symbol``.

        Raises ``UnsupportedPositionMode`` if a returned position is in hedge mode (D-26).
        """
        ...

    async def get_open_orders(
        self, keys: ApiKeyPair, category: Category, *, symbol: str | None, limit: int
    ) -> Page[Order]:
        """Open orders (plain and conditional), newest first, at most ``limit``; for ``linear``
        without ``symbol`` all USDT perpetual ones. ``origin`` is set by the adapter, which owns the
        client order id format (D-26). Raises ``UnsupportedPositionMode`` for hedge-mode orders."""
        ...


class KeyPermissionsReader(Protocol):
    """Asks the exchange what an API key may do (FR-49). Configured for one environment."""

    async def get_key_permissions(self, keys: ApiKeyPair) -> KeyPermissions:
        """Return the key's permissions as reported by the exchange.

        Raises:
            ExchangeCredentialsInvalid: the exchange does not accept the key (invalid, wrong
                environment, bad signature, IP not whitelisted, expired).
            ExchangeUnavailable: the permissions cannot be checked right now (network, timeout,
                5xx, endpoint missing, unexpected response).
        """
        ...


class AccountHistoryReader(Protocol):
    """Signed reads of an account's past orders and fills (FR-2, FR-3), paged by the exchange.

    ``exchange_cursor`` is the exchange's opaque cursor of the page to read (``None``: the first page),
    as an earlier page returned it in ``next_exchange_cursor``; the period and filters must be the
    ones that page was read with (the caller keeps them together, ``HistoryQuery``). A cursor the
    adapter cannot send safely raises ``InvalidParams``. Raises what ``AccountReader`` methods
    raise. ``origin`` of orders and fills is set by the adapter (D-26).
    """

    async def get_order_history(
        self,
        keys: ApiKeyPair,
        category: Category,
        *,
        symbol: str | None,
        period: HistoryPeriod,
        exchange_cursor: str | None,
        limit: int,
    ) -> HistoryPage[Order]:
        """Closed orders updated in ``period``, newest first, at most ``limit``; for ``linear``
        without ``symbol`` all USDT perpetual ones."""
        ...

    async def get_trades(
        self,
        keys: ApiKeyPair,
        category: Category,
        *,
        symbol: str | None,
        period: HistoryPeriod,
        exchange_cursor: str | None,
        limit: int,
    ) -> HistoryPage[Trade]:
        """Executions (fills) in ``period``, newest first, at most ``limit``; for ``linear``
        without ``symbol`` all USDT perpetual ones."""
        ...


class CredentialCipher(Protocol):
    """Encrypts exchange keys bound to their owner (ADR-009: AES-256-GCM, AAD = binding)."""

    def seal(self, keys: ApiKeyPair, binding: CredentialBinding) -> SealedSecret: ...

    def open(self, sealed: SealedSecret, binding: CredentialBinding) -> ApiKeyPair:
        """Decrypt; a wrong binding, key or tampered data raises ``CredentialUnreadable``."""
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


class TokenGenerator(Protocol):
    def new_token(self) -> str:
        """A new bearer token secret: ``TOKEN_PREFIX`` + at least 256 random bits, URL-safe."""
        ...


class UserRepository(Protocol):
    async def add(self, user: User) -> None:
        """Insert a new user. A taken name raises ``UserNameTaken`` (a concurrent writer may have
        inserted it after the caller checked); a duplicate id is a programming error (raises)."""
        ...

    async def get(self, user_id: str) -> User | None: ...

    async def get_by_name(self, name: str) -> User | None: ...

    async def list(self) -> Sequence[User]:
        """All users ordered by name."""
        ...


class AccessTokenRepository(Protocol):
    """Bearer tokens by hash (PLAN ADR-004). The secret itself is never passed to storage."""

    async def add(self, token: AccessToken) -> None:
        """Insert a new token. A duplicate id or hash is a programming error (raises)."""
        ...

    async def get(self, token_id: str) -> AccessToken | None: ...

    async def get_by_hash(self, token_hash: str) -> AccessToken | None: ...

    async def revoke(self, token_id: str, at: datetime) -> bool:
        """Set ``revoked_at`` if the token is not revoked yet; ``True`` only if this call did it.

        Needs a write unit of work.
        """
        ...


class AuditLogRepository(Protocol):
    """Append-only journal (FR-37): there is deliberately no update or delete."""

    async def append(self, chain_id: str, event: AuditEvent, at: datetime) -> AuditRecord:
        """Append ``event`` to ``chain_id`` with the next ``seq`` (gapless, from 1)."""
        ...

    async def list(self, chain_id: str, *, after_seq: int = 0, limit: int = 100) -> Sequence[AuditRecord]:
        """Records of one chain with ``seq > after_seq`` in ``seq`` order, at most ``limit``."""
        ...


class PolicyRepository(Protocol):
    """One policy record per user (FR-12). No record = default ceiling, no overrides."""

    async def get(self, user_id: str) -> PolicyRecord | None: ...

    async def save(self, user_id: str, record: PolicyRecord) -> None:
        """Insert or replace the user's record. Needs a write unit of work."""
        ...


class CredentialRepository(Protocol):
    """Exchange keys (D-9, D-12). The secret is stored only as ``SealedSecret``."""

    async def add(self, credential: Credential, sealed: SealedSecret) -> None:
        """Insert a new credential. A second active one for the same user, exchange and
        environment violates a unique index (raises). Needs a write unit of work."""
        ...

    async def get(self, credential_id: str) -> Credential | None: ...

    async def get_active(
        self, user_id: str, exchange: str, environment: Environment
    ) -> Credential | None: ...

    async def list_for_user(self, user_id: str) -> Sequence[Credential]:
        """All credentials of the user, active and deleted, oldest first."""
        ...

    async def list_active(self, exchange: str, environment: Environment) -> Sequence[Credential]:
        """Active credentials of every user on one exchange environment."""
        ...

    async def get_sealed(self, credential_id: str) -> SealedSecret | None:
        """The secret of any credential, also a deleted one (D-9: outcome reconnaissance)."""
        ...

    async def get_sealed_active(self, credential_id: str) -> SealedSecret | None:
        """The secret only while the credential is active; the path for new actions."""
        ...

    async def update_permissions(
        self, credential_id: str, permissions: KeyPermissions, checked_at: datetime
    ) -> None:
        """Store the latest permissions reported by the exchange. Needs a write unit of work."""
        ...

    async def mark_deleted(self, credential_id: str, at: datetime) -> bool:
        """Soft-delete an active credential; ``True`` only if this call changed it (D-9)."""
        ...


class IdempotencyRepository(Protocol):
    """Claims of state-changing calls (FR-26, ADR-014, D-20), one per ``(user, kind, key)``.

    Every change is conditional on the record still being held by the caller's ``action_id``: a call
    whose record expired and was taken over can no longer change it. Needs a write unit of work.
    """

    async def claim(self, record: IdempotencyRecord, now: datetime) -> IdempotencyRecord:
        """Insert ``record`` atomically unless a live record holds its key. A record is taken over
        once expired at ``now``, or while still ``in_progress`` (nothing sent) after
        ``IN_FLIGHT_GUARD``. Returns the record that holds the key afterwards: ``record`` itself if
        the claim succeeded, otherwise the existing one (compare ``action_id``). Expired answered
        records of the same user are purged on the way; unanswered ones only by a takeover."""
        ...

    async def mark_sent(self, record: IdempotencyRecord, now: datetime) -> bool:
        """``in_progress`` -> ``sent`` if still held by ``record.action_id`` and not expired at ``now``;
        the expiry becomes at least ``now + IN_FLIGHT_GUARD``. ``False``: not held any more (fence)."""
        ...

    async def current(self, record: IdempotencyRecord) -> IdempotencyRecord | None:
        """Whatever record holds ``record``'s key now (``None``: none)."""
        ...

    async def finish(
        self,
        record: IdempotencyRecord,
        status: RecordStatus,
        result: Mapping[str, JsonValue],
        expires_at: datetime,
    ) -> bool:
        """Store the call's answer if the record is still held by ``record.action_id``."""
        ...

    async def release(self, record: IdempotencyRecord) -> bool:
        """Delete the record if still held by ``record.action_id`` (nothing to protect any more)."""
        ...


class MasterKeyStore(Protocol):
    """Where the local master key lives (PLAN ADR-009). Never exposes the key to use cases."""

    def exists(self) -> bool:
        """``True`` if a usable key is present. An unsafe or corrupt key raises instead."""
        ...

    def create(self) -> None:
        """Generate and persist a new key atomically. Never overwrites an existing one (raises)."""
        ...


class Authenticator(Protocol):
    """Turns a credential presented by a remote caller into a user id (PLAN ADR-004).

    The only source of the remote principal: never client info, headers or tool arguments.
    Today: opaque bearer tokens from the database; an OAuth 2.1 adapter (JWT from a real
    authorization server) would implement the same port without touching use cases.
    """

    async def authenticate(self, credential: str) -> str | None:
        """The id of the active user the credential belongs to, or ``None`` (refuse the request).

        Runs on every request, so revoking the token or the user applies to the next request.
        """
        ...


class UnitOfWork(Protocol):
    """Repositories bound to one database transaction."""

    @property
    def users(self) -> UserRepository: ...

    @property
    def audit(self) -> AuditLogRepository: ...

    @property
    def policies(self) -> PolicyRepository: ...

    @property
    def access_tokens(self) -> AccessTokenRepository: ...

    @property
    def credentials(self) -> CredentialRepository: ...

    @property
    def idempotency(self) -> IdempotencyRepository: ...

    async def lock_user(self, user_id: str) -> None:
        """Serialise read-modify-write of one user's state until the transaction ends (PLAN D-8).

        Only valid in a write unit of work (raises otherwise).
        """
        ...


class UnitOfWorkFactory(Protocol):
    """Opens a transaction: committed when the block exits normally, rolled back on exception.

    ``write=True`` takes the write lock at the start (SQLite ``BEGIN IMMEDIATE``), so a
    read-then-write transaction cannot fail half-way on lock upgrade; ``write=False`` is a
    read-only snapshot and any write through it raises.
    """

    def __call__(self, *, write: bool) -> AbstractAsyncContextManager[UnitOfWork]: ...
