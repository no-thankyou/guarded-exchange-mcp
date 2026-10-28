"""Instruments: identity (category, symbol), trading rules and the v1 USDT-only restriction."""

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from guarded_exchange.domain.errors import InvalidParams

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}$")

SUPPORTED_QUOTE: Final = "USDT"
"""v1 trades only USDT-quoted instruments (SPEC FR-4): limits are expressed in USDT (FR-13)."""


class Category(StrEnum):
    """Market segment. v1 supports spot and USDT-margined linear perpetuals."""

    SPOT = "spot"
    LINEAR = "linear"


class InstrumentStatus(StrEnum):
    """Lifecycle of an instrument on the exchange. Only ``TRADING`` accepts orders."""

    PRE_LAUNCH = "pre_launch"
    PENDING_OPEN = "pending_open"
    TRADING = "trading"
    SETTLING = "settling"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    """A status this version does not know: treated as not trading (fail closed, NFR-1)."""


def normalize_symbol(raw: str) -> str:
    """Return the canonical symbol (``" btcusdt "`` -> ``"BTCUSDT"``) or raise ``InvalidParams``."""
    symbol = raw.strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        raise InvalidParams(
            "symbol must be 2-30 letters or digits, e.g. BTCUSDT",
            next_step="Pass the exchange symbol without separators, e.g. BTCUSDT or ETHUSDT.",
            details={"field": "symbol"},
        )
    return symbol


def parse_category(raw: str) -> Category:
    """Return the ``Category`` for ``"spot"``/``"linear"`` (case-insensitive) or raise ``InvalidParams``."""
    try:
        return Category(raw.strip().lower())
    except ValueError:
        allowed = ", ".join(c.value for c in Category)
        raise InvalidParams(
            f"category must be one of: {allowed}",
            next_step="Use 'spot' for spot pairs or 'linear' for USDT perpetual futures.",
            details={"field": "category"},
        ) from None


@dataclass(frozen=True, slots=True)
class Instrument:
    """What is traded: ``BTCUSDT`` on ``linear`` is BTC priced (and margined) in USDT."""

    symbol: str
    category: Category
    base: str
    quote: str


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    """Exchange constraints an order must satisfy before it is sent (SPEC FR-1, FR-10).

    Prices are in the quote currency, quantities in the base asset, ``min_notional`` is
    ``qty * price`` in the quote currency. ``max_leverage`` is ``None`` where leverage does not
    apply (spot in v1).
    """

    instrument: Instrument
    status: InstrumentStatus
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal | None
    max_leverage: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("tick_size", "qty_step"):
            value: Decimal = getattr(self, name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite decimal")
        if not self.min_qty.is_finite() or self.min_qty < 0:
            raise ValueError("min_qty must be a non-negative finite decimal")
        if self.min_notional is not None and (not self.min_notional.is_finite() or self.min_notional < 0):
            raise ValueError("min_notional must be a non-negative finite decimal")
        if self.max_leverage is not None and (not self.max_leverage.is_finite() or self.max_leverage < 1):
            raise ValueError("max_leverage must be a finite decimal >= 1")

    @property
    def is_trading(self) -> bool:
        return self.status is InstrumentStatus.TRADING


def require_supported_quote(instrument: Instrument) -> None:
    """Raise ``InvalidParams`` unless ``instrument`` is quoted in USDT (FR-4).

    The message names only values that passed our own validation (normalised symbol, the
    exchange's coin code), never raw exchange text.
    """
    if instrument.quote != SUPPORTED_QUOTE:
        raise InvalidParams(
            f"{instrument.symbol} is quoted in {instrument.quote}; only {SUPPORTED_QUOTE}-quoted "
            "instruments are supported",
            next_step=f"Use the {SUPPORTED_QUOTE} pair instead, e.g. {instrument.base}{SUPPORTED_QUOTE}.",
            details={"field": "symbol", "reason": "quote_not_supported", "quote": instrument.quote},
        )
