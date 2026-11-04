"""What the trading path needs to park an action for a human, and what every reader of a pending
confirmation shares (SPEC FR-19 - FR-25, FR-29, FR-42, FR-47; PLAN ADR-005, D-9, D-17, D-20).

``open_pending`` runs inside the decision's write transaction (under ``lock_user``), so the limit on
simultaneous confirmations (count + insert, D-17), the repeat check (D-20 (2)) and the journal record
are one atomic step. The confirmation is created from the **server's own plan**; the agent's call only
chose the action. Nothing here lets a caller (the agent or a channel) change what will be executed.

``ConfirmationPending`` is how the path tells its caller "parked, not done": it is not an error, but
it is raised so that no action type needs a second return type. The MCP layer turns it into the
``pending_confirmation`` answer (FR-47 (b)).
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog

from guarded_exchange.application.ports import UnitOfWork
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.audit import AuditEvent, AuditEventType, ClientDescription, JsonValue
from guarded_exchange.domain.confirmation import (
    INVALIDATABLE,
    KEY_MEMORY,
    ConfirmationStatus,
    DecisionChannel,
    PendingConfirmation,
    new_pending,
)
from guarded_exchange.domain.credentials import ApiKeyPair
from guarded_exchange.domain.errors import IdempotencyConflict, PendingLimitReached
from guarded_exchange.domain.policy import EffectivePolicy
from guarded_exchange.domain.risk import RiskDirection
from guarded_exchange.domain.users import Environment

log = structlog.get_logger(__name__)

CLI_ACTOR: Final = "cli:user"
SYSTEM_ACTOR: Final = "system:confirmations"
AGENT_ACTOR: Final = "mcp:agent"

OpenKeys = Callable[[str], Awaitable[ApiKeyPair]]
"""The keys of a credential by id, deleted ones too (D-9: reconnaissance of an in-flight action)."""


@dataclass(frozen=True, slots=True)
class ConfirmationSupport:
    """How an action path asks for confirmation. ``cli_enabled`` is the admin's explicit switch for the
    CLI as a trusted channel (FR-42, E-5); without it nothing is parked and an action that needs
    confirming is refused (FR-25). ``open_keys`` serves the reconnaissance of an interrupted execution."""

    cli_enabled: bool
    open_keys: OpenKeys

    @property
    def channel_available(self) -> bool:
        return self.cli_enabled


class ConfirmationPending(Exception):
    """The action was parked and waits for the human (or already did: a repeat of the same call)."""

    def __init__(self, pending: PendingConfirmation, *, created: bool) -> None:
        super().__init__(f"confirmation {pending.id} is {pending.status.value}")
        self.pending = pending
        self.created = created


@dataclass(frozen=True, slots=True)
class PendingRequest:
    """What a decision hands over to be parked (all from the server's own plan)."""

    pending_id: str
    user_id: str
    credential_id: str
    environment: Environment
    tool: ActionKind
    action: Mapping[str, JsonValue]
    risk: RiskDirection
    shown: Mapping[str, JsonValue]
    idempotency_key: str | None
    client: ClientDescription | None


async def open_pending(
    uow: UnitOfWork, request: PendingRequest, policy: EffectivePolicy, now: datetime, *, actor: str
) -> ConfirmationPending:
    """Park the action: the answer to hand to the agent. Caller holds ``lock_user``.

    A repeat of a call that is still waiting (same arguments, credential and environment; or the same
    idempotency key) gets that confirmation back and creates nothing (D-20 (2)); the same key with other
    arguments is ``idempotency_conflict``. Past ``max_pending`` it raises ``PendingLimitReached``.
    """
    pending = new_pending(
        pending_id=request.pending_id,
        user_id=request.user_id,
        credential_id=request.credential_id,
        environment=request.environment,
        tool=request.tool,
        action=request.action,
        risk=request.risk,
        shown=request.shown,
        now=now,
        ttl_s=policy.confirmation_ttl_s,
        idempotency_key=request.idempotency_key,
    )
    repeat = await _earlier(uow, pending, now)
    if repeat is not None:
        return ConfirmationPending(repeat, created=False)
    if await uow.confirmations.count_slots(pending.user_id, now) >= policy.max_pending:
        raise PendingLimitReached(
            f"{policy.max_pending} actions already wait for your confirmation; nothing was asked of you "
            "for this one",
            next_step="Do not retry in a loop. Tell the user to approve or reject the waiting ones "
            "(`guarded-exchange confirm list`), or cancel your own with cancel_confirmation, then "
            "call again.",
            details={"limit": "max_pending", "allowed": str(policy.max_pending)},
        )
    await uow.confirmations.add(pending)
    await uow.audit.append(
        pending.user_id,
        AuditEvent(
            AuditEventType.CONFIRMATION_REQUESTED,
            actor,
            {
                "confirmation_id": pending.id,
                "tool": pending.tool.value,
                "credential_id": pending.credential_id,
                "environment": pending.environment.value,
                "risk": pending.risk.value,
                "request": pending.action,
                "shown": pending.shown,
                "action_digest": pending.action_digest,
                "expires_at": pending.expires_at,
                "client": None if request.client is None else request.client.to_payload(),
            },
        ),
        now,
    )
    return ConfirmationPending(pending, created=True)


async def _earlier(
    uow: UnitOfWork, pending: PendingConfirmation, now: datetime
) -> PendingConfirmation | None:
    if pending.idempotency_key is not None:
        keyed = await uow.confirmations.find_by_key(
            pending.user_id, pending.idempotency_key, now - KEY_MEMORY
        )
        if keyed is not None:
            if keyed.action_digest != pending.action_digest:
                raise IdempotencyConflict(
                    "this idempotency_key was already used for a call with other arguments; nothing was sent",
                    next_step="Use a new idempotency_key for a new action; reuse a key only to repeat "
                    "exactly the same call.",
                    details={"reason": "different_arguments"},
                )
            return keyed
    return await uow.confirmations.find_open_by_digest(pending.user_id, pending.action_digest, now)


def decided_event(
    pending: PendingConfirmation,
    status: ConfirmationStatus,
    channel: DecisionChannel,
    actor: str,
    **extra: JsonValue,
) -> AuditEvent:
    """FR-24, FR-42: every decision with its channel, refusals and timeouts too."""
    payload: dict[str, JsonValue] = {
        "confirmation_id": pending.id,
        "tool": pending.tool.value,
        "status": status.value,
        "channel": channel.value,
        "credential_id": pending.credential_id,
        "action_digest": pending.action_digest,
        **extra,
    }
    return AuditEvent(AuditEventType.CONFIRMATION_DECIDED, actor, payload)


async def invalidate_pending(
    uow: UnitOfWork, user_id: str, credential_id: str, now: datetime, *, reason: str
) -> int:
    """FR-29, D-9: a replaced or deleted key (or revoked access) cancels what waits for a human and what
    is approved but not started. Caller holds ``lock_user``. ``executing`` and ``unknown`` are left:
    they are brought to an outcome."""
    count = 0
    for pending in await uow.confirmations.list_for_credential(user_id, credential_id, INVALIDATABLE):
        won = await uow.confirmations.transition(
            pending.id,
            expected=INVALIDATABLE,
            to=ConfirmationStatus.CANCELLED,
            at=now,
            channel=DecisionChannel.SYSTEM,
            decided_by=SYSTEM_ACTOR,
        )
        if won:
            count += 1
            await uow.audit.append(
                user_id,
                decided_event(
                    pending, ConfirmationStatus.CANCELLED, DecisionChannel.SYSTEM, SYSTEM_ACTOR, reason=reason
                ),
                now,
            )
    return count
