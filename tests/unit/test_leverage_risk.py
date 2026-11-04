"""FR-43 for set_leverage (ticket 014): higher leverage, or one whose starting point is unknown,
increases risk; the same or lower leverage reduces it."""

from decimal import Decimal

import pytest

from guarded_exchange.domain.risk import RiskDirection, classify_leverage

pytestmark = pytest.mark.req("FR-43")


@pytest.mark.parametrize(
    ("current", "new", "expected"),
    [
        pytest.param(None, Decimal(1), RiskDirection.INCREASES, id="unknown-current-is-strict"),
        pytest.param(Decimal(5), Decimal(10), RiskDirection.INCREASES, id="higher"),
        pytest.param(Decimal(5), Decimal(5), RiskDirection.REDUCES, id="same"),
        pytest.param(Decimal(10), Decimal(5), RiskDirection.REDUCES, id="lower"),
    ],
)
def test_leverage_risk_direction(current: Decimal | None, new: Decimal, expected: RiskDirection) -> None:
    assert classify_leverage(current, new) is expected
