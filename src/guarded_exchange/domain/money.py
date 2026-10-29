"""Decimal arithmetic for prices and quantities: parsing and steps (SPEC FR-10, GP-1).

Never through ``float``. A quantity that is not a multiple of the instrument's step is rounded
**down** (the order never grows); a price that is not a multiple of the tick is an error, but the
two nearest valid prices are offered (``neighbours``). Rounding a computed price (the worst price
of a market order, ADR-010) goes to the safe side: ``round_down`` for a buy, ``round_up`` for a sell.
"""

import re
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Final

from guarded_exchange.domain.errors import InvalidParams

AMOUNT_PATTERN: Final = r"^\d{1,20}(\.\d{1,18})?$"
"""A positive amount as text: plain digits with an optional fraction, no sign or exponent."""

_AMOUNT_RE: Final = re.compile(AMOUNT_PATTERN)


def parse_amount(field: str, raw: str) -> Decimal:
    """A positive decimal from text (``"0.010"`` -> ``Decimal("0.010")``) or ``InvalidParams``.

    Only strings: a JSON number would have gone through a binary float on the client side. The
    message never quotes the value.
    """
    text = raw.strip()
    if not _AMOUNT_RE.fullmatch(text):
        raise InvalidParams(
            f'{field} must be a positive decimal number written as a string, e.g. "0.01"',
            next_step=f'Pass {field} as a string of digits with an optional fraction, e.g. "0.01".',
            details={"field": field},
        )
    try:
        value = Decimal(text)
    except InvalidOperation:  # pragma: no cover - the pattern admits only valid decimals
        raise InvalidParams(f"{field} is not a number", details={"field": field}) from None
    if value <= 0:
        raise InvalidParams(f"{field} must be greater than zero", details={"field": field})
    return value


def _check_step(step: Decimal) -> None:
    if not step.is_finite() or step <= 0:
        raise ValueError("step must be a positive finite decimal")


def round_down(value: Decimal, step: Decimal) -> Decimal:
    """The largest multiple of ``step`` that is ``<= value``, with the step's precision."""
    _check_step(step)
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_up(value: Decimal, step: Decimal) -> Decimal:
    """The smallest multiple of ``step`` that is ``>= value``, with the step's precision."""
    _check_step(step)
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def is_multiple(value: Decimal, step: Decimal) -> bool:
    _check_step(step)
    return value % step == 0


def neighbours(value: Decimal, step: Decimal) -> tuple[Decimal, Decimal]:
    """The nearest multiples of ``step`` below and above ``value`` (equal if ``value`` is one)."""
    return round_down(value, step), round_up(value, step)
