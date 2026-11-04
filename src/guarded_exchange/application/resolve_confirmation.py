"""Deciding and reading pending confirmations (SPEC FR-20, FR-22 - FR-24, FR-42, FR-47; PLAN ADR-005, D-6).

Two kinds of callers, kept apart on purpose:

- ``ResolveConfirmation`` approves or rejects. Only a **human channel** calls it (today the CLI, and only
  when the admin enabled it, FR-42); **no MCP tool is wired to it** - the model cannot confirm anything
  (FR-20, AC-4). The test on the tool registry guards that.
- ``CancelConfirmation`` and ``GetConfirmationStatus`` are what the agent has: withdraw its own waiting
  request (never an approval, it only reduces risk, D-6) and read its state.

Every decision is a conditional update (CAS) under ``lock_user``: of two concurrent approvals exactly one
wins and the other is told the state it found. The journal records every decision with its channel.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from guarded_exchange.application.confirmation import AGENT_ACTOR, CLI_ACTOR, SYSTEM_ACTOR, decided_event
from guarded_exchange.application.policy import active_principal, no_access
from guarded_exchange.application.ports import Clock, UnitOfWork, UnitOfWorkFactory
from guarded_exchange.domain.confirmation import (
    DECIDABLE,
    INVALIDATABLE,
    SLOT_STATUSES,
    ConfirmationStatus,
    DecisionChannel,
    PendingConfirmation,
)
from guarded_exchange.domain.errors import (
    ConfirmationChannelUnavailable,
    ConfirmationExpired,
    ConfirmationNotPending,
    NotFound,
)

_NO_SUCH = "there is no such confirmation"


def not_found() -> NotFound:
    return NotFound(
        _NO_SUCH,
        next_step="Check the confirmation_id as the call that parked the action returned it.",
        details={"reason": "confirmation_not_found"},
    )


def _not_pending(pending: PendingConfirmation, now_status: ConfirmationStatus) -> ConfirmationNotPending:
    return ConfirmationNotPending(
        f"confirmation {pending.id} is {now_status.value}, not waiting for a decision",
        next_step="Nothing to do; read its state with get_confirmation_status.",
        details={"status": now_status.value},
    )


async def expire_waiting(uow: UnitOfWork, pending: PendingConfirmation, now: datetime) -> None:
    """A waiting confirmation past its expiry becomes ``expired`` (journaled); caller holds ``lock_user``."""
    if await uow.confirmations.transition(
        pending.id,
        expected=DECIDABLE,
        to=ConfirmationStatus.EXPIRED,
        at=now,
        channel=DecisionChannel.SYSTEM,
        decided_by=SYSTEM_ACTOR,
    ):
        await uow.audit.append(
            pending.user_id,
            decided_event(pending, ConfirmationStatus.EXPIRED, DecisionChannel.SYSTEM, SYSTEM_ACTOR),
            now,
        )


class ResolveConfirmation:
    """Approve or reject a waiting confirmation through the CLI (the only human channel in the MVP)."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, *, cli_enabled: bool) -> None:
        self._uow = uow
        self._clock = clock
        self._cli_enabled = cli_enabled

    async def approve(self, pending_id: str) -> PendingConfirmation:
        return await self._resolve(pending_id, ConfirmationStatus.APPROVED)

    async def reject(self, pending_id: str) -> PendingConfirmation:
        return await self._resolve(pending_id, ConfirmationStatus.REJECTED)

    async def _resolve(self, pending_id: str, to: ConfirmationStatus) -> PendingConfirmation:
        if not self._cli_enabled:
            raise ConfirmationChannelUnavailable(
                "confirming through the CLI is not enabled on this installation (FR-42)",
                next_step="The admin enables it with GX_CONFIRM_CLI_ENABLED=true for the server and the CLI "
                "(it is off by default: an agent with shell access could use it).",
                details={"reason": "cli_confirmation_disabled"},
            )
        failure: Exception | None = None
        async with self._uow(write=True) as uow:
            first = await uow.confirmations.get(pending_id)
            if first is None:
                raise not_found()
            await uow.lock_user(first.user_id)
            pending = await uow.confirmations.get(pending_id)
            if pending is None:
                raise not_found()
            now = self._clock.now()
            if pending.status is ConfirmationStatus.PENDING and pending.expired(now):
                await expire_waiting(uow, pending, now)
                failure = ConfirmationExpired(
                    f"confirmation {pending.id} expired at {pending.expires_at.isoformat()}; "
                    "nothing was executed",
                    next_step="Ask the agent to request the action again.",
                    details={"status": ConfirmationStatus.EXPIRED.value},
                )
            elif not await uow.confirmations.transition(
                pending.id,
                expected=DECIDABLE,
                to=to,
                at=now,
                live_at=now,
                decided_by=CLI_ACTOR,
                channel=DecisionChannel.CLI,
            ):
                latest = await uow.confirmations.get(pending_id)
                failure = _not_pending(pending, pending.status if latest is None else latest.status_at(now))
            else:
                await uow.audit.append(
                    pending.user_id, decided_event(pending, to, DecisionChannel.CLI, CLI_ACTOR), now
                )
        if failure is not None:
            raise failure  # after the block: the expiry above must be committed, not rolled back
        decided = await self._get(pending_id)
        return decided if decided is not None else replace(pending, status=to)

    async def _get(self, pending_id: str) -> PendingConfirmation | None:
        async with self._uow(write=False) as uow:
            return await uow.confirmations.get(pending_id)


