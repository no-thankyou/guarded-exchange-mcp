"""The single path of a trading action (SPEC FR-4, FR-5, FR-10, FR-13 - FR-15, FR-17, FR-25 - FR-27,
FR-36, FR-43; PLAN ADR-008, ADR-010, ADR-014, D-8, D-10, D-15, D-19, D-20, D-21).

auth -> repeat check (idempotency key, or the 30 s fingerprint window of a risk-increasing call) ->
key check (D-24) -> exchange reads -> plan (rules, rounding, worst price) -> one write transaction
under ``lock_user``: fence the idempotency claim, re-check access, key, policy, kill switch, limits,
confirmation, journal the refusal or the attempt -> send outside any transaction, with an overall
deadline -> on an unknown outcome, find out by reading the exchange (never resend) -> journal the
outcome and store the answer for repeats, in one transaction.

``ActionPath`` is that path once, for every action; an action plugs in only what is its own: its
canonical arguments, the exchange reads and the plan, the policy check under the lock, and how it
is sent and its outcome found out. ``SubmitPlaceOrder`` is the order (ticket 011/012); cancel, amend
and cancel-all are in ``order_changes`` (ticket 013).

Network I/O never happens inside a database transaction (D-10): everything the decision needs from
the exchange is read first; the decision and its journal record are one transaction, so a refusal
(FR-17) or an attempt is recorded exactly when it is decided. The attempt is committed before the
request leaves, the outcome after the answer (ADR-008). Confirmation by a human is not built yet:
if the policy requires it, the action is refused with ``confirmation_channel_unavailable`` and
nothing is sent (FR-25, fail closed); trading without confirmation needs the admin to say so.

Repeats (FR-26, ADR-014): a call claims its idempotency record before anything else touches the
exchange; a repeat with the same key gets the first answer (or "still in progress"), never a second
order; the same key with other arguments is ``idempotency_conflict``. The claim is released when the
call ends before its decision or with a transient failure; from the decision on its answer is
stored (``domain.idempotency.keeps_record``). A new attempt needs a new key.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

import anyio
import anyio.lowlevel
import structlog

from guarded_exchange.application.credentials import RequireTradingCredential
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.order_results import (
    OrderResult,
    ResultSource,
    answer_payload,
    restore_answer,
)
from guarded_exchange.application.policy import active_principal, load_policy_view, no_access
from guarded_exchange.application.ports import (
    Clock,
    ExchangeClient,
    IdGenerator,
    TradingExchange,
    UnitOfWork,
    UnitOfWorkFactory,
)
from guarded_exchange.domain.account import SymbolPosition
from guarded_exchange.domain.actions import ActionKind, OrderKind, PlaceOrder, parse_place_order
from guarded_exchange.domain.audit import AuditEvent, AuditEventType, ClientDescription, JsonValue
from guarded_exchange.domain.credentials import ApiKeyPair, key_replaced_or_deleted
from guarded_exchange.domain.errors import (
    CredentialsPermissionsChanged,
    DomainError,
    ExchangeCredentialsInvalid,
    ExchangeUnavailable,
    IdempotencyConflict,
    InstrumentRuleViolation,
    InvalidParams,
    OutcomeUnknown,
    PermissionDenied,
    PositionNotFound,
    RateLimited,
)
from guarded_exchange.domain.idempotency import (
    FINGERPRINT_WINDOW,
    IN_FLIGHT_GUARD,
    IdempotencyRecord,
    KeyKind,
    Outcome,
    RecordStatus,
    expiry_when_done,
    fingerprint_applies,
    is_stale,
    keeps_record,
    new_claim,
    parse_idempotency_key,
    request_hash,
)
from guarded_exchange.domain.instruments import Category, InstrumentRules, require_supported_quote
from guarded_exchange.domain.kill_switch import require_allowed
from guarded_exchange.domain.limits import (
    check_limits,
    no_confirmation_channel,
    require_trading_environment,
    requires_confirmation,
)
from guarded_exchange.domain.orders import (
    BestPrices,
    OrderFill,
    OrderPlan,
    OrderRequest,
    PlacedOrder,
    PriceBand,
    plan_order,
)
from guarded_exchange.domain.policy import EffectivePolicy, Permission, require_permission
from guarded_exchange.domain.risk import RiskDirection, classify
from guarded_exchange.domain.users import Environment, User

__all__ = [
    "AGENT_ACTOR",
    "ActionPath",
    "Attempt",
    "OrderResult",
    "PlaceOrderInput",
    "ResultSource",
    "SubmitPlaceOrder",
]

log = structlog.get_logger(__name__)

AGENT_ACTOR: Final = "mcp:agent"
"""The journal's actor for actions an AI agent asked for (the user is the chain)."""

INPUT_ERRORS: Final = (InvalidParams, InstrumentRuleViolation, PositionNotFound)
"""Errors in the request itself: answered before the decision and not journaled (PLAN, ticket 011 (5))."""

TRANSIENT_ERRORS: Final = (
    ExchangeUnavailable,
    RateLimited,
    ExchangeCredentialsInvalid,
    CredentialsPermissionsChanged,
)
"""Refusals about conditions, not about the action: once they are fixed, the same call may pass, so
its idempotency record is released instead of answering it from storage (``keeps_record``)."""

_LOOKED_UP: Final = {
    "not_found": "; it was not found by its client order id (yet)",
    "failed": "; looking it up by its client order id failed",
}

