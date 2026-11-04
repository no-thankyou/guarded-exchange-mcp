"""Output of ``get_confirmation_status`` / ``cancel_confirmation`` and of any trading call that was
parked for a human (FR-21, FR-47 (b); tickets 016, 017). Separate from ``schemas`` like the other slices."""

from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from guarded_exchange.domain.confirmation import ConfirmationStatus, PendingConfirmation

_NEXT: Final = {
    ConfirmationStatus.PENDING: "The action is NOT done: it waits for the user. Tell the user to approve or "
    "reject it outside the chat with `guarded-exchange confirm approve|reject <confirmation_id>`; you "
    "cannot approve it. Do not repeat the call. Read the state again with get_confirmation_status, or "
    "withdraw the request with cancel_confirmation.",
    ConfirmationStatus.APPROVED: "The user approved it; the server executes it itself. Read the state again "
    "with get_confirmation_status in a few seconds. Do not repeat the call.",
    ConfirmationStatus.EXECUTING: "It is being sent to the exchange. Read the state again with "
    "get_confirmation_status in a few seconds. Do not repeat the call.",
    ConfirmationStatus.EXECUTED: "Done. `result` has what the exchange reported.",
    ConfirmationStatus.FAILED: "NOT executed: `result.outcome.code` says why (it was re-checked at the time "
    "of execution). Tell the user; do not retry unchanged.",
    ConfirmationStatus.UNKNOWN: "It may or may not have reached the exchange. Do not repeat it: check "
    "get_open_orders and get_positions, then tell the user.",
    ConfirmationStatus.REJECTED: "The user rejected it. Do not ask again unless they say so.",
    ConfirmationStatus.EXPIRED: "Nobody confirmed it in time; nothing was done. Request it again only if the "
    "user still wants it.",
    ConfirmationStatus.EXPIRED_AFTER_APPROVAL: "It was approved but could not be started in time; nothing "
    "was done. Request it again only if the user still wants it.",
    ConfirmationStatus.CANCELLED: "It was withdrawn (by you, or because the key was replaced or deleted); "
    "nothing was done.",
}


class ConfirmationOut(BaseModel):
    """One confirmation: what was asked, what the user is shown, where it stands."""

    model_config = ConfigDict(frozen=True)

    confirmation_id: str = Field(description="Use it with get_confirmation_status / cancel_confirmation.")
    status: str = Field(
        description="pending: waits for the user. approved/executing: being executed. executed, failed "
        "(re-check refused it), unknown, rejected, expired, expired_after_approval, cancelled: final."
    )
    tool: str = Field(description="The action that was parked.")
    environment: str
    risk: str = Field(description="increases or reduces risk (FR-43).")
    expires_at: datetime = Field(description="The user must decide before this (UTC).")
    request: dict[str, Any] = Field(description="The arguments as asked.")
    shown_to_user: dict[str, Any] = Field(
        description="What the user sees and the executor holds the action to: sizes, the price bound of a "
        "market order, the orders a cancel touches (external ones marked)."
    )
    result: dict[str, Any] | None = Field(
        default=None, description="The outcome, once it is final (executed, failed, unknown)."
    )
    already_waiting: bool | None = Field(
        default=None,
        description="True: this call repeated one that was already parked; nothing new was created.",
    )
    next_step: str

    @classmethod
    def from_domain(
        cls, pending: PendingConfirmation, *, already_waiting: bool | None = None
    ) -> "ConfirmationOut":
        return cls(
            confirmation_id=pending.id,
            status=pending.status.value,
            tool=pending.tool.value,
            environment=pending.environment.value,
            risk=pending.risk.value,
            expires_at=pending.expires_at,
            request=dict(pending.action),
            shown_to_user=dict(pending.shown),
            result=None if pending.result is None else dict(pending.result),
            already_waiting=already_waiting,
            next_step=_NEXT[pending.status],
        )
