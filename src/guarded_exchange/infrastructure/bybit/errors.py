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


def error_for_ret_code(ret_code: int, ret_msg: str) -> DomainError:
    """Translate a non-zero ``retCode`` into the domain exception the agent will see."""
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
        # Full rate-limit handling (retry_after from X-Bapi-Limit-Reset-Timestamp) is ticket 022.
        return ExchangeUnavailable(
            "the exchange rate limit was hit",
            next_step=_RETRY_LATER,
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


def unavailable(reason: str) -> ExchangeUnavailable:
    """Network failure, timeout, HTTP 5xx/403 or an unparsable body."""
    return ExchangeUnavailable(f"the exchange is unavailable: {reason}", next_step=_RETRY_LATER)
