"""Idempotency rules of the domain (ticket 012: FR-26, ADR-014, D-4, D-20)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from guarded_exchange.domain.audit import JsonValue
from guarded_exchange.domain.errors import (
    ConfirmationChannelUnavailable,
    DomainError,
    IdempotencyConflict,
    InvalidParams,
    LimitExceeded,
    OutcomeUnknown,
    UnsupportedPositionMode,
    restore_error,
)
from guarded_exchange.domain.idempotency import (
    EXPLICIT_KEY_TTL,
    FINGERPRINT_WINDOW,
    IN_FLIGHT_GUARD,
    KeyKind,
    Outcome,
    RecordStatus,
    expiry_when_done,
    fingerprint_applies,
    keeps_record,
    new_claim,
    parse_idempotency_key,
    request_hash,
)
from guarded_exchange.domain.risk import RiskDirection
from guarded_exchange.domain.users import Environment

pytestmark = pytest.mark.req("FR-26")

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
TESTNET = Environment.TESTNET


def call(**changes: JsonValue) -> dict[str, JsonValue]:
    args: dict[str, JsonValue] = {
        "symbol": "BTCUSDT",
        "category": "linear",
        "side": "buy",
        "order_type": "market",
        "qty": Decimal("0.1"),
        "price": None,
        "reduce_only": False,
    }
    args.update(changes)
    return args


def test_edge_decimal_spellings_give_one_fingerprint() -> None:
    """``"0.10"`` and ``"0.1"`` are one call (D-4: decimals normalised)."""
    assert request_hash("place_order", call(qty=Decimal("0.10")), TESTNET) == request_hash(
        "place_order", call(qty=Decimal("0.1")), TESTNET
    )
    assert request_hash("place_order", call(qty=Decimal("1E-1")), TESTNET) == request_hash(
        "place_order", call(qty=Decimal("0.100")), TESTNET
    )


def test_any_other_argument_tool_or_environment_is_another_call() -> None:
    base = request_hash("place_order", call(), TESTNET)
    others = {
        request_hash("place_order", call(qty=Decimal("0.2")), TESTNET),
        request_hash("place_order", call(side="sell"), TESTNET),
        request_hash("place_order", call(reduce_only=True), TESTNET),
        request_hash("place_order", call(price=Decimal(80000)), TESTNET),
        request_hash("amend_order", call(), TESTNET),
        request_hash("place_order", call(), Environment.MAINNET),
    }
    assert base not in others
    assert len(others) == 6
    assert len(base) == 64


def test_a_float_has_no_canonical_form_and_is_refused() -> None:
    with pytest.raises(TypeError, match="float"):
        request_hash("place_order", call(qty=0.1), TESTNET)  # type: ignore[arg-type]


def test_only_risk_increasing_actions_get_a_fingerprint() -> None:
    """D-20 (3): cancel, cancel-all, reduce-only, SL/TP, leverage down are never deduplicated."""
    assert fingerprint_applies(RiskDirection.INCREASES) is True
    assert fingerprint_applies(RiskDirection.REDUCES) is False


@pytest.mark.parametrize(
    ("kind", "outcome", "kept"),
    [
        (KeyKind.EXPLICIT, Outcome.ACCEPTED, True),
        (KeyKind.EXPLICIT, Outcome.UNKNOWN, True),
        (KeyKind.EXPLICIT, Outcome.REFUSED, True),
        (KeyKind.EXPLICIT, Outcome.TRANSIENT, False),
        (KeyKind.FINGERPRINT, Outcome.ACCEPTED, True),
        (KeyKind.FINGERPRINT, Outcome.UNKNOWN, True),
        (KeyKind.FINGERPRINT, Outcome.REFUSED, False),
        (KeyKind.FINGERPRINT, Outcome.TRANSIENT, False),
    ],
)
def test_an_order_that_may_exist_always_keeps_its_record(kind: KeyKind, outcome: Outcome, kept: bool) -> None:
    assert keeps_record(kind, outcome) is kept


def test_expiry_of_an_explicit_key_and_of_a_fingerprint() -> None:
    explicit = new_claim(
        user_id="u", kind=KeyKind.EXPLICIT, key="k", request_hash="h" * 64, action_id="a", now=NOW
    )
    fingerprint = new_claim(
        user_id="u", kind=KeyKind.FINGERPRINT, key="h" * 64, request_hash="h" * 64, action_id="b", now=NOW
    )
    assert (explicit.status, explicit.expires_at) == (RecordStatus.IN_PROGRESS, NOW + EXPLICIT_KEY_TTL)
    assert fingerprint.expires_at == NOW + IN_FLIGHT_GUARD  # never expires while the call is in flight
    later = NOW + timedelta(seconds=5)
    assert expiry_when_done(explicit, later, FINGERPRINT_WINDOW) == NOW + EXPLICIT_KEY_TTL
    assert expiry_when_done(fingerprint, later, FINGERPRINT_WINDOW) == later + FINGERPRINT_WINDOW
    assert explicit.held_by("a")
    assert not explicit.held_by("b")


@pytest.mark.parametrize(
    "key", ["a", "0b6f4d7e-1c2a-4f7b-9a1e-5d3c2b1a0f9e", "order:2026-11-03_1.a", "Z" * 64]
)
def test_an_idempotency_key_is_taken_as_given(key: str) -> None:
    assert parse_idempotency_key(key) == key


@pytest.mark.parametrize("key", ["", " a", "a b", "-a", "Z" * 65, "ключ", "a\n", "sk_live/secret"])
def test_a_malformed_key_is_invalid_params_without_echo(key: str) -> None:
    with pytest.raises(InvalidParams) as error:
        parse_idempotency_key(key)
    assert error.value.details == {"field": "idempotency_key"}
    if len(key.strip()) > 3:  # never echoed (a pasted secret must not travel back to the model)
        assert key.strip() not in error.value.message


@pytest.mark.parametrize(
    "error",
    [
        LimitExceeded("too big", next_step="smaller", details={"limit": "max_order_notional_usdt"}),
        ConfirmationChannelUnavailable("confirm", next_step="ask"),
        OutcomeUnknown("maybe", details={"client_order_id": "gx-1"}, untrusted_text="Server Timeout"),
        IdempotencyConflict("other args"),
    ],
)
def test_a_stored_error_is_restored_as_the_same_exception(error: DomainError) -> None:
    restored = restore_error(
        error.code,
        error.message,
        next_step=error.next_step,
        details=error.details,
        untrusted_text=error.untrusted_text,
    )
    assert type(restored) is type(error)
    assert (restored.message, restored.next_step, restored.details, restored.untrusted_text) == (
        error.message,
        error.next_step,
        error.details,
        error.untrusted_text,
    )


def test_restoring_an_unknown_code_is_an_error_not_a_guess() -> None:
    assert isinstance(
        restore_error("unsupported_position_mode", "", next_step=None, details={}, untrusted_text=None),
        UnsupportedPositionMode,
    )
    with pytest.raises(ValueError, match="cannot restore"):
        restore_error("internal_error", "boom", next_step=None, details={}, untrusted_text=None)
