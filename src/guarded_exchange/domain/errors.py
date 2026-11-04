"""Domain exceptions and the stable error codes the agent sees (SPEC FR-28, PLAN ADR-013).

Every exception carries a machine-readable ``code`` that the interface layer passes to the
agent unchanged, a human-readable ``message`` written by us (never raw exchange text), and an
optional ``next_step`` hint. Free text from external systems goes to ``untrusted_text`` so the
interface can label it as data, not instructions (FR-41).
"""

from typing import ClassVar


class DomainError(Exception):
    """Base class for every anticipated failure the agent should be able to act on."""

    code: ClassVar[str] = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        next_step: str | None = None,
        retry_after_s: float | None = None,
        details: dict[str, str] | None = None,
        untrusted_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.next_step = next_step
        self.retry_after_s = retry_after_s
        self.details = dict(details or {})
        self.untrusted_text = untrusted_text


class InvalidParams(DomainError):
    """The request is malformed or names something the exchange does not accept."""

    code: ClassVar[str] = "invalid_params"


class NotFound(DomainError):
    """The requested object does not exist (or is not visible to the caller)."""

    code: ClassVar[str] = "not_found"


class PermissionDenied(DomainError):
    """The caller's profile or policy does not allow this (FR-11, FR-12, FR-44)."""

    code: ClassVar[str] = "permission_denied"


class ExchangeUnavailable(DomainError):
    """The exchange could not be reached or answered with a transient failure."""

    code: ClassVar[str] = "exchange_unavailable"


class ExchangeRejected(DomainError):
    """The exchange understood the request and refused it."""

    code: ClassVar[str] = "exchange_rejected"


class ExchangeCredentialsInvalid(DomainError):
    """The exchange key is missing, refused by the exchange, expired or not acceptable (FR-28, FR-49)."""

    code: ClassVar[str] = "exchange_credentials_invalid"


class CredentialsPermissionsChanged(DomainError):
    """A re-check found the key's permissions outside the allowlist: trading is blocked (FR-49, D-18)."""

    code: ClassVar[str] = "credentials_permissions_changed"


class RateLimited(DomainError):
    """A rate limit refused the request (the exchange's now; this server's own in ticket 022).

    ``retry_after_s`` says when trying again makes sense.
    """

    code: ClassVar[str] = "rate_limited"


class UnsupportedPositionMode(DomainError):
    """The account (or a symbol) is in hedge mode; v1 supports one-way mode only (PLAN D-26)."""

    code: ClassVar[str] = "unsupported_position_mode"

    def __init__(self) -> None:
        super().__init__(
            "the account uses hedge mode (separate long and short positions); only one-way mode is supported",
            next_step="Do not retry. Tell the user to switch USDT perpetuals to one-way mode on the "
            "exchange; until then perpetual positions and orders cannot be read or managed here.",
            details={"reason": "hedge_mode"},
        )


class InstrumentRuleViolation(DomainError):
    """The order breaks a rule of the instrument (tick size, minimum quantity or value, trading
    status, the exchange's price band); nothing was sent (FR-10)."""

    code: ClassVar[str] = "instrument_rule_violation"


class LimitExceeded(DomainError):
    """A risk limit of the effective policy refuses the action; nothing was sent (FR-13 - FR-17).

    ``details`` name the limit (``limit``) and the allowed value (``allowed``).
    """

    code: ClassVar[str] = "limit_exceeded"


class KillSwitchActive(DomainError):
    """The user's kill switch is on and the action increases risk; nothing was sent (FR-16)."""

    code: ClassVar[str] = "kill_switch_active"


class ConfirmationChannelUnavailable(DomainError):
    """The policy requires the user's confirmation, but no trusted channel can ask for it (FR-25)."""

    code: ClassVar[str] = "confirmation_channel_unavailable"


class PendingLimitReached(DomainError):
    """Too many actions of this user wait for confirmation at the same time (FR-19, E-6); nothing was
    asked of the human. Transient: a slot frees up when one is decided, cancelled or expires."""

    code: ClassVar[str] = "pending_limit_reached"


class ConfirmationExpired(DomainError):
    """The confirmation request ran out of time before the human decided (FR-23)."""

    code: ClassVar[str] = "confirmation_expired"


class ConfirmationDeclined(DomainError):
    """The human did not confirm in the client's UI (declined, cancelled the form, or submitted it without
    ticking the box); nothing was executed (FR-20)."""

    code: ClassVar[str] = "confirmation_declined"


class ConfirmationNotExecuted(DomainError):
    """The action was approved but the re-check at execution time refused it, so nothing was sent (FR-48)."""

    code: ClassVar[str] = "confirmation_not_executed"


class ConfirmationNotPending(DomainError):
    """The confirmation is not waiting for a decision any more (decided, cancelled, executing, done)."""

    code: ClassVar[str] = "confirmation_not_pending"


class RiskDirectionChanged(DomainError):
    """The human approved a risk-reducing action that would now increase risk (FR-48, D-8)."""

    code: ClassVar[str] = "risk_direction_changed"


class PositionNotFound(DomainError):
    """A risk-reducing action needs an open position it can reduce, and there is none (FR-43, D-18)."""

    code: ClassVar[str] = "position_not_found"


class OutcomeUnknown(DomainError):
    """The request may or may not have reached the exchange (timeout, broken connection, an
    exchange-side internal error): never retried blindly (FR-27)."""

    code: ClassVar[str] = "outcome_unknown"


class IdempotencyConflict(DomainError):
    """The idempotency key was already used for a call with other arguments (FR-26); nothing was sent."""

    code: ClassVar[str] = "idempotency_conflict"


_RESTORABLE: dict[str, type[DomainError]] = {
    cls.code: cls
    for cls in (
        InvalidParams,
        NotFound,
        PermissionDenied,
        ExchangeUnavailable,
        ExchangeRejected,
        ExchangeCredentialsInvalid,
        CredentialsPermissionsChanged,
        RateLimited,
        InstrumentRuleViolation,
        LimitExceeded,
        KillSwitchActive,
        ConfirmationChannelUnavailable,
        PositionNotFound,
        OutcomeUnknown,
        IdempotencyConflict,
    )
}


def restore_error(
    code: str,
    message: str,
    *,
    next_step: str | None,
    details: dict[str, str],
    untrusted_text: str | None,
) -> DomainError:
    """The stored answer of an earlier call, as the exception it was (FR-26: same key, same answer).

    ``retry_after_s`` is not restored: an answer that asked to wait is never stored. A code this
    version does not know is a programming error (raises ``ValueError``), never a guess.
    """
    if code == UnsupportedPositionMode.code:
        return UnsupportedPositionMode()
    cls = _RESTORABLE.get(code)
    if cls is None:
        raise ValueError(f"cannot restore an error with code {code!r}")
    return cls(message, next_step=next_step, details=details, untrusted_text=untrusted_text)
