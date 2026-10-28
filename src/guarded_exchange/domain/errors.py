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
