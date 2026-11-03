"""Instrument rules with a time-to-live cache (FR-1, FR-10).

Rules (tick size, quantity step, minimums, max leverage) change rarely, while order
validation (ticket 011) needs them on every order: one exchange round-trip per instrument per
TTL instead of one per call. The cache is per process and in memory (PLAN ADR-007: no Redis);
a stale entry is at most ``ttl`` old, and the exchange still rejects an order that breaks a
rule changed in between.
"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from guarded_exchange.application.ports import Clock, ExchangeClient
from guarded_exchange.domain.instruments import Category, InstrumentRules

DEFAULT_RULES_TTL: Final = timedelta(minutes=10)
DEFAULT_MAX_ENTRIES: Final = 4096
"""Bybit lists ~1000 spot and ~600 linear instruments; failures are never cached, so only real
instruments occupy entries. The bound is a guard, not a tuning knob."""


@dataclass(frozen=True, slots=True)
class _Entry:
    rules: InstrumentRules
    expires_at: datetime


class InstrumentRulesCache:
    """Reads rules through the ``ExchangeClient`` port and keeps successful answers for ``ttl``.

    Inputs must already be normalised (domain value objects). Errors are not cached: a
    transient failure or an unknown symbol is asked again on the next call. Two concurrent
    misses for one instrument may both reach the exchange; that is harmless for a read.
    """

    def __init__(
        self,
        exchange: ExchangeClient,
        clock: Clock,
        *,
        ttl: timedelta = DEFAULT_RULES_TTL,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if ttl <= timedelta(0) or max_entries < 1:
            raise ValueError("ttl must be positive and max_entries at least 1")
        self._exchange = exchange
        self._clock = clock
        self._ttl = ttl
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, Category], _Entry] = OrderedDict()

    async def get(self, symbol: str, category: Category, *, any_status: bool = False) -> InstrumentRules:
        """``any_status``: also find an instrument that is no longer open for trading (see the port).

        A cached entry answers both kinds of lookup: it carries its status, and callers decide by it.
        """
        key = (symbol, category)
        entry = self._entries.get(key)
        if entry is not None:
            if self._clock.now() < entry.expires_at:
                self._entries.move_to_end(key)  # LRU: a hit makes the entry the most recent
                return entry.rules
            del self._entries[key]  # expired: drop it now, even if the refresh below fails
        rules = await self._exchange.get_instrument_rules(symbol, category, any_status=any_status)
        self._entries[key] = _Entry(rules, self._clock.now() + self._ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)  # the least recently used goes first
        return rules

    def __len__(self) -> int:
        return len(self._entries)
