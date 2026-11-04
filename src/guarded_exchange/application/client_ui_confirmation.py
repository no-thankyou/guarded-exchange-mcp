"""The client-UI channel of a confirmation: the human's answer to the form the server asked for in the
same call (SPEC FR-20 - FR-22, FR-42, FR-47 (a); PLAN ADR-005, D-1, D-2, D-4, D-6).

It feeds the **same** pending record and the **same** execution as the CLI channel; there is no second
path to an action. What is different is only who may call it: the MCP layer, in the second round of a
trading call, with the sealed ``requestState`` the server minted in the first. A decision here is:

- bound to the pending: the state carries the ``terms_digest`` of what the human was shown, and it must
  still equal the stored one (a state minted for other terms, another tool or another user finds
  nothing);
- allowed only while the policy trusts the client's UI **now** (``trust_client_ui``, not
  ``prefer_external_channel``): a mainnet user's form is never a confirmation (FR-42);
- one CAS ``pending -> approved|rejected|cancelled`` under ``lock_user``, so of a form answer and a CLI
  approval exactly one wins and the loser is told the state it found (D-2);
- followed, for an approval the caller won, by the shared ``ExecuteConfirmed``.

The model cannot call this: it is not an MCP tool, and the answer comes from the client's own
``elicitation`` response, which a tool call cannot produce (AC-4).
"""

from dataclasses import replace
from enum import StrEnum

from guarded_exchange.application.confirmation import CLIENT_UI_ACTOR, ConfirmationSupport, decided_event
from guarded_exchange.application.execute_confirmed import ExecuteConfirmed
from guarded_exchange.application.policy import active_principal, load_policy_view, no_access
from guarded_exchange.application.ports import Clock, UnitOfWork, UnitOfWorkFactory
from guarded_exchange.application.resolve_confirmation import expire_waiting, not_found
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.confirmation import (
    DECIDABLE,
    ConfirmationStatus,
    DecisionChannel,
    PendingConfirmation,
    intact,
)
from guarded_exchange.domain.errors import ConfirmationChannelUnavailable, PermissionDenied


class UiAnswer(StrEnum):
    """What the human's form answer means, after the MCP layer read it fail-closed."""

    CONFIRM = "confirm"
    DECLINE = "decline"
    """Declined, or submitted without ticking the box, or any answer that is not a clear confirmation."""
    CANCEL = "cancel"
    """The human closed the form (D-4): the request is withdrawn."""


_TARGET = {
    UiAnswer.CONFIRM: ConfirmationStatus.APPROVED,
    UiAnswer.DECLINE: ConfirmationStatus.REJECTED,
    UiAnswer.CANCEL: ConfirmationStatus.CANCELLED,
}


def _not_trusted() -> ConfirmationChannelUnavailable:
    return ConfirmationChannelUnavailable(
        "this policy does not accept confirmations from the AI client's own UI",
        next_step="Do not retry. Tell the user to decide with `guarded-exchange confirm`.",
        details={"reason": "client_ui_not_trusted"},
    )


class ClientUiConfirmation:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        clock: Clock,
        support: ConfirmationSupport,
        execute: ExecuteConfirmed,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._support = support
        self._execute = execute

    @property
    def execute(self) -> ExecuteConfirmed:
        return self._execute

    async def current(
        self, user_id: str | None, tool: ActionKind, pending_id: str, terms_digest: str
    ) -> PendingConfirmation:
        """The bound pending as it stands now (for a state-only retry: ask the same question again). Refused
        like a decision when the policy no longer trusts the client's UI; a pending past its expiry reads
        as ``expired``."""
        async with self._uow(write=False) as uow:
            pending = await self._bound(uow, user_id, tool, pending_id, terms_digest)
            user = await active_principal(uow, user_id)
            if user is None:
                raise no_access()
            policy = (await load_policy_view(uow, user)).effective
        if pending.status is ConfirmationStatus.PENDING and not self._support.client_ui_trusted(policy):
            raise _not_trusted()
        return replace(pending, status=pending.status_at(self._clock.now()))

    async def decide(
        self,
        user_id: str | None,
        tool: ActionKind,
        pending_id: str,
        terms_digest: str,
        answer: UiAnswer,
        *,
        note: str,
    ) -> PendingConfirmation:
        """Apply the human's answer. Returns the pending as it stands afterwards: for an approval that
        this call won, already executed (or executing, if the background executor was faster). A pending
        that was no longer waiting is returned as found - the caller reports its state, nothing is redone."""
        async with self._uow(write=True) as uow:
            first = await self._bound(uow, user_id, tool, pending_id, terms_digest)
            await uow.lock_user(first.user_id)
            pending = await self._bound(uow, user_id, tool, pending_id, terms_digest)
            user = await active_principal(uow, user_id)
            if user is None:
                raise no_access()
            policy = (await load_policy_view(uow, user)).effective
            now = self._clock.now()
            if pending.status is not ConfirmationStatus.PENDING:
                return replace(pending, status=pending.status_at(now))
            if not self._support.client_ui_trusted(policy):
                # Not a decision: the pending stays for a channel the policy does trust (FR-42, D-6).
                raise _not_trusted()
            if pending.expired(now):
                await expire_waiting(uow, pending, now)
                return replace(pending, status=ConfirmationStatus.EXPIRED)
            target = _TARGET[answer]
            if await uow.confirmations.transition(
                pending.id,
                expected=DECIDABLE,
                to=target,
                at=now,
                live_at=now,
                decided_by=CLIENT_UI_ACTOR,
                channel=DecisionChannel.CLIENT_UI,
            ):
                await uow.audit.append(
                    pending.user_id,
                    decided_event(pending, target, DecisionChannel.CLIENT_UI, CLIENT_UI_ACTOR, note=note),
                    now,
                )
            else:
                latest = await uow.confirmations.get(pending.id)
                return pending if latest is None else replace(latest, status=latest.status_at(now))
        if answer is UiAnswer.CONFIRM:
            await self._execute(replace(pending, status=ConfirmationStatus.APPROVED))
        return await self._reload(pending, target)

    async def _reload(self, pending: PendingConfirmation, target: ConfirmationStatus) -> PendingConfirmation:
        async with self._uow(write=False) as uow:
            latest = await uow.confirmations.get(pending.id)
        return replace(pending, status=target) if latest is None else latest

    @staticmethod
    async def _bound(
        uow: UnitOfWork, user_id: str | None, tool: ActionKind, pending_id: str, terms_digest: str
    ) -> PendingConfirmation:
        """The pending this state was minted for, or ``not_found`` (same answer for every mismatch)."""
        user = await active_principal(uow, user_id)
        if user is None:
            raise no_access()
        pending = await uow.confirmations.get(pending_id)
        if pending is None or pending.user_id != user.id or pending.tool is not tool:
            raise not_found()
        if pending.terms_digest != terms_digest or not intact(pending):
            raise PermissionDenied(
                "this confirmation does not match what the form was built for; nothing was executed",
                next_step="Do not retry. The user can look at it with `guarded-exchange confirm list`.",
                details={"reason": "confirmation_terms_changed"},
            )
        return pending