_KEYED_REPEAT: Final = (
    "Repeating this exact call with the same idempotency_key returns this answer and never sends a "
    "second order."
)
_KEYLESS_REPEAT: Final = (
    "Without an idempotency_key, repeating this call later places a NEW order: first check "
    "get_open_orders and get_order_history for this client_order_id."
)

DEFAULT_SEND_DEADLINE_S: Final = 15.0
"""Overall bound of one order request, whatever the HTTP client's per-phase timeouts allow."""
DEFAULT_RECONNAISSANCE_DELAY_S: Final = 1.0
"""Pause before looking the order up after an unknown outcome: the exchange's lists lag behind."""
DEFAULT_RECONNAISSANCE_DEADLINE_S: Final = 15.0
DEFAULT_FILL_READ_DEADLINE_S: Final = 5.0
"""Bound of reading an accepted order back; after it the fill is "unknown", not an error."""


class TradingAction(Protocol):
    """What the path needs of any action: its kind (the tool, the policy's confirmation rule)."""

    @property
    def kind(self) -> ActionKind: ...


@dataclass(frozen=True, slots=True)
class PlaceOrderInput:
    """Raw arguments as an entrypoint received them; parsed by the domain."""

    symbol: str
    category: str
    side: str
    order_type: str
    qty: str
    price: str | None = None
    reduce_only: bool = False
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class _Market:
    """Everything the decision needs from the exchange, read before the transaction (D-10)."""

    rules: InstrumentRules
    band: PriceBand
    best: BestPrices | None
    symbol_position: SymbolPosition | None


@dataclass(frozen=True, slots=True)
class _Claims:
    """The records a call holds. ``primary`` fences the call (its key, or its fingerprint without a
    key); ``shadow`` is the fingerprint a keyed call also holds when it was free, so that a keyless
    repeat from another client is answered too (review 1). Only the primary may stop a send."""

    primary: IdempotencyRecord | None = None
    shadow: IdempotencyRecord | None = None

    def held(self) -> tuple[IdempotencyRecord, ...]:
        return tuple(r for r in (self.primary, self.shadow) if r is not None)


@dataclass(frozen=True, slots=True)
class Attempt:
    """One decided call: who, which credential, which records it holds. ``client_order_id`` only for
    an action that creates an order (D-19)."""

    user_id: str
    action_id: str
    client_order_id: str | None
    credential_id: str
    environment: Environment
    client: ClientDescription | None
    claims: _Claims


@dataclass(frozen=True, slots=True)
class _Cleared:
    credential_id: str
    keys: ApiKeyPair


