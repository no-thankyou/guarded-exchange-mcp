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
    NotFound,
    OutcomeUnknown,
    RateLimited,
)

RET_OK: Final = 0
# Transient server-side failures: safe to retry for GET requests.
RETRYABLE_RET_CODES: Final = frozenset({10000, 10016})
_PARAM_ERROR: Final = 10001
RATE_LIMITED_RET_CODES: Final = frozenset({10006, 10429})
"""Too many visits (per UID) and system-level frequency protection: nothing was done, wait."""
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
    if ret_code in RATE_LIMITED_RET_CODES:
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


UNKNOWN_OUTCOME_RET_CODES: Final = frozenset(
    {
        10000,  # Server Timeout
        10016,  # Server error
        3400214,  # Server error, please try again later (UTA)
        170001,  # Internal error (spot)
        170007,  # Timeout waiting for response from backend server (spot)
        170032,  # Network error. Please try again later (spot)
        170146,  # Order creation timeout (spot)
        170234,  # System Error (spot)
        170035,  # Submitted to the system for processing! (spot)
        170149,  # Create order failed (spot): no reason given
        10014,  # Invalid duplicate request
    }
)
"""Server-side errors, timeouts and "being processed" answers after which an order may still exist
(ADR-014): never "failed". Re-scanned against https://bybit-exchange.github.io/docs/v5/error on
2026-11-03 (ticket 012 review 1); the amend/cancel ones are ``ORDER_CHANGE_UNKNOWN_RET_CODES``."""
DUPLICATE_CLIENT_ID_RET_CODES: Final = frozenset({110072, 170141, 110030})
"""``orderLinkId`` already used (spike 004: also after cancel or fill), or "Duplicate orderId": the
order may be ours."""
PRICE_BAND_RET_CODES: Final = frozenset({110121, 170193})
"""Price outside the exchange's corridor (linear in reject mode / spot, spike 004): no order."""
DEFINITE_CREATE_FAMILIES: Final = (
    range(10001, 10030),  # request, signature, key, rate and access errors
    range(110001, 111000),  # derivatives order validation (UTA)
    range(170001, 171000),  # spot order validation
    range(3400001, 3401011),  # UTA account
)
"""Families of documented refusals of ``order/create`` (minus the codes above): no order was created.
Any other non-zero ``retCode`` is treated as an unknown outcome (ticket 012): reconnaissance by
client id then finds out, instead of a code nobody listed being read as "failed"."""


def _definite_refusal(ret_code: int) -> bool:
    return any(ret_code in family for family in DEFINITE_CREATE_FAMILIES)


def _unknown_create_reason(ret_code: int) -> tuple[str, str] | None:
    """Why an ``order/create`` answer leaves the order possibly created, or ``None`` (a refusal)."""
    if ret_code in UNKNOWN_OUTCOME_RET_CODES:
        return "the exchange reported an internal error while placing the order", "exchange_internal_error"
    if ret_code in DUPLICATE_CLIENT_ID_RET_CODES:
        return "the exchange already has an order with this client id", "duplicate_client_order_id"
    if not _definite_refusal(ret_code):
        return (
            "the exchange answered with a code this server does not recognise as a refusal",
            "unrecognised_exchange_code",
        )
    return None


def error_for_create(ret_code: int, ret_msg: str, *, retry_after_s: float | None = None) -> DomainError:
    """``retCode`` of ``POST /v5/order/create`` -> domain exception (FR-27, FR-28, ADR-014).

    Unknown-outcome and duplicate-id codes, and every code outside the documented refusal families,
    become ``OutcomeUnknown`` (the use case looks the order up by its client id); the rest is a
    definite refusal: no order was created.
    """
    details = {"exchange_code": str(ret_code)}
    # Explicit definite answers first: they are no order whatever family they are in (33004 is not in
    # any, 10429 neither).
    if ret_code in CREDENTIAL_RET_CODES or ret_code in RATE_LIMITED_RET_CODES:
        return error_for_ret_code(ret_code, ret_msg, retry_after_s=retry_after_s)
    unknown = _unknown_create_reason(ret_code)
    if unknown is not None:
        message, reason = unknown
        return OutcomeUnknown(message, details={**details, "reason": reason}, untrusted_text=ret_msg)
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


