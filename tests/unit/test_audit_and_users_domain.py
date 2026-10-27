from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from guarded_exchange.domain.audit import AuditRecord, canonical_decimal, canonical_json
from guarded_exchange.domain.errors import InvalidParams
from guarded_exchange.domain.users import Environment, UserStatus, new_user, validate_user_name

pytestmark = pytest.mark.req("FR-36")


def test_canonical_json_is_sorted_compact_and_utf8() -> None:
    text = canonical_json({"b": 1, "a": {"y": [True, None], "x": "привет"}})

    assert text == '{"a":{"x":"привет","y":[true,null]},"b":1}'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0.10"), "0.1"),
        (Decimal("100"), "100"),
        (Decimal("1E+3"), "1000"),
        (Decimal("-0.000"), "0"),
        (Decimal("0.00000001"), "0.00000001"),
        (Decimal("123456789012345678901234567890.123456789"), "123456789012345678901234567890.123456789"),
    ],
)
def test_decimal_is_written_without_exponent_or_trailing_zeros(value: Decimal, expected: str) -> None:
    assert canonical_decimal(value) == expected
    assert canonical_json({"v": value}) == f'{{"v":"{expected}"}}'


def test_datetime_becomes_utc_iso_with_milliseconds() -> None:
    moscow = timezone(timedelta(hours=3))

    text = canonical_json({"at": datetime(2026, 10, 27, 15, 0, 0, 5000, tzinfo=moscow)})

    assert text == '{"at":"2026-10-27T12:00:00.005Z"}'


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"v": 0.1}, TypeError),
        ({"v": Decimal("NaN")}, ValueError),
        ({"v": Decimal("Infinity")}, ValueError),
        ({"v": datetime(2026, 10, 27)}, ValueError),  # noqa: DTZ001 - naive on purpose
        ({"v": b"raw"}, TypeError),
        ({"v": {1: "x"}}, TypeError),
        ({"v": object()}, TypeError),
    ],
)
def test_values_without_a_single_canonical_text_are_rejected(payload: dict[str, object], error: type) -> None:
    with pytest.raises(error):
        canonical_json(payload)  # type: ignore[arg-type]


def test_audit_record_payload_is_read_only() -> None:
    record = AuditRecord("system", 1, datetime(2026, 1, 1, tzinfo=UTC), "x", "cli", {"a": 1})

    with pytest.raises(TypeError):
        record.payload["a"] = 2  # type: ignore[index]


@pytest.mark.req("FR-45")
def test_new_user_is_active_on_testnet() -> None:
    created = datetime(2026, 10, 27, tzinfo=UTC)

    user = new_user("u-1", " Local ", created)

    assert user.name == "local"
    assert user.status is UserStatus.ACTIVE
    assert user.environment is Environment.TESTNET
    assert user.created_at == created
    assert user.is_active


@pytest.mark.parametrize("bad", ["", " ", "-x", "a b", "x" * 65, "имя"])
def test_invalid_user_names_are_rejected(bad: str) -> None:
    with pytest.raises(InvalidParams) as err:
        validate_user_name(bad)
    assert err.value.details == {"field": "name"}