class ActionPath[A: TradingAction, M, P, R](ABC):
    """The path every trading action takes; ``A`` is the action, ``M`` what is read from the exchange
    before the decision, ``P`` the decided plan, ``R`` the answer. Subclasses implement the hooks."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        credentials: RequireTradingCredential,
        *,
        clock: Clock,
        ids: IdGenerator,
        exchange: str,
        environment: Environment,
        actor: str = AGENT_ACTOR,
        send_deadline_s: float = DEFAULT_SEND_DEADLINE_S,
        reconnaissance_delay_s: float = DEFAULT_RECONNAISSANCE_DELAY_S,
        reconnaissance_deadline_s: float = DEFAULT_RECONNAISSANCE_DEADLINE_S,
        fingerprint_window: timedelta = FINGERPRINT_WINDOW,
        fill_read_deadline_s: float = DEFAULT_FILL_READ_DEADLINE_S,
    ) -> None:
        self._uow = uow
        self._credentials = credentials
        self._clock = clock
        self._ids = ids
        self._exchange = exchange
        self._environment = environment
        self._actor = actor
        self._send_deadline_s = send_deadline_s
        self._reconnaissance_delay_s = reconnaissance_delay_s
        self._reconnaissance_deadline_s = reconnaissance_deadline_s
        self._fingerprint_window = fingerprint_window
        self._fill_read_deadline_s = fill_read_deadline_s

    # --- hooks ---------------------------------------------------------------------------------

    @abstractmethod
    def _payload(self, action: A) -> dict[str, JsonValue]:
        """Every argument, defaults included: the journal's ``request`` and the canonical call of the
        idempotency hash (D-4)."""

    @abstractmethod
    def _deduplicated_without_key(self, action: A) -> bool:
        """Whether an identical call without a key within the window is a repeat (D-20 (3))."""

    @abstractmethod
    async def _read(self, action: A, keys: ApiKeyPair, policy: EffectivePolicy) -> M:
        """Everything read from the exchange before the decision, and the early checks of the request
        (an input error here is answered without a journal record)."""

    @abstractmethod
    def _risk_before_reads(self, action: A) -> RiskDirection | None:
        """The risk direction (FR-43) when the arguments alone tell it; ``None``: only the exchange's
        state does (an amend compares with the current order)."""

    @abstractmethod
    def _risk(self, action: A, market: M) -> RiskDirection:
        """The risk direction given what was read before the decision."""

    @abstractmethod
    def _check(self, action: A, market: M, policy: EffectivePolicy) -> P:
        """Under ``lock_user`` with the fresh policy: the plan, the limits, the confirmation rule.
        Raises the refusal; no I/O."""

    @abstractmethod
    def _attempt_details(self, plan: P) -> dict[str, JsonValue]:
        """What the ``order_attempt`` record says about the decided plan."""

    @abstractmethod
    async def _execute(self, attempt: Attempt, plan: P, keys: ApiKeyPair) -> R:
        """Send (outside any transaction, shielded and bounded), find out, record the outcome."""

    @abstractmethod
    def _store(self, answer: R | DomainError) -> dict[str, JsonValue]:
        """The answer as stored for repeats."""

    @abstractmethod
    def _restore(self, payload: Mapping[str, Any]) -> R | DomainError:
        """A stored answer, marked as a replay."""

    @abstractmethod
    def _in_progress(self, held: IdempotencyRecord) -> OutcomeUnknown:
        """The answer to a repeat while the first call has not answered yet."""

    def _client_order_id(self, action_id: str) -> str | None:
        """The client order id of an action that creates an order; ``None`` for the others."""
        return None

    def _superseded(self, attempt: Attempt, holder: IdempotencyRecord | None) -> OutcomeUnknown:
        """The call's record expired before its decision: it sends nothing."""
        details = {"reason": "superseded"}
        return OutcomeUnknown(
            "this call took too long before sending: nothing was sent to the exchange",
            next_step="Check get_open_orders for the current state, then tell the user before trying again.",
            details=details,
        )

    # --- the path ------------------------------------------------------------------------------

    async def _submit(
        self, user_id: str | None, action: A, raw_key: str | None, client: ClientDescription | None
    ) -> R:
        key = None if raw_key is None else parse_idempotency_key(raw_key)
        user = await self._principal(user_id)  # unknown or revoked: nothing to journal it under
        try:
            policy = await self._authorize(user)
        except DomainError as refusal:  # FR-36: every attempt of a known user, refused ones too
            await self._journal_early_refusal(user, action, refusal, client, None)
            raise
        action_id = self._ids.new_id()
        # D-20 (1): the repeat check comes before anything that could refuse or slow a repeat.
        wanted = self._new_claims(user, action, key, action_id)
        claims = _Claims()
        if wanted.primary is not None:
            with anyio.CancelScope(shield=True):  # a committed claim is always released or settled
                held, claims = await self._claim(wanted)
            if not held.held_by(action_id):
                return await self._repeat(user, action, wanted.primary, held, client)
        try:
            cleared, market = await self._prepare(user, action, policy, claims.primary, client)
            # A cancellation that arrived before the decision is honoured here; from now on a
            # committed attempt must be followed by the send and the outcome, so the decision is
            # shielded too (its transaction is short and bounded by SQLite's busy_timeout).
            await anyio.lowlevel.checkpoint_if_cancelled()
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self._try_release(claims)  # nothing was decided: a repeat may run afresh
            raise
        attempt = Attempt(
            user_id=user.id,
            action_id=action_id,
            client_order_id=self._client_order_id(action_id),
            credential_id=cleared.credential_id,
            environment=user.environment,
            client=client,
            claims=claims,
        )
        with anyio.CancelScope(shield=True):
            try:
                decided = await self._decide(attempt, action, market)
            except BaseException:  # the decision did not commit: nothing journaled, nothing sent
                await self._try_release(claims)
                raise
        if isinstance(decided, DomainError):
            raise decided
        return await self._execute(attempt, decided, cleared.keys)

    # --- repeats (FR-26, ADR-014, D-20) --------------------------------------------------------

    def _new_claims(self, user: User, action: A, key: str | None, action_id: str) -> _Claims:
        digest = request_hash(action.kind.value, self._payload(action), user.environment)
        now = self._clock.now()

        def record(kind: KeyKind, key: str) -> IdempotencyRecord:
            return new_claim(
                user_id=user.id, kind=kind, key=key, request_hash=digest, action_id=action_id, now=now
            )

        # A risk-reducing call is never deduplicated by its fingerprint (D-20 (3)).
        fingerprint = record(KeyKind.FINGERPRINT, digest) if self._deduplicated_without_key(action) else None
        if key is None:
            return _Claims(primary=fingerprint)
        return _Claims(primary=record(KeyKind.EXPLICIT, key), shadow=fingerprint)

    async def _claim(self, wanted: _Claims) -> tuple[IdempotencyRecord, _Claims]:
        """One transaction: the primary record, then (if the primary is ours) the fingerprint of a
        keyed call when it is free; a busy fingerprint is not consulted by a keyed call (ladders)."""
        primary = wanted.primary
        if primary is None:
            raise ValueError("nothing to claim")
        async with self._uow(write=True) as uow:
            now = self._clock.now()
            held = await uow.idempotency.claim(primary, now)
            if not held.held_by(primary.action_id):
                return held, _Claims()
            shadow = None
            if wanted.shadow is not None:
                taken = await uow.idempotency.claim(wanted.shadow, now)
                shadow = wanted.shadow if taken.held_by(wanted.shadow.action_id) else None
            return held, _Claims(primary=primary, shadow=shadow)

    async def _repeat(
        self,
        user: User,
        action: A,
        claim: IdempotencyRecord,
        held: IdempotencyRecord,
        client: ClientDescription | None,
    ) -> R:
        """A live record holds the key: answer from it, never act again."""
        if held.request_hash != claim.request_hash:  # only an explicit key: a fingerprint is the hash
            conflict = IdempotencyConflict(
                "this idempotency_key was already used for a call with other arguments; nothing was sent",
                next_step="Use a new idempotency_key for a new action; reuse a key only to repeat exactly "
                "the same call.",
                details={"reason": "different_arguments"},
            )
            await self._journal_early_refusal(user, action, conflict, client, claim)
            raise conflict
        log.info(
            "idempotent_repeat",
            kind=held.kind.value,
            status=held.status.value,
            action_id=held.action_id,
        )
        status = "in_progress" if held.result is None else "replay"
        await self._journal_repeat(user, action, held, client, status)
        if held.result is None:  # in_progress or sent: the first call has not answered yet
            raise self._in_progress(held)
        answer = self._restore(held.result)
        if isinstance(answer, DomainError):
            if held.kind is KeyKind.FINGERPRINT and answer.next_step:
                # Stored by a keyed call: this caller has no key, so a later repeat is a new order.
                answer.next_step = answer.next_step.replace(_KEYED_REPEAT, _KEYLESS_REPEAT)
            raise answer
        return answer

    async def _journal_repeat(
        self,
        user: User,
        action: A,
        held: IdempotencyRecord,
        client: ClientDescription | None,
        status: str,
    ) -> None:
        """FR-36: a repeat is a call of a known user too; nothing was sent, no exchange was asked."""
        payload: dict[str, JsonValue] = {
            "tool": action.kind.value,
            "status": status,
            "holder_action_id": held.action_id,
            "environment": user.environment.value,
            "client": None if client is None else client.to_payload(),
            "idempotency": _claim_payload(held),
        }
        client_order_id = self._client_order_id(held.action_id)
        if client_order_id is not None:
            payload["client_order_id"] = client_order_id
        try:
            async with self._uow(write=True) as uow:
                await uow.audit.append(
                    user.id,
                    AuditEvent(AuditEventType.ORDER_REPEAT, self._actor, payload),
                    self._clock.now(),
                )
        except Exception:
            log.exception("order_repeat_not_recorded", action_id=held.action_id)

    # --- before the decision ------------------------------------------------------------------

    async def _principal(self, user_id: str | None) -> User:
        async with self._uow(write=False) as uow:
            user = await active_principal(uow, user_id)
        if user is None:
            raise no_access()
        return user

    async def _authorize(self, user: User) -> EffectivePolicy:
        self._require_environment(user)
        async with self._uow(write=False) as uow:
            view = await load_policy_view(uow, user)
        require_permission(view.effective, Permission.TRADE)
        return view.effective

    async def _prepare(
        self,
        user: User,
        action: A,
        policy: EffectivePolicy,
        claim: IdempotencyRecord | None,
        client: ClientDescription | None,
    ) -> tuple[_Cleared, M]:
        """The key check and everything read from the exchange before the decision. Refusals of a
        known user are journaled (FR-36); errors in the request itself are not."""
        try:
            # FR-16, after the repeat check: a repeat still gets the first call's answer.
            early = self._risk_before_reads(action)
            if early is not None:
                require_allowed(policy.kill_switch, early, action.kind)
            cleared = await self._credentials(user.id)  # D-24 re-check if stale; may call the exchange
            market = await self._read(action, cleared.keys, policy)
            if early is None:  # known only now, e.g. an amend against the current order
                require_allowed(policy.kill_switch, self._risk(action, market), action.kind)
        except INPUT_ERRORS:
            raise
        except DomainError as refusal:  # incl. "no best price" from the plan (ticket 011 review 3)
            await self._journal_early_refusal(user, action, refusal, client, claim)
            raise
        return _Cleared(cleared.credential.id, cleared.keys), market

    async def _journal_early_refusal(
        self,
        user: User,
        action: A,
        refusal: DomainError,
        client: ClientDescription | None,
        claim: IdempotencyRecord | None,
    ) -> None:
        """A failure to journal must not replace the refusal itself with ``internal_error``."""
        payload: dict[str, JsonValue] = {
            "tool": action.kind.value,
            "stage": "idempotency" if isinstance(refusal, IdempotencyConflict) else "pre_check",
            "environment": user.environment.value,
            "client": None if client is None else client.to_payload(),
            "request": self._payload(action),
            "idempotency": _claim_payload(claim),
            "code": refusal.code,
            "details": dict(refusal.details),
        }
        try:
            async with self._uow(write=True) as uow:
                await uow.audit.append(
                    user.id,
                    AuditEvent(AuditEventType.ORDER_REFUSED, self._actor, payload),
                    self._clock.now(),
                )
        except Exception:
            log.exception("order_refused_not_recorded", code=refusal.code)

    def _require_environment(self, user: User) -> None:
        if user.environment is not self._environment:
            raise PermissionDenied(
                f"your account is on {user.environment.value}, but this server is connected to the "
                f"{self._environment.value} exchange environment",
                next_step="Do not retry. Tell the user; the admin runs a server for their environment.",
                details={"reason": "environment_mismatch"},
            )
        require_trading_environment(user.environment)

    # --- the decision: one write transaction ---------------------------------------------------

    async def _decide(self, attempt: Attempt, action: A, market: M) -> P | DomainError:
        """Refusal or plan, journaled in the same transaction that decided it (FR-17, ADR-008); the
        idempotency record moves to ``sent`` (or gets the refusal) in it too."""
        async with self._uow(write=True) as uow:
            await uow.lock_user(attempt.user_id)
            now = self._clock.now()
            primary = attempt.claims.primary
            if primary is not None and not await uow.idempotency.mark_sent(primary, now):
                # The record expired while this call was stuck before its decision (and may have been
                # taken over by another call, which answers for the key now). Never send twice.
                superseded = self._superseded(attempt, await uow.idempotency.current(primary))
                await uow.audit.append(
                    attempt.user_id, self._refused(attempt, action, superseded), self._clock.now()
                )
                return superseded
            if attempt.claims.shadow is not None:
                await self._keep_shadow(uow, attempt.claims.shadow, now)
            try:
                plan = await self._decide_locked(uow, attempt, action, market)
            except DomainError as refusal:
                await uow.audit.append(
                    attempt.user_id, self._refused(attempt, action, refusal), self._clock.now()
                )
                await self._settle(uow, attempt.claims, refusal_outcome(refusal), refusal)
                return refusal
            await uow.audit.append(attempt.user_id, self._attempted(attempt, action, plan), self._clock.now())
            return plan

    @staticmethod
    async def _keep_shadow(uow: UnitOfWork, shadow: IdempotencyRecord, now: datetime) -> None:
        """The keyed call's fingerprint follows it to ``sent`` without fencing the send. If it expired
        while the call was stuck, take it again (when free), so a keyless identical call during this
        send is a repeat and not a second order (review 2)."""
        if await uow.idempotency.mark_sent(shadow, now):
            return
        fresh = replace(shadow, created_at=now, expires_at=now + IN_FLIGHT_GUARD)
        held = await uow.idempotency.claim(fresh, now)
        if held.held_by(shadow.action_id):
            await uow.idempotency.mark_sent(fresh, now)

    async def _decide_locked(self, uow: UnitOfWork, attempt: Attempt, action: A, market: M) -> P:
        # Everything stored may have changed since the checks above (D-8): access, key, policy.
        user = await active_principal(uow, attempt.user_id)
        if user is None:
            raise no_access()
        self._require_environment(user)
        current = await uow.credentials.get_active(user.id, self._exchange, user.environment)
        if current is None or current.id != attempt.credential_id:
            raise key_replaced_or_deleted()
        policy = (await load_policy_view(uow, user)).effective
        require_permission(policy, Permission.TRADE)
        # FR-16, as of now (D-8): the switch may have been turned on since the early check.
        require_allowed(policy.kill_switch, self._risk(action, market), action.kind)
        return self._check(action, market, policy)

    # --- sending and the outcome ----------------------------------------------------------------

    async def _send_within_deadline[T](
        self, send: Callable[[], Awaitable[T]], seconds: float | None = None
    ) -> T:
        """One request, never repeated (ADR-002), bounded as a whole: the HTTP client's read timeout
        only limits the silence between chunks, so a slowly dripping answer could hold the shield."""
        bound = self._send_deadline_s if seconds is None else seconds
        try:
            with anyio.fail_after(bound):
                return await send()
        except TimeoutError:
            raise OutcomeUnknown(
                f"no answer from the exchange within {bound:g} s",
                details={"reason": "send_deadline"},
            ) from None

    async def _try_record_outcome(
        self,
        attempt: Attempt,
        outcome: Mapping[str, JsonValue],
        kind: Outcome,
        answer: R | DomainError,
        *,
        order_id: str | None = None,
    ) -> None:
        """Journal ``order_outcome`` and store the answer for repeats, in one transaction. A failure
        here must not replace the call's own answer (an ``internal_error`` instead of
        ``outcome_unknown`` could make the agent act again): it is logged, and the idempotency record
        stays ``sent``, so a repeat is told "in progress", never sent again."""
        payload: dict[str, JsonValue] = {"action_id": attempt.action_id}
        if attempt.client_order_id is not None:
            payload["client_order_id"] = attempt.client_order_id
        payload.update(outcome)
        try:
            async with self._uow(write=True) as uow:
                await uow.audit.append(
                    attempt.user_id,
                    AuditEvent(AuditEventType.ORDER_OUTCOME, self._actor, payload),
                    self._clock.now(),
                )
                await self._settle(uow, attempt.claims, kind, answer)
        except Exception:
            log.exception(
                "order_outcome_not_recorded",
                action_id=attempt.action_id,
                client_order_id=attempt.client_order_id,
                order_id=order_id,
                status=outcome.get("status"),
            )

    async def _settle(
        self,
        uow: UnitOfWork,
        claims: _Claims,
        outcome: Outcome,
        answer: R | DomainError,
    ) -> None:
        """Store the answer for repeats, or release the key (``keeps_record``), per held record."""
        status = RecordStatus.UNKNOWN if outcome is Outcome.UNKNOWN else RecordStatus.COMPLETED
        for record in claims.held():
            if not keeps_record(record.kind, outcome):
                await uow.idempotency.release(record)
                continue
            expires_at = expiry_when_done(record, self._clock.now(), self._fingerprint_window, outcome)
            await uow.idempotency.finish(record, status, self._store(answer), expires_at)

    async def _try_release(self, claims: _Claims) -> None:
        held = claims.held()
        if not held:
            return
        try:
            async with self._uow(write=True) as uow:
                for record in held:
                    await uow.idempotency.release(record)
        except Exception:  # the key stays claimed until it expires: fail closed
            log.exception("idempotency_claim_not_released", action_id=held[0].action_id)

    # --- journal payloads ------------------------------------------------------------------------

    def _base(self, attempt: Attempt, action: A) -> dict[str, JsonValue]:
        base: dict[str, JsonValue] = {
            "tool": action.kind.value,
            "action_id": attempt.action_id,
            "credential_id": attempt.credential_id,
            "environment": attempt.environment.value,
            "client": None if attempt.client is None else attempt.client.to_payload(),
            "request": self._payload(action),
            "idempotency": _claim_payload(attempt.claims.primary),
        }
        if attempt.client_order_id is not None:
            base["client_order_id"] = attempt.client_order_id
        return base

    def _refused(self, attempt: Attempt, action: A, refusal: DomainError) -> AuditEvent:
        payload = {**self._base(attempt, action), "code": refusal.code, "details": dict(refusal.details)}
        return AuditEvent(AuditEventType.ORDER_REFUSED, self._actor, payload)

    def _attempted(self, attempt: Attempt, action: A, plan: P) -> AuditEvent:
        payload = {
            **self._base(attempt, action),
            "decision": "allowed_without_confirmation",
            **self._attempt_details(plan),
        }
        return AuditEvent(AuditEventType.ORDER_ATTEMPT, self._actor, payload)


