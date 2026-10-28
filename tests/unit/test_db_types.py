"""Decimal and time survive the trip to the database column representation and back (D-10)."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.dialects import sqlite

from guarded_exchange.infrastructure.clock import SystemClock, UuidGenerator
from guarded_exchange.infrastructure.db.dialect import DecimalText, EpochMillis

DIALECT = sqlite.dialect()


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "0.10",
        "0.00000001",
        "-42.5000",
        "1E+3",
        "123456789012345678901234567890.123456789012345678",  # > 17 significant digits
        "80571.10",
    ],
)
def test_decimal_round_trip_is_exact_including_trailing_zeros(value: str) -> None:
    original = Decimal(value)
    column = DecimalText()

    stored = column.process_bind_param(original, DIALECT)
    restored = column.process_result_value(stored, DIALECT)

    assert isinstance(stored, str)
    assert restored == original
    assert str(restored) == str(original)  # same exponent, not just numerically equal


@pytest.mark.parametrize("bad", [0.1, 1, "1.0"])
def test_decimal_column_rejects_non_decimals(bad: object) -> None:
    with pytest.raises(TypeError):
        DecimalText().process_bind_param(bad, DIALECT)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_decimal_column_rejects_non_finite(bad: str) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        DecimalText().process_bind_param(Decimal(bad), DIALECT)


def test_null_passes_through_both_types() -> None:
    assert DecimalText().process_bind_param(None, DIALECT) is None
    assert DecimalText().process_result_value(None, DIALECT) is None
    assert EpochMillis().process_bind_param(None, DIALECT) is None
    assert EpochMillis().process_result_value(None, DIALECT) is None


@pytest.mark.parametrize(
    "value",
    [
        datetime(1970, 1, 1, tzinfo=UTC),
        datetime(2026, 10, 27, 18, 53, 43, 449000, tzinfo=UTC),
        datetime(1969, 12, 31, 23, 59, 59, 999000, tzinfo=UTC),
        datetime(2262, 4, 11, 23, 47, 16, 854000, tzinfo=UTC),
    ],
)
def test_time_round_trip_is_exact(value: datetime) -> None:
    column = EpochMillis()

    stored = column.process_bind_param(value, DIALECT)
    restored = column.process_result_value(stored, DIALECT)

    assert isinstance(stored, int)
    assert restored == value
    assert restored is not None
    assert restored.tzinfo is UTC


def test_time_is_stored_as_epoch_milliseconds_and_read_back_as_utc() -> None:
    moscow = datetime(2026, 10, 27, 21, 0, 0, 5000, tzinfo=timezone(timedelta(hours=3)))

    stored = EpochMillis().process_bind_param(moscow, DIALECT)

    assert stored == 1_793_124_000_005
    assert EpochMillis().process_result_value(stored, DIALECT) == datetime(
        2026, 10, 27, 18, 0, 0, 5000, tzinfo=UTC
    )


def test_naive_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        EpochMillis().process_bind_param(datetime(2026, 10, 27), DIALECT)  # noqa: DTZ001


def test_sub_millisecond_time_is_rejected_instead_of_truncated() -> None:
    with pytest.raises(ValueError, match="sub-millisecond"):
        EpochMillis().process_bind_param(datetime(2026, 10, 27, 0, 0, 0, 1, tzinfo=UTC), DIALECT)


def test_time_column_rejects_non_datetimes() -> None:
    with pytest.raises(TypeError):
        EpochMillis().process_bind_param(1_793_127_600_005, DIALECT)  # type: ignore[arg-type]


def test_system_clock_returns_aware_utc_whole_milliseconds_that_survive_storage() -> None:
    now = SystemClock().now()

    assert now.tzinfo is UTC
    assert now.microsecond % 1000 == 0
    assert EpochMillis().process_result_value(EpochMillis().process_bind_param(now, DIALECT), DIALECT) == now


def test_uuid_ids_are_unique_hex() -> None:
    ids = {UuidGenerator().new_id() for _ in range(100)}

    assert len(ids) == 100
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in ids)
