"""Decimal arithmetic for prices and quantities: parsing and steps (SPEC FR-10, GP-1).

Never through ``float``. A quantity that is not a multiple of the instrument's step is rounded
**down** (the order never grows); a price that is not a multiple of the tick is an error, but the
two nearest valid prices are offered (``neighbours``). Rounding a computed price (the worst price
of a market order, ADR-010) goes to the safe side: ``round_down`` for a buy, ``round_up`` for a sell.
Step arithmetic runs in its own decimal context, independent of the caller's precision.
"""

import re
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from typing import Final

from guarded_exchange.domain.errors import InvalidParams

AMOUNT_PATTERN: Final = r"^[0-9]{1,20}(\.[0-9]{1,18})?$"
"""A positive amount as text: ASCII digits with an optional fraction, no sign or exponent.
``[0-9]``, not ``\\d``: in Python and JSON Schema engines ``\\d`` may match other scripts' digits."""

MAX_SIGNIFICANT_DIGITS: Final = 28
"""Decimal's default precision: an amount never needs more."""

_PRECISION: Final = 80
"""Working precision of step arithmetic: quotient and product of two amounts of at most 38 digits
each (the pattern's bound) stay exact."""

_AMOUNT_RE: Final = re.compile(AMOUNT_PATTERN)


def parse_amount(field: str, raw: str) -> Decimal:
    """A positive decimal from text (``"0.010"`` -> ``Decimal("0.010")``) or ``InvalidParams``.

    Only strings: a JSON number would have gone through a binary float on the client side. At most
    ``MAX_SIGNIFICANT_DIGITS`` significant digits. The message never quotes the value.
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
    if len(value.as_tuple().digits) > MAX_SIGNIFICANT_DIGITS:
        raise InvalidParams(
            f"{field} has more than {MAX_SIGNIFICANT_DIGITS} significant digits",
            details={"field": field, "reason": "too_many_digits"},
        )
    return value


def _check_step(step: Decimal) -> None:
    if not step.is_finite() or step <= 0:
        raise ValueError("step must be a positive finite decimal")


def round_down(value: Decimal, step: Decimal) -> Decimal:
    """The largest multiple of ``step`` that is ``<= value``, with the step's precision."""
    _check_step(step)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        result = (value / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if result > value:  # an order quantity must never grow, whatever happened above
            raise ArithmeticError("round_down increased the value")
    return result


def round_up(value: Decimal, step: Decimal) -> Decimal:
    """The smallest multiple of ``step`` that is ``>= value``, with the step's precision."""
    _check_step(step)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        result = (value / step).to_integral_value(rounding=ROUND_CEILING) * step
        if result < value:
            raise ArithmeticError("round_up decreased the value")
    return result


def is_multiple(value: Decimal, step: Decimal) -> bool:
    _check_step(step)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return value % step == 0


def neighbours(value: Decimal, step: Decimal) -> tuple[Decimal, Decimal]:
    """The nearest multiples of ``step`` below and above ``value`` (equal if ``value`` is one)."""
    return round_down(value, step), round_up(value, step)