class SubmitPlaceOrder(ActionPath[PlaceOrder, _Market, OrderPlan, OrderResult]):
    """Place one market or limit order for the calling user (testnet only in v1, SPEC 7.1)."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        credentials: RequireTradingCredential,
        rules: InstrumentRulesCache,
        *,
        market: ExchangeClient,
        trading: TradingExchange,
        clock: Clock,
        ids: IdGenerator,
        exchange: str,
        environment: Environment,
        actor: str = AGENT_ACTOR,
        send_deadline_s: float = DEFAULT_SEND_DEADLINE_S,
        reconnaissance_delay_s: float = DEFAULT_RECONNAISSANCE_DELAY_S,
        reconnaissance_deadline_s: float = DEFAULT_RECONNAISSANCE_DEADLINE_S,
        fingerprint_window: timedelta = FINGERPRINT_WINDOW,
        fill_read_deadline_s: float = DEFAULT_FILL_READ_DEADLINE_S,
    ) -> None:
        super().__init__(
            uow,
            credentials,
            clock=clock,
            ids=ids,
            exchange=exchange,
            environment=environment,
            actor=actor,
            send_deadline_s=send_deadline_s,
            reconnaissance_delay_s=reconnaissance_delay_s,
            reconnaissance_deadline_s=reconnaissance_deadline_s,
            fingerprint_window=fingerprint_window,
            fill_read_deadline_s=fill_read_deadline_s,
        )
        self._rules = rules
        self._market = market
        self._trading = trading

    async def __call__(
        self, user_id: str | None, request: PlaceOrderInput, client: ClientDescription | None = None
    ) -> OrderResult:
        action = parse_place_order(
            symbol=request.symbol,
            category=request.category,
            side=request.side,
            order_type=request.order_type,
            qty=request.qty,
            price=request.price,
            reduce_only=request.reduce_only,
        )
        return await self._submit(user_id, action, request.idempotency_key, client)

    # --- hooks ---------------------------------------------------------------------------------

    def _payload(self, action: PlaceOrder) -> dict[str, JsonValue]:
        return _action_payload(action)

    def _deduplicated_without_key(self, action: PlaceOrder) -> bool:
        return fingerprint_applies(classify(action))

    def _risk_before_reads(self, action: PlaceOrder) -> RiskDirection:
        return classify(action)

    def _risk(self, action: PlaceOrder, market: _Market) -> RiskDirection:
        return classify(action)

    def _client_order_id(self, action_id: str) -> str:
        return self._trading.client_order_id(action_id)

    def _store(self, answer: OrderResult | DomainError) -> dict[str, JsonValue]:
        return answer_payload(answer)

    def _restore(self, payload: Mapping[str, Any]) -> OrderResult | DomainError:
        return restore_answer(payload)

    def _attempt_details(self, plan: OrderPlan) -> dict[str, JsonValue]:
        return {"order": _plan_payload(plan)}

    def _in_progress(self, held: IdempotencyRecord) -> OutcomeUnknown:
        client_order_id = self._trading.client_order_id(held.action_id)
        if is_stale(held, self._clock.now()):  # sent long ago and never answered: its call is gone
            return OutcomeUnknown(
                "an earlier identical call sent this order but did not complete; its outcome was never "
                "recorded, and this call sent nothing",
                next_step="Do not place it again blindly: check get_open_orders and get_order_history "
                f"for client_order_id {client_order_id}, then tell the user. A new attempt needs a new "
                "idempotency_key.",
                details={
                    "reason": "incomplete",
                    "client_order_id": client_order_id,
                    "replayed": "true",
                },
            )
        how = (
            "an earlier call with the same idempotency_key"
            if held.kind is KeyKind.EXPLICIT
            else "an identical call moments ago"
        )
        return OutcomeUnknown(
            f"{how} is still being processed; this call sent nothing",
            next_step="Do not place the order again. Repeat exactly this call in a few seconds to get "
            f"the first call's answer, or check get_open_orders for client_order_id {client_order_id}.",
            details={
                "reason": "in_progress",
                "client_order_id": client_order_id,
                "replayed": "true",
            },
        )

    def _superseded(self, attempt: Attempt, holder: IdempotencyRecord | None) -> OutcomeUnknown:
        client_order_id = attempt.client_order_id or ""
        details = {"reason": "superseded", "client_order_id": client_order_id}
        other = ""
        if holder is not None and not holder.held_by(attempt.action_id):
            details["holder_client_order_id"] = self._trading.client_order_id(holder.action_id)
            other = f"; the call now handling it uses client_order_id {details['holder_client_order_id']}"
        return OutcomeUnknown(
            "this call took too long before sending: its order (client_order_id "
            f"{client_order_id}) was never sent{other}",
            next_step="Do not place the order again yet. Check get_open_orders for the client_order_id "
            "named here, then tell the user.",
            details=details,
        )

    async def _read(self, action: PlaceOrder, keys: ApiKeyPair, policy: EffectivePolicy) -> _Market:
        market = await self._read_market(action, keys)
        # Early plan: rule violations (tick, minimums, status) are input errors, answered before
        # anything is decided. The decision plans again with the fresh policy.
        self._plan(action, market, policy)
        return market

    def _check(self, action: PlaceOrder, market: _Market, policy: EffectivePolicy) -> OrderPlan:
        plan = self._plan(action, market, policy)
        leverage = market.symbol_position.leverage if market.symbol_position is not None else None
        check_limits(plan, policy, leverage=leverage)
        if requires_confirmation(policy, action.kind, plan.notional):
            raise no_confirmation_channel(action.kind)  # FR-25 until tickets 016/020
        return plan

    async def _execute(self, attempt: Attempt, plan: OrderPlan, keys: ApiKeyPair) -> OrderResult:
        return await self._send(attempt, plan, keys)

    # --- before the decision ------------------------------------------------------------------

    async def _read_market(self, action: PlaceOrder, keys: ApiKeyPair) -> _Market:
        risk = classify(action)
        # A risk-reducing order must still find an instrument being delisted (FR-15, FR-43).
        rules = await self._rules.get(
            action.symbol, action.category, any_status=risk is RiskDirection.REDUCES
        )
        require_supported_quote(rules.instrument)
        band = await self._trading.get_price_band(action.symbol, action.category)
        best = None
        if action.order_type is OrderKind.MARKET:
            ticker = await self._market.get_ticker(action.symbol, action.category)
            best = BestPrices(bid=ticker.bid_price, ask=ticker.ask_price)
        symbol_position = None
        if action.category is Category.LINEAR:
            symbol_position = await self._trading.get_symbol_position(keys, action.symbol)
        return _Market(rules, band, best, symbol_position)

    @staticmethod
    def _plan(action: PlaceOrder, market: _Market, policy: EffectivePolicy) -> OrderPlan:
        return plan_order(
            action,
            market.rules,
            band=market.band,
            slippage_pct=policy.slippage_pct,
            best=market.best,
            position=market.symbol_position.position if market.symbol_position is not None else None,
        )

    # --- sending: outside any transaction ------------------------------------------------------

    async def _send(self, attempt: Attempt, plan: OrderPlan, keys: ApiKeyPair) -> OrderResult:
        client_order_id = _order_id_of(attempt)
        request = OrderRequest.from_plan(plan, client_order_id)
        # Shielded: once the request may be on its way, a cancelled call (the client went away)
        # must not skip the outcome record. Every await here is bounded: the send by its overall
        # deadline, the reconnaissance by its own.
        with anyio.CancelScope(shield=True):
            try:
                placed = await self._place_within_deadline(keys, request)
            except OutcomeUnknown as exc:
                return await self._reconnoitre(attempt, plan, keys, exc)
            except ExchangeUnavailable as exc:  # the adapter raises it only before anything was sent
                await self._try_record_outcome(attempt, failure("not_sent", exc), Outcome.TRANSIENT, exc)
                raise
            except DomainError as exc:
                await self._try_record_outcome(attempt, failure("rejected", exc), refusal_outcome(exc), exc)
                raise
            except Exception as exc:  # a bug: the order may have been sent
                log.exception("order_send_failed", action_id=attempt.action_id)
                cause = OutcomeUnknown("internal error while sending", details={"reason": "internal_error"})
                unknown = self._unknown(attempt, cause, "skipped")
                outcome: dict[str, JsonValue] = {"status": "unknown", "error_type": type(exc).__name__}
                await self._try_record_outcome(attempt, outcome, Outcome.UNKNOWN, unknown)
                raise unknown from exc
            except BaseException as exc:  # not an error (e.g. the interpreter exits): record, re-raise
                unknown = self._unknown(attempt, OutcomeUnknown("send interrupted"), "skipped")
                stopped: dict[str, JsonValue] = {"status": "unknown", "error_type": type(exc).__name__}
                await self._try_record_outcome(attempt, stopped, Outcome.UNKNOWN, unknown)
                raise
            # The exchange accepted the order: from here on nothing may turn into an error the agent
            # could read as "failed" and retry (a second order). Worst case: accepted, fill unknown.
            fill = await self._read_fill(keys, plan, client_order_id)
            result = OrderResult(attempt.action_id, client_order_id, placed.order_id, plan, fill)
            await self._try_record_outcome(
                attempt, _accepted(placed.order_id, fill), Outcome.ACCEPTED, result, order_id=placed.order_id
            )
        return result

    async def _place_within_deadline(self, keys: ApiKeyPair, request: OrderRequest) -> PlacedOrder:
        return await self._send_within_deadline(lambda: self._trading.place_order(keys, request))

    async def _reconnoitre(
        self, attempt: Attempt, plan: OrderPlan, keys: ApiKeyPair, cause: OutcomeUnknown
    ) -> OrderResult:
        """FR-27: find out instead of resending. Found -> the order as the exchange shows it; not found
        anywhere -> still unknown (the exchange's lists lag behind), never "failed"."""
        client_order_id = _order_id_of(attempt)
        found: OrderFill | None = None
        looked = "not_found"
        with anyio.move_on_after(self._reconnaissance_deadline_s) as bound:
            try:
                await anyio.sleep(self._reconnaissance_delay_s)
                found = await self._trading.find_order(
                    keys, plan.action.category, plan.action.symbol, client_order_id
                )
            except Exception:  # whatever broke, the answer stays "unknown", never an internal error
                looked = "failed"
                log.exception("order_reconnaissance_failed", action_id=attempt.action_id)
        if bound.cancelled_caught:
            looked = "failed"
        sent_error: dict[str, JsonValue] = {"code": cause.code, "details": dict(cause.details)}
        if found is None:
            unknown = self._unknown(attempt, cause, looked)
            outcome = {**failure("unknown", cause), "reconnaissance": looked}
            await self._try_record_outcome(attempt, outcome, Outcome.UNKNOWN, unknown)
            raise unknown from cause
        result = OrderResult(
            attempt.action_id,
            client_order_id,
            found.order_id,
            plan,
            found,
            source=ResultSource.RECONNAISSANCE,
        )
        accepted = {**_accepted(found.order_id, found), "reconnaissance": "found", "send_error": sent_error}
        await self._try_record_outcome(attempt, accepted, Outcome.ACCEPTED, result, order_id=found.order_id)
        return result

    async def _read_fill(self, keys: ApiKeyPair, plan: OrderPlan, client_order_id: str) -> OrderFill | None:
        """The order right after placement (D-15). A failed read is "not visible yet", not an error:
        the order was accepted, and the agent is told how to check."""
        try:
            with anyio.move_on_after(self._fill_read_deadline_s):
                return await self._trading.get_order(
                    keys, plan.action.category, plan.action.symbol, client_order_id
                )
            log.warning("order_fill_read_timed_out", client_order_id=client_order_id)
        except Exception:
            log.exception("order_fill_not_read", client_order_id=client_order_id)
        return None

    def _unknown(self, attempt: Attempt, cause: OutcomeUnknown, looked: str) -> OutcomeUnknown:
        client_order_id = _order_id_of(attempt)
        check = (
            "Do not place it again. The exchange may still list it with a delay: check get_open_orders "
            f"(and get_order_history) for client_order_id {client_order_id} in a minute, then "
            "tell the user what you found. "
        )
        primary = attempt.claims.primary
        keyed = primary is not None and primary.kind is KeyKind.EXPLICIT
        # Without a key: only a fingerprint that expires (or none for a reduce-only order).
        repeat = _KEYED_REPEAT if keyed else _KEYLESS_REPEAT
        return OutcomeUnknown(
            f"the order may or may not have reached the exchange ({cause.message})"
            + _LOOKED_UP.get(looked, ""),
            next_step=check + repeat,
            details={
                **cause.details,
                "client_order_id": client_order_id,
                "reconnaissance": looked,
            },
            untrusted_text=cause.untrusted_text,
        )


def _order_id_of(attempt: Attempt) -> str:
    if attempt.client_order_id is None:
        raise ValueError("an order attempt without a client order id")
    return attempt.client_order_id


def refusal_outcome(error: DomainError) -> Outcome:
    return Outcome.TRANSIENT if isinstance(error, TRANSIENT_ERRORS) else Outcome.REFUSED


def _claim_payload(claim: IdempotencyRecord | None) -> dict[str, JsonValue] | None:
    return None if claim is None else {"kind": claim.kind.value, "key": claim.key}


def _action_payload(action: PlaceOrder) -> dict[str, JsonValue]:
    """Every argument, defaults included: also the canonical call of the idempotency hash (D-4)."""
    return {
        "symbol": action.symbol,
        "category": action.category.value,
        "side": action.side.value,
        "order_type": action.order_type.value,
        "qty": action.qty,
        "price": action.price,
        "reduce_only": action.reduce_only,
    }


def _plan_payload(plan: OrderPlan) -> dict[str, JsonValue]:
    return {
        "risk": plan.risk.value,
        "qty": plan.qty,
        "qty_adjustment": None if plan.qty_adjustment is None else plan.qty_adjustment.value,
        "price": plan.price,
        "time_in_force": plan.time_in_force.value,
        "notional_usdt": plan.notional,
        "notional_price": plan.notional_price,
        "order_value_usdt": plan.order_value,
        "best_price": plan.best_price,
        "worst_price": plan.worst_price,
        "slippage_pct": plan.slippage_pct,
        "position_size": plan.position_size,
    }


def failure(status: str, error: DomainError) -> dict[str, JsonValue]:
    """No exchange text here: ``untrusted_text`` stays out of the journal's structured fields."""
    return {"status": status, "code": error.code, "details": dict(error.details)}


def _accepted(order_id: str, fill: OrderFill | None) -> dict[str, JsonValue]:
    if fill is None:
        return {"status": "accepted", "order_id": order_id, "fill": "unknown"}
    return {
        "status": "accepted",
        "order_id": order_id,
        "fill": {
            "status": fill.status.value,
            "price": fill.price,
            "qty": fill.qty,
            "filled_qty": fill.filled_qty,
            "avg_fill_price": fill.avg_fill_price,
        },
    }
