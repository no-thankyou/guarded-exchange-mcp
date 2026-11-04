"""The ConfirmedActionExecutor: executes what a human approved (SPEC FR-47 (b), FR-48; PLAN ADR-006, D-3).

A background task of the server process. Each pass (and a wake-up signal would only make it earlier):

1. ``pending`` confirmations past their expiry become ``expired`` (frees the slot, journaled).
2. ``approved`` ones are handed to their action path's ``run_confirmed``: the re-check of everything at
   execution time, the CAS ``approved -> executing`` (one executor wins) and the send with the
   deterministic client order id derived from the confirmation id. An approval older than
   ``APPROVAL_DEADLINE`` is ``expired_after_approval`` (D-3).
3. ``executing`` ones that nobody is finishing (the server stopped mid-send) are looked up on the exchange
   (``reconcile_confirmed``, FR-27) - never resent; a live one in this process is left alone.

MVP: one server process (SPEC 7.1). Several processes would each run an executor; the CAS keeps a
confirmation from being executed twice, but the restart rule (a second process taking over an
``executing`` one it does not own, fencing, leadership) is ticket 019.
"""

from collections.abc import Mapping
from typing import Protocol

import anyio
import structlog

from guarded_exchange.application.confirmation import SYSTEM_ACTOR, decided_event
from guarded_exchange.application.ports import Clock, UnitOfWorkFactory
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.audit import AuditEvent, AuditEventType, JsonValue
from guarded_exchange.domain.confirmation import (
    RECONCILE_AFTER,
    ConfirmationStatus,
    DecisionChannel,
    PendingConfirmation,
)

log = structlog.get_logger(__name__)

DEFAULT_SCAN_INTERVAL_S = 1.0
_BATCH = 50


class ConfirmablePath(Protocol):
    """What an action path offers the executor (``ActionPath`` does)."""

    async def run_confirmed(self, pending: PendingConfirmation) -> None: ...

    async def reconcile_confirmed(self, pending: PendingConfirmation) -> None: ...


class ExecuteConfirmed:
    """Executes one ``approved`` confirmation through its action path - the single way an approval becomes
    an action, whoever approved it: the background executor (CLI approvals) and the UI channel's second
    round (same call, FR-47 (a)) both come here. The path's CAS ``approved -> executing`` keeps it to one
    execution; a second caller finds nothing to do."""

    def __init__(
        self, uow: UnitOfWorkFactory, clock: Clock, paths: Mapping[ActionKind, ConfirmablePath]
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._paths = paths
        self.running: dict[str, int] = {}
        """Confirmations this process is executing right now (a count: a call that lost the CAS must not
        drop the winner's mark): not reconciled behind its back."""

    async def __call__(self, pending: PendingConfirmation) -> None:
        path = self._paths.get(pending.tool)
        if path is None:
            await self._fail_unsupported(pending)
            return
        self.running[pending.id] = self.running.get(pending.id, 0) + 1
        try:
            await path.run_confirmed(pending)
        except Exception:
            log.exception("confirmed_action_failed_to_run", confirmation_id=pending.id)
        finally:
            if self.running[pending.id] <= 1:
                del self.running[pending.id]
            else:
                self.running[pending.id] -= 1

    async def _fail_unsupported(self, pending: PendingConfirmation) -> None:
        """Fail closed: a tool with no registered path is never executed."""
        now = self._clock.now()
        reason: dict[str, JsonValue] = {
            "status": "refused",
            "code": "internal_error",
            "details": {"reason": "no_executor_for_tool", "tool": pending.tool.value},
        }
        async with self._uow(write=True) as uow:
            await uow.lock_user(pending.user_id)
            if await uow.confirmations.transition(
                pending.id,
                expected=frozenset({ConfirmationStatus.APPROVED}),
                to=ConfirmationStatus.FAILED,
                at=now,
                channel=DecisionChannel.SYSTEM,
                result={"outcome": reason},
            ):
                payload: dict[str, JsonValue] = {
                    "tool": pending.tool.value,
                    "action_id": pending.id,
                    "confirmation_id": pending.id,
                    "stage": "executor",
                    "code": "internal_error",
                    "details": {"reason": "no_executor_for_tool"},
                }
                await uow.audit.append(
                    pending.user_id, AuditEvent(AuditEventType.ORDER_REFUSED, SYSTEM_ACTOR, payload), now
                )


class ConfirmedActionExecutor:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        clock: Clock,
        paths: Mapping[ActionKind, ConfirmablePath],
        *,
        scan_interval_s: float = DEFAULT_SCAN_INTERVAL_S,
        execute: ExecuteConfirmed | None = None,
    ) -> None:
        """``execute``: share it with the UI channel so both know what this process is executing."""
        self._uow = uow
        self._clock = clock
        self._paths = paths
        self._scan_interval_s = scan_interval_s
        self._execute_confirmed = execute or ExecuteConfirmed(uow, clock, paths)

    async def run(self, stop: anyio.Event | None = None) -> None:
        """Scan until ``stop`` is set (or the task is cancelled). A failing pass is logged and the next
        one tries again.

        ``stop`` is the graceful way out: it is only looked at between passes, so a pass is never cut
        in the middle of a database call (cancelling an aiosqlite call mid-way leaves its worker thread
        posting to a closed event loop, and the process then never exits).
        """
        stop = stop or anyio.Event()
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("confirmed_executor_pass_failed")
            with anyio.move_on_after(self._scan_interval_s):
                await stop.wait()

    async def run_once(self) -> None:
        await self._expire_waiting()
        approved = await self._listed({ConfirmationStatus.APPROVED})
        for pending in approved:
            await self._execute_confirmed(pending)
        now = self._clock.now()
        for pending in await self._listed({ConfirmationStatus.EXECUTING}):
            started = pending.exec_started_at
            if (
                pending.id in self._execute_confirmed.running
                or started is None
                or now < started + RECONCILE_AFTER
            ):
                continue
            await self._reconcile(pending)

    async def _listed(self, statuses: set[ConfirmationStatus]) -> list[PendingConfirmation]:
        async with self._uow(write=False) as uow:
            return list(await uow.confirmations.list_by_status(frozenset(statuses), limit=_BATCH))

    async def _expire_waiting(self) -> None:
        now = self._clock.now()
        for pending in await self._listed({ConfirmationStatus.PENDING}):
            if not pending.expired(now):
                continue
            async with self._uow(write=True) as uow:
                await uow.lock_user(pending.user_id)
                if await uow.confirmations.transition(
                    pending.id,
                    expected=frozenset({ConfirmationStatus.PENDING}),
                    to=ConfirmationStatus.EXPIRED,
                    at=now,
                    channel=DecisionChannel.SYSTEM,
                    decided_by=SYSTEM_ACTOR,
                ):
                    event = decided_event(
                        pending, ConfirmationStatus.EXPIRED, DecisionChannel.SYSTEM, SYSTEM_ACTOR
                    )
                    await uow.audit.append(pending.user_id, event, now)

    async def _reconcile(self, pending: PendingConfirmation) -> None:
        path = self._paths.get(pending.tool)
        if path is None:
            return
        try:
            await path.reconcile_confirmed(pending)
        except Exception:
            log.exception("confirmed_action_reconciliation_failed", confirmation_id=pending.id)
