"""Instrument identity: market category and symbol normalisation."""

import re
from enum import StrEnum

from guarded_exchange.domain.errors import InvalidParams

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}$")


class Category(StrEnum):
    """Market segment. v1 supports spot and USDT-margined linear perpetuals."""

    SPOT = "spot"
    LINEAR = "linear"


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
