"""Read-only account use cases: balances, positions, open orders (SPEC FR-2, FR-3, FR-41).

Every read is scoped to the calling user: ``ReadCredential`` picks that user's active key and
nothing else, so the account read is always the caller's own (FR-33). Raw input is validated with
domain value objects, so every entrypoint gets the same ``InvalidParams``; the key is resolved in
one short read transaction and the exchange is called after it (D-10).
"""

from dataclasses import replace

from guarded_exchange.application.credentials import ReadCredential
from guarded_exchange.application.ports import AccountReader
from guarded_exchange.domain.account import AccountBalances, Order, Page, Position, account_items_limit
from guarded_exchange.domain.instruments import normalize_symbol, parse_category


def _symbol_or_none(raw: str | None) -> str | None:
    return None if raw is None else normalize_symbol(raw)


class GetBalances:
    """The caller's trading account balances."""

    def __init__(self, keys: ReadCredential, account: AccountReader) -> None:
        self._keys = keys
        self._account = account

    async def __call__(self, user_id: str | None) -> AccountBalances:
        return await self._account.get_balances(await self._keys(user_id))


class GetPositions:
    """The caller's open perpetual positions, at most ``limit`` (FR-3)."""

    def __init__(self, keys: ReadCredential, account: AccountReader) -> None:
        self._keys = keys
        self._account = account

    async def __call__(self, user_id: str | None, symbol: str | None, limit: int) -> Page[Position]:
        sym, count = _symbol_or_none(symbol), account_items_limit(limit)
        page = await self._account.get_positions(await self._keys(user_id), symbol=sym, limit=count)
        return _cut(page, count)


class GetOpenOrders:
    """The caller's open orders in one category, newest first, at most ``limit`` (FR-3); each one
    marked as created through this system or external (FR-2)."""

    def __init__(self, keys: ReadCredential, account: AccountReader) -> None:
        self._keys = keys
        self._account = account

    async def __call__(
        self, user_id: str | None, category: str, symbol: str | None, limit: int
    ) -> Page[Order]:
        cat, sym, count = parse_category(category), _symbol_or_none(symbol), account_items_limit(limit)
        page = await self._account.get_open_orders(await self._keys(user_id), cat, symbol=sym, limit=count)
        return _cut(page, count)


def _cut[T](page: Page[T], limit: int) -> Page[T]:
    """Never more than ``limit`` items, whatever the adapter returned."""
    if len(page.items) <= limit:
        return page
    return replace(page, items=page.items[:limit], has_more=True)
