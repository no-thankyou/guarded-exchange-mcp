"""Pending confirmations: an action parked until a human decides (SPEC FR-19 - FR-25, FR-47, FR-48;
PLAN ADR-005, ADR-006, D-3, D-4, D-8; tickets 016, 017).

A ``PendingConfirmation`` is the single source of truth of one confirmation: the action exactly as
asked (``action``, canonical JSON of its arguments), what the human is shown (``shown``: the plan
with sizes and the worst price of a market order, the orders a cancel touches, their origin), the
credential and environment it was created for, and where it stands. The record is written once, by
the server, from its own plan; no channel (and no agent) can change what is executed.

States (PLAN 5.1)::

    PENDING -> APPROVED -> EXECUTING -> EXECUTED | FAILED | UNKNOWN
    PENDING -> REJECTED | EXPIRED | CANCELLED
    APPROVED -> EXPIRED_AFTER_APPROVAL | CANCELLED | FAILED   (FAILED: refused by the re-check)
    UNKNOWN -> EXECUTED | FAILED                              (reconnaissance, later)

A transition is a conditional update in storage (``expected`` states): of several concurrent
deciders exactly one wins (CAS), and the winner alone may act on it.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.audit import JsonValue, canonical_json
from guarded_exchange.domain.errors import InvalidParams
from guarded_exchange.domain.risk import RiskDirection
from guarded_exchange.domain.users import Environment

APPROVAL_DEADLINE: Final = timedelta(seconds=30)
"""D-3: an approved action must start executing within this long after the approval, not after the
request's own expiry: approving one second before the TTL must not turn into a refusal (price risk is
covered by the fixed price bound, FR-48). Later it is ``EXPIRED_AFTER_APPROVAL``."""

RECONCILE_AFTER: Final = timedelta(seconds=30)
"""An ``EXECUTING`` confirmation is looked up on the exchange only this long after its start: a live
executor may still be sending (D-3)."""

KEY_MEMORY: Final = timedelta(hours=24)
"""How long an idempotency key of a confirmed action is remembered (as for other actions, D-20 (4))."""


class ConfirmationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXPIRED_AFTER_APPROVAL = "expired_after_approval"
    CANCELLED = "cancelled"


S = ConfirmationStatus
SLOT_STATUSES: Final = frozenset({S.PENDING, S.APPROVED, S.EXECUTING})
"""Confirmations that hold one of the user's slots (``max_pending``, FR-19)."""
DECIDABLE: Final = frozenset({S.PENDING})
INVALIDATABLE: Final = frozenset({S.PENDING, S.APPROVED})
"""FR-29 / D-9: what a replaced key or revoked access cancels. ``EXECUTING`` and ``UNKNOWN`` are
brought to an outcome instead."""

TRANSITIONS: Final[Mapping[ConfirmationStatus, frozenset[ConfirmationStatus]]] = {
    S.PENDING: frozenset({S.APPROVED, S.REJECTED, S.EXPIRED, S.CANCELLED}),
    S.APPROVED: frozenset({S.EXECUTING, S.EXPIRED_AFTER_APPROVAL, S.CANCELLED, S.FAILED}),
    S.EXECUTING: frozenset({S.EXECUTED, S.FAILED, S.UNKNOWN}),
    S.UNKNOWN: frozenset({S.EXECUTED, S.FAILED}),
    S.EXECUTED: frozenset(),
    S.FAILED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.EXPIRED_AFTER_APPROVAL: frozenset(),
    S.CANCELLED: frozenset(),
}


class DecisionChannel(StrEnum):
    """Where a decision came from; always journaled (FR-42)."""

    CLI = "cli"
    AGENT = "mcp:agent"
    """The agent withdrew its own request (``cancel_confirmation``): never an approval."""
    SYSTEM = "system"
    """The server: a timeout, a revoked key, a failed re-check."""


def require_transition(old: ConfirmationStatus, new: ConfirmationStatus) -> None:
    if new not in TRANSITIONS[old]:
        raise ValueError(f"a confirmation cannot go from {old.value} to {new.value}")


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """One confirmation. ``id`` is also the ``action_id`` of the action when it is executed, so the
    client order id the exchange sees is derived from it and is the same on every attempt (D-19)."""

    id: str
    user_id: str
    credential_id: str
    environment: Environment
    tool: ActionKind
    action: Mapping[str, JsonValue]
    """The arguments as asked (``_payload`` of the action path), canonical JSON."""
    action_digest: str
    """D-4: the call (tool, arguments, environment, credential); equal for a repeat of the call."""
    terms_digest: str
    """FR-22: the call together with what was shown; recomputed before executing."""
    risk: RiskDirection
    shown: Mapping[str, JsonValue]
    """What the human sees and what the executor holds the action to (sizes, price bound, orders)."""
    status: ConfirmationStatus
    created_at: datetime
    expires_at: datetime
    idempotency_key: str | None = None
    approved_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    channel: DecisionChannel | None = None
    exec_started_at: datetime | None = None
    result: Mapping[str, JsonValue] | None = None
    """The outcome once there is one (``executed``/``failed``/``unknown``): the journal's outcome and
    the answer of the action."""

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def status_at(self, now: datetime) -> ConfirmationStatus:
        """What a reader should see: a ``pending`` past its expiry is ``expired`` even before the
        executor's sweep writes it."""
        if self.status is S.PENDING and self.expired(now):
            return S.EXPIRED
        return self.status

    @property
    def approval_deadline(self) -> datetime | None:
        return None if self.approved_at is None else self.approved_at + APPROVAL_DEADLINE


def action_digest(
    tool: ActionKind,
    args: Mapping[str, JsonValue],
    environment: Environment,
    credential_id: str,
) -> str:
    """D-4: SHA-256 of the canonical call. Decimals are normalised (``"0.10"`` = ``"0.1"``), floats
    refused, every argument present."""
    canonical = canonical_json(
        {"tool": tool.value, "environment": environment.value, "credential_id": credential_id, "args": args}
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def terms_digest(digest: str, risk: RiskDirection, shown: Mapping[str, JsonValue]) -> str:
    """FR-22: binds the call to what was shown (for cancel-all: the exact list of order ids)."""
    canonical = canonical_json({"action_digest": digest, "risk": risk.value, "shown": shown})
    return hashlib.sha256(canonical.encode()).hexdigest()


def new_pending(
    *,
    pending_id: str,
    user_id: str,
    credential_id: str,
    environment: Environment,
    tool: ActionKind,
    action: Mapping[str, JsonValue],
    risk: RiskDirection,
    shown: Mapping[str, JsonValue],
    now: datetime,
    ttl_s: int,
    idempotency_key: str | None,
) -> PendingConfirmation:
    if ttl_s <= 0:
        raise InvalidParams("the confirmation lifetime must be positive")
    digest = action_digest(tool, action, environment, credential_id)
    return PendingConfirmation(
        id=pending_id,
        user_id=user_id,
        credential_id=credential_id,
        environment=environment,
        tool=tool,
        action=action,
        action_digest=digest,
        terms_digest=terms_digest(digest, risk, shown),
        risk=risk,
        shown=shown,
        status=S.PENDING,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_s),
        idempotency_key=idempotency_key,
    )


def intact(pending: PendingConfirmation) -> bool:
    """FR-22: the stored action and terms still hash to what was recorded at creation."""
    digest = action_digest(pending.tool, pending.action, pending.environment, pending.credential_id)
    return digest == pending.action_digest and (
        terms_digest(digest, pending.risk, pending.shown) == pending.terms_digest
    )