# --- cancel and amend (ticket 013) -----------------------------------------------------------------

ORDER_CHANGE_UNKNOWN_RET_CODES: Final = frozenset(
    {
        170147,  # Order cancellation timeout (spot)
        170150,  # Cancel order failed (spot): no reason given
        170191,  # Can not cancel order, please try again later (spot)
        170212,  # Cancel order request processing (spot)
        170190,  # Cancel order has been finished (spot): whose cancel, it does not say
        170310,  # Order modification timeout (spot)
        170311,  # Order modification failed (spot): no reason given
        110079,  # The order is processing and can not be operated, please try again later
        40004,  # the order is modified during the process of replacing, please check the order status
    }
)
"""Answers to ``order/cancel`` and ``order/amend`` after which the change may have been (or may still
be) applied: timeouts, "being processed", failures without a reason. Checked against
https://bybit-exchange.github.io/docs/v5/error on 2026-11-03 (ticket 013): none of them says that the
order was left as it was, so each is "unknown" and the order's status is read (D-21)."""
ORDER_NOT_OPEN_RET_CODES: Final = frozenset(
    {
        110001,  # Order does not exist
        110008,  # The order has been completed or cancelled
        110010,  # The order has been cancelled
        170142,  # Order has been cancelled (spot)
        170213,  # Order does not exist. (spot)
    }
)
"""The order is not open any more (or never was): this request changed nothing."""


def error_for_order_change(  # noqa: PLR0911 - one return per class of answer
    ret_code: int, ret_msg: str, *, retry_after_s: float | None = None
) -> DomainError:
    """``retCode`` of ``POST /v5/order/cancel`` or ``/v5/order/amend`` -> domain exception (FR-27, FR-28).

    The rule of ``order/create`` (ticket 012): a definite refusal only within the documented refusal
    families, minus the codes after which the change may have happened; anything else is
    ``OutcomeUnknown``, and the use case reads the order to find out.
    """
    details = {"exchange_code": str(ret_code)}
    if ret_code in CREDENTIAL_RET_CODES or ret_code in RATE_LIMITED_RET_CODES:
        return error_for_ret_code(ret_code, ret_msg, retry_after_s=retry_after_s)
    if ret_code in UNKNOWN_OUTCOME_RET_CODES or ret_code in ORDER_CHANGE_UNKNOWN_RET_CODES:
        return OutcomeUnknown(
            "the exchange did not confirm the change (it timed out, failed without a reason, or is still "
            "processing the order)",
            details={**details, "reason": "exchange_internal_error"},
            untrusted_text=ret_msg,
        )
    if ret_code in ORDER_NOT_OPEN_RET_CODES:
        return NotFound(
            "the exchange says the order is not open (filled, cancelled or unknown to it); nothing was "
            "changed",
            next_step="Check get_order_history for what happened to the order; do not retry.",
            details={**details, "reason": "order_not_open"},
            untrusted_text=ret_msg,
        )
    if not _definite_refusal(ret_code):
        return OutcomeUnknown(
            "the exchange answered with a code this server does not recognise as a refusal",
            details={**details, "reason": "unrecognised_exchange_code"},
            untrusted_text=ret_msg,
        )
    if ret_code in PRICE_BAND_RET_CODES:
        return ExchangeRejected(
            "the price is outside the exchange's current price band",
            next_step="Use a price closer to the market; the band moves with the market.",
            details={**details, "reason": "price_outside_band"},
            untrusted_text=ret_msg,
        )
    if ret_code == _PARAM_ERROR:
        return InvalidParams(
            "the exchange rejected the change",
            next_step="Check qty and price against get_instrument_rules and the order in get_open_orders; "
            "report the exchange reason to the user.",
            details=details,
            untrusted_text=ret_msg,
        )
    return error_for_ret_code(ret_code, ret_msg, retry_after_s=retry_after_s)
