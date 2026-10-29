"""Bybit V5 ``retCode`` and HTTP failures -> domain exceptions.

Only codes relevant to the endpoints implemented so far are mapped explicitly; everything
else becomes ``ExchangeRejected`` with the exchange text attached as untrusted data (FR-41).
Source: https://bybit-exchange.github.io/docs/v5/error (checked 2026-10-27).
"""

from typing import Final

from guarded_exchange.domain.errors import (
    DomainError,
    ExchangeCredentialsInvalid,
    ExchangeRejected,
    ExchangeUnavailable,
    InvalidParams,
    OutcomeUnknown,
    RateLimited,
)

RET_OK: Final = 0
# Transient server-side failures: safe to retry for GET requests.
RETRYABLE_RET_CODES: Final = frozenset({10000, 10016})
_PARAM_ERROR: Final = 10001
_RATE_LIMITED: Final = 10006
# The key itself is not accepted: invalid / other environment, bad signature, no permission for the
# endpoint, authentication failed, IP not whitelisted, expired.
CREDENTIAL_RET_CODES: Final = frozenset({10003, 10004, 10005, 10007, 10010, 33004})

_RETRY_LATER = "Retry in a few seconds; if it keeps failing, tell the user the exchange is unavailable."


DEFAULT_RETRY_AFTER_S: Final = 1.0
"""When a rate-limited answer carries no usable ``X-Bapi-Limit-Reset-Timestamp``."""


def error_for_ret_code(ret_code: int, ret_msg: str, *, retry_after_s: float | None = None) -> DomainError:
    """Translate a non-zero ``retCode`` into the domain exception the agent will see.

    ``retry_after_s`` (from the rate-limit reset header) is used for ``10006`` only.
    """
    details = {"exchange_code": str(ret_code)}
    if ret_code == _PARAM_ERROR:
        return InvalidParams(
            "the exchange rejected the request parameters",
            next_step="Check the symbol and category (spot or linear), e.g. BTCUSDT on linear.",
            details=details,
            untrusted_text=ret_msg,
        )
    if ret_code in CREDENTIAL_RET_CODES:
        return ExchangeCredentialsInvalid(
            "the exchange did not accept the API key",
            next_step="Check that the key belongs to this environment (a Demo Trading key works only on "
            "api-demo.bybit.com), is not expired and allows this server's IP address.",
            # No exchange text: authentication errors may echo the signed origin string, which
            # contains the API key (e.g. "error sign! origin_string[...]").
            details=details,
        )
    if ret_code == _RATE_LIMITED:
        # Our own buckets and the circuit breaker in front of the exchange are ticket 022.
        return RateLimited(
            "the exchange rate limit was hit",
            next_step="Wait retry_after_s seconds before calling again; do not retry in a tight loop.",
            retry_after_s=DEFAULT_RETRY_AFTER_S if retry_after_s is None else retry_after_s,
            details=details,
            untrusted_text=ret_msg,
        )
    if ret_code in RETRYABLE_RET_CODES:
        return ExchangeUnavailable(
            "the exchange reported an internal error",
            next_step=_RETRY_LATER,
            details=details,
            untrusted_text=ret_msg,
        )
    return ExchangeRejected(
        "the exchange rejected the request",
        next_step="Do not retry unchanged; report the exchange reason to the user.",
        details=details,
        untrusted_text=ret_msg,
    )


UNKNOWN_OUTCOME_RET_CODES: Final = frozenset({10000, 10016, 170146})
"""Server-side errors after which an order may still exist (ADR-014): never "failed"."""
DUPLICATE_CLIENT_ID_RET_CODES: Final = frozenset({110072, 170141})
"""``orderLinkId`` already used (spike 004: also after cancel or fill): the order may be ours."""
PRICE_BAND_RET_CODES: Final = frozenset({110121, 170193})
"""Price outside the exchange's corridor (linear in reject mode / spot, spike 004): no order."""


def error_for_create(ret_code: int, ret_msg: str, *, retry_after_s: float | None = None) -> DomainError:
    """``retCode`` of ``POST /v5/order/create`` -> domain exception (FR-27, FR-28, ADR-014).

    Unknown-outcome and duplicate-id codes become ``OutcomeUnknown`` (ticket 012 finds out what
    happened); everything else is a definite refusal: no order was created.
    """
    details = {"exchange_code": str(ret_code)}
    if ret_code in UNKNOWN_OUTCOME_RET_CODES:
        return OutcomeUnknown(
            "the exchange reported an internal error while placing the order",
            details={**details, "reason": "exchange_internal_error"},
            untrusted_text=ret_msg,
        )
    if ret_code in DUPLICATE_CLIENT_ID_RET_CODES:
        return OutcomeUnknown(
            "the exchange already has an order with this client id",
            details={**details, "reason": "duplicate_client_order_id"},
            untrusted_text=ret_msg,
        )
    if ret_code in PRICE_BAND_RET_CODES:
        return ExchangeRejected(
            "the price is outside the exchange's current price band",
            next_step="Use a price closer to the market (or a market order); the band moves with the market.",
            details={**details, "reason": "price_outside_band"},
            untrusted_text=ret_msg,
        )
    if ret_code == _PARAM_ERROR:
        return InvalidParams(
            "the exchange rejected the order parameters",
            next_step="Check qty and price against get_instrument_rules; report the exchange reason to "
            "the user.",
            details=details,
            untrusted_text=ret_msg,
        )
    return error_for_ret_code(ret_code, ret_msg, retry_after_s=retry_after_s)


def outcome_unknown(reason: str) -> OutcomeUnknown:
    """The order request may have reached the exchange (read timeout, broken connection, 5xx)."""
    return OutcomeUnknown(f"no reliable answer from the exchange: {reason}", details={"reason": "no_answer"})


def unavailable(reason: str) -> ExchangeUnavailable:
    """Network failure, timeout, HTTP 5xx/403 or an unparsable body."""
    return ExchangeUnavailable(f"the exchange is unavailable: {reason}", next_step=_RETRY_LATER)