class CancelConfirmation:
    """The agent withdraws its own waiting or approved-but-not-started request (D-6, FR-47).

    Not an approval and never executes anything; it frees the slot. Another user's confirmation is
    ``not_found``, as an unknown one."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def __call__(self, user_id: str | None, pending_id: str) -> PendingConfirmation:
        failure: Exception | None = None
        async with self._uow(write=True) as uow:
            user = await active_principal(uow, user_id)
            if user is None:
                raise no_access()
            await uow.lock_user(user.id)
            pending = await uow.confirmations.get(pending_id)
            if pending is None or pending.user_id != user.id:
                raise not_found()
            now = self._clock.now()
            if await uow.confirmations.transition(
                pending.id,
                expected=INVALIDATABLE,
                to=ConfirmationStatus.CANCELLED,
                at=now,
                decided_by=AGENT_ACTOR,
                channel=DecisionChannel.AGENT,
            ):
                await uow.audit.append(
                    user.id,
                    decided_event(pending, ConfirmationStatus.CANCELLED, DecisionChannel.AGENT, AGENT_ACTOR),
                    now,
                )
            else:
                latest = await uow.confirmations.get(pending_id)
                failure = _not_pending(pending, pending.status if latest is None else latest.status_at(now))
        if failure is not None:
            raise failure
        return replace(pending, status=ConfirmationStatus.CANCELLED)


class GetConfirmationStatus:
    """The agent reads the state of its own confirmation (FR-47 (b))."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def __call__(self, user_id: str | None, pending_id: str) -> PendingConfirmation:
        async with self._uow(write=False) as uow:
            user = await active_principal(uow, user_id)
            if user is None:
                raise no_access()
            pending = await uow.confirmations.get(pending_id)
        if pending is None or pending.user_id != user.id:
            raise not_found()
        return replace(pending, status=pending.status_at(self._clock.now()))


class ListConfirmations:
    """What waits for the human, for the CLI: not expired ``pending``, ``approved`` and ``executing``."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def __call__(self) -> Sequence[PendingConfirmation]:
        async with self._uow(write=False) as uow:
            found = await uow.confirmations.list_by_status(SLOT_STATUSES)
        now = self._clock.now()
        return [p for p in found if p.status_at(now) in SLOT_STATUSES]


class ShowConfirmation:
    """One confirmation by id, for the human at the CLI (what they are about to decide, FR-21)."""

    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def __call__(self, pending_id: str) -> PendingConfirmation:
        async with self._uow(write=False) as uow:
            pending = await uow.confirmations.get(pending_id)
        if pending is None:
            raise not_found()
        return pending
