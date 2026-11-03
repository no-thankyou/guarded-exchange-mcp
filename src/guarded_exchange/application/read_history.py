"""Read-only history use cases: closed orders and trades over a period, paged (SPEC FR-2, FR-3, FR-41).

Like the account reads, every read uses the caller's own key (``ReadCredential``, FR-33). All
input is checked before the key is read or the exchange is called: the period against the
exchange's limits (at most 7 days per read, at most 2 years back) and the cursor, which must be
one this tool returned. The cursor carries the whole query, so a next page is read with exactly
the parameters of the first one.
"""

from dataclasses import dataclass, replace
from datetime import datetime

from guarded_exchange.application.credentials import ReadCredential
from guarded_exchange.application.ports import AccountHistoryReader, Clock
from guarded_exchange.domain.account import Order, account_items_limit
from guarded_exchange.domain.errors import InvalidParams
from guarded_exchange.domain.history import (
    HistoryKind,
    HistoryPage,
    HistoryPeriod,
    HistoryQuery,
    Trade,
    decode_cursor,
    encode_cursor,
    ensure_within_depth,
    history_period,
)
from guarded_exchange.domain.instruments import normalize_symbol, parse_category


@dataclass(frozen=True, slots=True)
class HistoryResult[T]:
    """One page for the agent: the items, the period actually read, the cursor of the next page
    (``None``: there is none) and how many rows of the page were left out (``HistoryPage``)."""

    items: tuple[T, ...]
    period: HistoryPeriod
    next_cursor: str | None
    omitted: int


class _HistoryRead:
    _kind: HistoryKind

    def __init__(self, keys: ReadCredential, history: AccountHistoryReader, clock: Clock) -> None:
        self._keys = keys
        self._history = history
        self._clock = clock

    def _query(
        self,
        category: str,
        symbol: str | None,
        start: datetime | None,
        end: datetime | None,
        cursor: str | None,
    ) -> HistoryQuery:
        cat, now = parse_category(category), self._clock.now()
        if cursor is None:
            sym = None if symbol is None else normalize_symbol(symbol)
            return HistoryQuery(self._kind, cat, sym, history_period(start, end, now))
        if symbol is not None or start is not None or end is not None:
            raise InvalidParams(
                "a cursor already holds the symbol and period of the first page",
                next_step="Pass only category, cursor and limit to read the next page; to change the "
                "symbol or period, call again without cursor.",
                details={"field": "cursor", "reason": "cursor_with_filters"},
            )
        query = decode_cursor(cursor, self._kind)
        if query.category is not cat:
            raise InvalidParams(
                "the cursor belongs to another category",
                next_step=f"Pass category={query.category.value} with this cursor.",
                details={"field": "category", "reason": "cursor_category_mismatch"},
            )
        if query.symbol is not None:
            normalize_symbol(query.symbol)
        ensure_within_depth(query.period, now)
        return query

    @staticmethod
    def _result[T](query: HistoryQuery, page: HistoryPage[T]) -> HistoryResult[T]:
        """Not cut to ``limit`` like the account reads: the next page starts after the last row the
        exchange returned, so dropping rows here would lose them. The port returns at most ``limit``."""
        token = page.next_exchange_cursor
        next_cursor = None if token is None else encode_cursor(replace(query, exchange_cursor=token))
        return HistoryResult(page.items, query.period, next_cursor, page.omitted)


class GetOrderHistory(_HistoryRead):
    """The caller's closed orders in one category over a period, newest first, at most ``limit``
    per page; each one marked as created through this system or external (FR-2)."""

    _kind = HistoryKind.ORDERS

    async def __call__(
        self,
        user_id: str | None,
        category: str,
        *,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        cursor: str | None = None,
        limit: int,
    ) -> HistoryResult[Order]:
        query, count = self._query(category, symbol, start, end, cursor), account_items_limit(limit)
        page = await self._history.get_order_history(
            await self._keys(user_id),
            query.category,
            symbol=query.symbol,
            period=query.period,
            exchange_cursor=query.exchange_cursor,
            limit=count,
        )
        return self._result(query, page)


class GetTrades(_HistoryRead):
    """The caller's executions (fills) in one category over a period, newest first, at most
    ``limit`` per page; each one marked by the origin of its order (FR-2)."""

    _kind = HistoryKind.TRADES

    async def __call__(
        self,
        user_id: str | None,
        category: str,
        *,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        cursor: str | None = None,
        limit: int,
    ) -> HistoryResult[Trade]:
        query, count = self._query(category, symbol, start, end, cursor), account_items_limit(limit)
        page = await self._history.get_trades(
            await self._keys(user_id),
            query.category,
            symbol=query.symbol,
            period=query.period,
            exchange_cursor=query.exchange_cursor,
            limit=count,
        )
        return self._result(query, page)
