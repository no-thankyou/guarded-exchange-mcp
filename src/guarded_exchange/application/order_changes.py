"""Cancel an order, cancel all plain orders on an instrument, amend an order (SPEC FR-5, FR-16, FR-19,
FR-21, FR-25 - FR-27, FR-36, FR-43; PLAN D-10, D-20, D-21; ticket 013).

Each is an ``ActionPath``: the same auth, idempotency, decision under ``lock_user``, journal and
"never report a possibly-applied change as failed" as ``place_order``. What is their own:

- **Which orders.** Only orders this call has just read and labelled: one order by its exchange id,
  or a snapshot of the plain open orders on the instrument (``orderFilter=Order``), cancelled id by
  id. Never "whatever happens to be open when the request lands": no order the call did not see,
  and never a conditional order or a position's TP/SL. External orders are allowed (FR-5) and are
  named as such in the answer and the journal (FR-21).
- **Risk (FR-43).** A cancel reduces risk. An amend increases it with a larger quantity or a worse
  price, and then faces the kill switch, the allowlist, the order value and the leverage limits.
- **Asynchronous at the exchange (D-21).** "Accepted" is not "applied": after an accepted request
  the order is read back; the answer says ``cancelled``/``amended`` only when the order shows it,
  otherwise ``cancel_requested``/``amend_requested``. After an unknown outcome the order is read
  by its id (never resent); if it does not show the change, the answer stays ``outcome_unknown``.
- **No fingerprint window (D-20 (3)).** Repeating a cancel cannot add risk, and an amend sets
  absolute values, so a keyless repeat is a new call; an idempotency key works as for orders.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import anyio
import structlog

from guarded_exchange.application.confirmation import ConfirmationSupport
from guarded_exchange.application.credentials import RequireTradingCredential
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.order_change_results import (
    AmendResult,
    CancelAllItem,
    CancelAllResult,
    CancelResult,
    ChangeResult,
    ChangeState,
    ItemState,
    change_answer_payload,
    restore_change_answer,
)
from guarded_exchange.application.order_results import ResultSource
from guarded_exchange.application.ports import (
    Clock,
    IdGenerator,
    OrderChanges,
    TradingExchange,
    UnitOfWork,
    UnitOfWorkFactory,
)
from guarded_exchange.application.submit_action import (
    AGENT_ACTOR,
    DEFAULT_FILL_READ_DEADLINE_S,
    DEFAULT_RECONNAISSANCE_DEADLINE_S,
    DEFAULT_RECONNAISSANCE_DELAY_S,
    DEFAULT_SEND_DEADLINE_S,
    TRANSIENT_ERRORS,
    ActionPath,
    Attempt,
    TradingAction,
    failure,
    refusal_outcome,
)
from guarded_exchange.domain.account import Order, OrderStatus
from guarded_exchange.domain.actions import (
    AmendOrder,
    CancelAllOrders,
    CancelOrder,
    parse_amend_order,
    parse_cancel_all_orders,
    parse_cancel_order,
)
from guarded_exchange.domain.audit import AuditRecord, ClientDescription, JsonValue
from guarded_exchange.domain.credentials import ApiKeyPair
from guarded_exchange.domain.errors import (
    DomainError,
    ExchangeUnavailable,
    NotFound,
    OutcomeUnknown,
    RateLimited,
)
from guarded_exchange.domain.idempotency import IdempotencyRecord, Outcome, is_stale
from guarded_exchange.domain.instruments import Category, require_supported_quote
from guarded_exchange.domain.limits import check_amend_limits
from guarded_exchange.domain.order_changes import (
    CANCEL_ALL_MAX,
    OPEN_STATUSES,
    AmendPlan,
    CancelAllPlan,
    CancelPlan,
    OrderAmendment,
    TouchedOrder,
    amend_applied,
    plan_amend,
    plan_cancel,
    plan_cancel_all,
    require_changeable,
)
from guarded_exchange.domain.policy import EffectivePolicy
from guarded_exchange.domain.risk import RiskDirection
from guarded_exchange.domain.users import Environment

__all__ = [
    "AmendOrderInput",
    "CancelAllOrdersInput",
    "CancelOrderInput",
    "SubmitAmendOrder",
    "SubmitCancelAllOrders",
    "SubmitCancelOrder",
]

log = structlog.get_logger(__name__)

_CANCELLED: Final = frozenset({OrderStatus.CANCELLED, OrderStatus.PARTIALLY_FILLED_CANCELLED})
_NO_THRESHOLD: Final = Decimal(0)
"""A cancel is confirmed by its kind only (FR-19: the value threshold is about orders being placed)."""


@dataclass(frozen=True, slots=True)
class CancelOrderInput:
    symbol: str
    category: str
    order_id: str
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class AmendOrderInput:
    symbol: str
    category: str
    order_id: str
    qty: str | None = None
    price: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class CancelAllOrdersInput:
    symbol: str
    category: str
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class _AmendRead:
    plan: AmendPlan
    leverage: Decimal | None
    read_at: datetime
    """When this call started reading the order: changes recorded after it are not in the plan."""


CHANGE_SETTLE_WINDOW: Final = timedelta(seconds=60)
"""How far back an amend looks for other changes of the same order that may not be visible in its own
read yet (D-8, review 1 of ticket 013). Longer than the exchange takes to show an accepted change
(spike 004: at once) and than one call's send deadline."""
CHANGE_IN_PROGRESS_RETRY_S: Final = 5.0
_CHANGE_TOOLS: Final = frozenset({"amend_order", "cancel_order", "cancel_all_orders"})
_JOURNAL_TYPES: Final = frozenset({"order_attempt", "order_outcome"})
_UNSETTLED_STATES: Final = frozenset({"amend_requested", "cancel_requested", "unknown"})
_NOTHING_CHANGED: Final = frozenset({"rejected", "not_sent"})


class _OrderChangePath[A: TradingAction, M, P, R: ChangeResult](ActionPath[A, M, P, R]):
    """What the three order changes share: storage of answers, repeats, reading an order back."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        credentials: RequireTradingCredential,
        *,
        changes: OrderChanges,
        clock: Clock,
        ids: IdGenerator,
        exchange: str,
        environment: Environment,
        actor: str = AGENT_ACTOR,
        send_deadline_s: float = DEFAULT_SEND_DEADLINE_S,
        reconnaissance_delay_s: float = DEFAULT_RECONNAISSANCE_DELAY_S,
        reconnaissance_deadline_s: float = DEFAULT_RECONNAISSANCE_DEADLINE_S,
        read_back_deadline_s: float = DEFAULT_FILL_READ_DEADLINE_S,
        confirmations: ConfirmationSupport | None = None,
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
            fill_read_deadline_s=read_back_deadline_s,
            confirmations=confirmations,
        )
        self._changes = changes

    def _deduplicated_without_key(self, action: A) -> bool:
        return False  # D-20 (3); an amend sets absolute values: a repeat cannot add risk either

    def _store(self, answer: R | DomainError) -> dict[str, JsonValue]:
        return change_answer_payload(answer)

    def _in_progress(self, held: IdempotencyRecord) -> OutcomeUnknown:
        if is_stale(held, self._clock.now()):
            return OutcomeUnknown(
                "an earlier call with the same idempotency_key sent this change but did not complete; this "
                "call sent nothing",
                next_step="Check get_open_orders for the order(s), then tell the user. A new attempt needs "
                "a new idempotency_key.",
                details={"reason": "incomplete", "replayed": "true"},
            )
        return OutcomeUnknown(
            "an earlier call with the same idempotency_key is still being processed; this call sent nothing",
            next_step="Repeat exactly this call in a few seconds to get the first call's answer, or check "
            "get_open_orders.",
            details={"reason": "in_progress", "replayed": "true"},
        )

    async def _read_back(self, keys: ApiKeyPair, order: TouchedOrder) -> Order | None:
        """The order right after an accepted change; a failed read is "not visible yet", not an error."""
        try:
            with anyio.move_on_after(self._fill_read_deadline_s):
                return await self._changes.get_order_by_id(keys, order.category, order.symbol, order.order_id)
            log.warning("order_read_back_timed_out", order_id=order.order_id)
        except Exception:
            log.exception("order_read_back_failed", order_id=order.order_id)
        return None

    async def _reconnoitre(self, keys: ApiKeyPair, order: TouchedOrder) -> tuple[Order | None, str]:
        """FR-27/D-21: the order's status after an unknown outcome, by its id; never a resend."""
        found: Order | None = None
        looked = "not_found"
        with anyio.move_on_after(self._reconnaissance_deadline_s) as bound:
            try:
                await anyio.sleep(self._reconnaissance_delay_s)
                found = await self._changes.get_order_by_id(
                    keys, order.category, order.symbol, order.order_id
                )
            except Exception:
                looked = "failed"
                log.exception("order_change_reconnaissance_failed", order_id=order.order_id)
        if bound.cancelled_caught:
            looked = "failed"
        if found is not None:
            looked = "found"
        return found, looked

    async def _change_one(
        self,
        attempt: Attempt,
        keys: ApiKeyPair,
        order: TouchedOrder,
        send: Callable[[], Awaitable[None]],
        *,
        known: Callable[[Order | None, ResultSource], R | None],
        requested: Callable[[Order | None], R],
        what: str,
    ) -> R:
        """Send one cancel or amend and find out what is known about it. ``known`` turns the order read
        back into a result when it shows a final answer; ``requested`` is the accepted, not yet visible
        one. Shielded: once the request may be on its way, the outcome is always recorded."""
        with anyio.CancelScope(shield=True):
            try:
                await self._send_within_deadline(send)
            except OutcomeUnknown as exc:
                found, looked = await self._reconnoitre(keys, order)
                learned = known(found, ResultSource.RECONNAISSANCE)
                if learned is None:
                    unknown = _unknown(what, order, exc, found, looked)
                    outcome = {**failure("unknown", exc), "reconnaissance": looked, **_seen(found)}
                    await self._try_record_outcome(attempt, outcome, Outcome.UNKNOWN, unknown)
                    raise unknown from exc
                sent_error: dict[str, JsonValue] = {"code": exc.code, "details": dict(exc.details)}
                accepted = {**_state(learned), "reconnaissance": "found", "send_error": sent_error}
                await self._try_record_outcome(attempt, accepted, Outcome.ACCEPTED, learned)
                return learned
            except ExchangeUnavailable as exc:  # raised only before anything was sent
                await self._try_record_outcome(attempt, failure("not_sent", exc), Outcome.TRANSIENT, exc)
                raise
            except DomainError as exc:
                await self._try_record_outcome(attempt, failure("rejected", exc), refusal_outcome(exc), exc)
                raise
            except Exception as exc:  # a bug: the request may have been sent
                log.exception("order_change_send_failed", action_id=attempt.action_id)
                cause = OutcomeUnknown("internal error while sending", details={"reason": "internal_error"})
                unknown = _unknown(what, order, cause, None, "skipped")
                await self._try_record_outcome(
                    attempt, {"status": "unknown", "error_type": type(exc).__name__}, Outcome.UNKNOWN, unknown
                )
                raise unknown from exc
            except BaseException as exc:
                unknown = _unknown(what, order, OutcomeUnknown("send interrupted"), None, "skipped")
                await self._try_record_outcome(
                    attempt, {"status": "unknown", "error_type": type(exc).__name__}, Outcome.UNKNOWN, unknown
                )
                raise
            # Accepted: from here on nothing may turn into an error. Worst case: requested, not seen.
            after = await self._read_back(keys, order)
            confirmed = known(after, ResultSource.PLACED)
            result = requested(after) if confirmed is None else confirmed
            await self._try_record_outcome(attempt, _state(result), Outcome.ACCEPTED, result)
        return result


# --- cancel_order ---------------------------------------------------------------------------------


class SubmitCancelOrder(_OrderChangePath[CancelOrder, CancelPlan, CancelPlan, CancelResult]):
    """Cancel one open plain order, ours or external."""

    async def __call__(
        self, user_id: str | None, request: CancelOrderInput, client: ClientDescription | None = None
    ) -> CancelResult:
        action = parse_cancel_order(
            symbol=request.symbol, category=request.category, order_id=request.order_id
        )
        return await self._submit(user_id, action, request.idempotency_key, client)

    def _payload(self, action: CancelOrder) -> dict[str, JsonValue]:
        return {"symbol": action.symbol, "category": action.category.value, "order_id": action.order_id}

    def _restore(self, payload: Mapping[str, Any]) -> CancelResult | DomainError:
        answer = restore_change_answer(payload)
        if not isinstance(answer, CancelResult | DomainError):
            raise ValueError("stored answer of another action")
        return answer

    def _risk_before_reads(self, action: CancelOrder) -> RiskDirection:
        return RiskDirection.REDUCES

    def _risk(self, action: CancelOrder, market: CancelPlan) -> RiskDirection:
        return RiskDirection.REDUCES

    async def _read(self, action: CancelOrder, keys: ApiKeyPair, policy: EffectivePolicy) -> CancelPlan:
        order = await self._changes.get_order_by_id(keys, action.category, action.symbol, action.order_id)
        return plan_cancel(action, order)

    def _check(self, action: CancelOrder, market: CancelPlan, policy: EffectivePolicy) -> CancelPlan:
        return market

    def _confirm_notional(self, action: CancelOrder, plan: CancelPlan) -> Decimal:
        return _NO_THRESHOLD

    def _parse_payload(self, payload: Mapping[str, Any]) -> CancelOrder:
        return parse_cancel_order(
            symbol=payload["symbol"], category=payload["category"], order_id=payload["order_id"]
        )

    def _attempt_details(self, plan: CancelPlan) -> dict[str, JsonValue]:
        return {"risk": plan.risk.value, "order": _touched(plan.order)}

    async def _execute(self, attempt: Attempt, plan: CancelPlan, keys: ApiKeyPair) -> CancelResult:
        order = plan.order

        def known(found: Order | None, source: ResultSource) -> CancelResult | None:
            if found is None or found.status not in _CANCELLED | {OrderStatus.FILLED}:
                return None
            state = ChangeState.FILLED if found.status is OrderStatus.FILLED else ChangeState.CANCELLED
            return CancelResult(attempt.action_id, plan, state, TouchedOrder.of(found), source)

        def requested(found: Order | None) -> CancelResult:
            after = None if found is None else TouchedOrder.of(found)
            return CancelResult(attempt.action_id, plan, ChangeState.CANCEL_REQUESTED, after)

        return await self._change_one(
            attempt,
            keys,
            order,
            lambda: self._changes.cancel_order(keys, order.category, order.symbol, order.order_id),
            known=known,
            requested=requested,
            what="cancel",
        )


# --- amend_order ----------------------------------------------------------------------------------


class SubmitAmendOrder(_OrderChangePath[AmendOrder, _AmendRead, AmendPlan, AmendResult]):
    """Change the quantity and/or limit price of one open plain limit order, ours or external."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        credentials: RequireTradingCredential,
        rules: InstrumentRulesCache,
        *,
        trading: TradingExchange,
        changes: OrderChanges,
        clock: Clock,
        ids: IdGenerator,
        exchange: str,
        environment: Environment,
        send_deadline_s: float = DEFAULT_SEND_DEADLINE_S,
        reconnaissance_delay_s: float = DEFAULT_RECONNAISSANCE_DELAY_S,
        reconnaissance_deadline_s: float = DEFAULT_RECONNAISSANCE_DEADLINE_S,
        read_back_deadline_s: float = DEFAULT_FILL_READ_DEADLINE_S,
        confirmations: ConfirmationSupport | None = None,
    ) -> None:
        super().__init__(
            uow,
            credentials,
            changes=changes,
            clock=clock,
            ids=ids,
            exchange=exchange,
            environment=environment,
            send_deadline_s=send_deadline_s,
            reconnaissance_delay_s=reconnaissance_delay_s,
            reconnaissance_deadline_s=reconnaissance_deadline_s,
            read_back_deadline_s=read_back_deadline_s,
            confirmations=confirmations,
        )
        self._rules = rules
        self._trading = trading

    async def __call__(
        self, user_id: str | None, request: AmendOrderInput, client: ClientDescription | None = None
    ) -> AmendResult:
        action = parse_amend_order(
            symbol=request.symbol,
            category=request.category,
            order_id=request.order_id,
            qty=request.qty,
            price=request.price,
        )
        return await self._submit(user_id, action, request.idempotency_key, client)

    def _payload(self, action: AmendOrder) -> dict[str, JsonValue]:
        return {
            "symbol": action.symbol,
            "category": action.category.value,
            "order_id": action.order_id,
            "qty": action.qty,
            "price": action.price,
        }

    def _restore(self, payload: Mapping[str, Any]) -> AmendResult | DomainError:
        answer = restore_change_answer(payload)
        if not isinstance(answer, AmendResult | DomainError):
            raise ValueError("stored answer of another action")
        return answer

    def _risk_before_reads(self, action: AmendOrder) -> None:
        return None  # it depends on the order as it is now (FR-43)

    def _risk(self, action: AmendOrder, market: _AmendRead) -> RiskDirection:
        return market.plan.risk

    async def _read(self, action: AmendOrder, keys: ApiKeyPair, policy: EffectivePolicy) -> _AmendRead:
        read_at = self._clock.now()
        order = await self._changes.get_order_by_id(keys, action.category, action.symbol, action.order_id)
        require_changeable(order, symbol=action.symbol, category=action.category, order_id=action.order_id)
        # Any status: a reducing amend must still work on an instrument being delisted (FR-15).
        rules = await self._rules.get(action.symbol, action.category, any_status=True)
        require_supported_quote(rules.instrument)
        band = None
        if action.price is not None:
            band = await self._trading.get_price_band(action.symbol, action.category)
        position, leverage = None, None
        if action.category is Category.LINEAR:
            state = await self._trading.get_symbol_position(keys, action.symbol)
            position, leverage = state.position, state.leverage
        plan = plan_amend(action, order, rules, band=band, position=position)
        return _AmendRead(plan, leverage, read_at)

    async def _guard_locked(
        self, uow: UnitOfWork, attempt: Attempt, action: AmendOrder, market: _AmendRead
    ) -> None:
        """D-8 for amends: the plan (risk direction, order value) was computed from the order as read.
        Another change of the same order that is still in flight, unconfirmed, or confirmed only after
        that read makes the read stale: an earlier amend accepted but not yet visible would let this one
        pass the order value limit or the kill switch against values that are no longer true."""
        since = self._clock.now() - CHANGE_SETTLE_WINDOW
        records = await uow.audit.since(attempt.user_id, since, _JOURNAL_TYPES)
        other = unsettled_change(records, action.order_id, market.read_at, own_action_id=attempt.action_id)
        if other is not None:
            raise RateLimited(
                "another change of this order is not settled yet, so the order as read may be out of date; "
                "nothing was sent",
                next_step="Read the order again with get_open_orders in a few seconds and call again with "
                "values based on what it shows now.",
                retry_after_s=CHANGE_IN_PROGRESS_RETRY_S,
                details={"reason": "order_change_in_progress", "order_id": action.order_id},
            )

    def _check(self, action: AmendOrder, market: _AmendRead, policy: EffectivePolicy) -> AmendPlan:
        plan = market.plan
        check_amend_limits(plan, policy, leverage=market.leverage)
        return plan

    def _confirm_notional(self, action: AmendOrder, plan: AmendPlan) -> Decimal:
        return plan.notional

    def _parse_payload(self, payload: Mapping[str, Any]) -> AmendOrder:
        return parse_amend_order(
            symbol=payload["symbol"],
            category=payload["category"],
            order_id=payload["order_id"],
            qty=payload["qty"],
            price=payload["price"],
        )

    def _attempt_details(self, plan: AmendPlan) -> dict[str, JsonValue]:
        return {
            "risk": plan.risk.value,
            "order": _touched(plan.order),
            "change": {
                "qty": plan.qty,
                "qty_adjustment": None if plan.qty_adjustment is None else plan.qty_adjustment.value,
                "price": plan.price,
                "notional_usdt": plan.notional,
                "position_size": plan.position_size,
            },
        }

    async def _execute(self, attempt: Attempt, plan: AmendPlan, keys: ApiKeyPair) -> AmendResult:
        amendment = OrderAmendment.from_plan(plan)

        def known(found: Order | None, source: ResultSource) -> AmendResult | None:
            if found is None or not amend_applied(plan, found):
                return None
            return AmendResult(attempt.action_id, plan, ChangeState.AMENDED, TouchedOrder.of(found), source)

        def requested(found: Order | None) -> AmendResult:
            if found is None:
                return AmendResult(attempt.action_id, plan, ChangeState.AMEND_REQUESTED, None)
            # Closed without the new values: it filled or was cancelled first; nothing was changed.
            state = ChangeState.AMEND_REQUESTED if found.status in OPEN_STATUSES else ChangeState.ORDER_CLOSED
            return AmendResult(attempt.action_id, plan, state, TouchedOrder.of(found))

        return await self._change_one(
            attempt,
            keys,
            plan.order,
            lambda: self._changes.amend_order(keys, amendment),
            known=known,
            requested=requested,
            what="amend",
        )


# --- cancel_all_orders ----------------------------------------------------------------------------


class SubmitCancelAllOrders(_OrderChangePath[CancelAllOrders, CancelAllPlan, CancelAllPlan, CancelAllResult]):
    """Cancel the plain open orders on one instrument (at most ``CANCEL_ALL_MAX`` per call), ours and
    external, id by id; conditional orders and a position's TP/SL are never touched."""

    async def __call__(
        self, user_id: str | None, request: CancelAllOrdersInput, client: ClientDescription | None = None
    ) -> CancelAllResult:
        action = parse_cancel_all_orders(symbol=request.symbol, category=request.category)
        return await self._submit(user_id, action, request.idempotency_key, client)

    def _payload(self, action: CancelAllOrders) -> dict[str, JsonValue]:
        return {"symbol": action.symbol, "category": action.category.value}

    def _restore(self, payload: Mapping[str, Any]) -> CancelAllResult | DomainError:
        answer = restore_change_answer(payload)
        if not isinstance(answer, CancelAllResult | DomainError):
            raise ValueError("stored answer of another action")
        return answer

    def _risk_before_reads(self, action: CancelAllOrders) -> RiskDirection:
        return RiskDirection.REDUCES

    def _risk(self, action: CancelAllOrders, market: CancelAllPlan) -> RiskDirection:
        return RiskDirection.REDUCES

    async def _read(
        self, action: CancelAllOrders, keys: ApiKeyPair, policy: EffectivePolicy
    ) -> CancelAllPlan:
        page = await self._changes.list_cancellable_orders(
            keys, action.category, action.symbol, CANCEL_ALL_MAX
        )
        return plan_cancel_all(action, page)

    def _check(
        self, action: CancelAllOrders, market: CancelAllPlan, policy: EffectivePolicy
    ) -> CancelAllPlan:
        return market

    def _confirm_notional(self, action: CancelAllOrders, plan: CancelAllPlan) -> Decimal:
        return _NO_THRESHOLD

    def _parse_payload(self, payload: Mapping[str, Any]) -> CancelAllOrders:
        return parse_cancel_all_orders(symbol=payload["symbol"], category=payload["category"])

    def _confirmed_plan(
        self,
        action: CancelAllOrders,
        market: CancelAllPlan,
        policy: EffectivePolicy,
        shown: Mapping[str, Any],
    ) -> CancelAllPlan:
        """FR-22, FR-48: only the orders the human was shown. An order that appeared after the snapshot
        was never shown and stays; one that is gone is simply not cancelled again."""
        shown_ids = {str(order["order_id"]) for order in shown.get("orders") or ()}
        kept = tuple(order for order in market.orders if order.order_id in shown_ids)
        return replace(market, orders=kept)

    def _attempt_details(self, plan: CancelAllPlan) -> dict[str, JsonValue]:
        return {
            "risk": plan.risk.value,
            "orders": [_touched(order) for order in plan.orders],
            "external_count": plan.external_count,
            "has_more": plan.has_more,
        }

    async def _execute(self, attempt: Attempt, plan: CancelAllPlan, keys: ApiKeyPair) -> CancelAllResult:
        with anyio.CancelScope(shield=True):
            items: list[CancelAllItem] = []
            stop: DomainError | None = None
            try:
                deadline = anyio.current_time() + self._send_deadline_s
                for order in plan.orders:
                    if stop is not None:
                        items.append(CancelAllItem(order, ItemState.NOT_SENT))
                        continue
                    item, stop = await self._cancel_one(keys, order, deadline - anyio.current_time())
                    items.append(item)
                items = await self._verify(keys, plan, items)
            except Exception as exc:  # a bug: some cancels may have been sent; never "failed"
                log.exception("cancel_all_failed", action_id=attempt.action_id)
                broken = _cancel_all_unknown(plan, items)
                await self._try_record_outcome(
                    attempt, {"status": "unknown", "error_type": type(exc).__name__}, Outcome.UNKNOWN, broken
                )
                raise broken from exc
            except BaseException as exc:  # not an error (the interpreter exits): record, re-raise
                await self._try_record_outcome(
                    attempt,
                    {"status": "unknown", "error_type": type(exc).__name__},
                    Outcome.UNKNOWN,
                    _cancel_all_unknown(plan, items),
                )
                raise
            result = CancelAllResult(
                attempt.action_id, plan, tuple(items), stopped_by=None if stop is None else stop.code
            )
            if stop is not None and isinstance(stop, TRANSIENT_ERRORS) and _nothing_happened(result):
                # Nothing was cancelled or possibly cancelled: the transient failure is the answer.
                await self._try_record_outcome(attempt, failure("not_sent", stop), Outcome.TRANSIENT, stop)
                raise stop
            unknown = result.count(ItemState.UNKNOWN) > 0
            await self._try_record_outcome(
                attempt,
                _cancel_all_outcome(result),
                Outcome.UNKNOWN if unknown else Outcome.ACCEPTED,
                result,
            )
        return result

    async def _cancel_one(  # noqa: PLR0911 - one return per kind of answer
        self, keys: ApiKeyPair, order: TouchedOrder, remaining_s: float
    ) -> tuple[CancelAllItem, DomainError | None]:
        """One cancel; the second value stops the rest (an unknown outcome or a transient failure:
        sending on would only add unknowns or hit the same wall)."""
        if remaining_s <= 0:
            # Nothing of this one was sent: not "unknown", a transient stop (retrying may work).
            late = ExchangeUnavailable(
                "the overall send deadline of this call passed before this cancel was sent",
                next_step="Call cancel_all_orders again for the rest; check get_open_orders first.",
                details={"reason": "send_deadline"},
            )
            return CancelAllItem(order, ItemState.NOT_SENT, late.code), late
        try:
            await self._send_within_deadline(
                lambda: self._changes.cancel_order(keys, order.category, order.symbol, order.order_id),
                remaining_s,
            )
        except OutcomeUnknown as exc:
            return CancelAllItem(order, ItemState.UNKNOWN, exc.code), exc
        except NotFound as exc:
            return CancelAllItem(order, ItemState.NOT_OPEN, exc.code), None
        except TRANSIENT_ERRORS as exc:
            return CancelAllItem(order, ItemState.NOT_SENT, exc.code), exc
        except DomainError as exc:
            return CancelAllItem(order, ItemState.REJECTED, exc.code), None
        except Exception:  # a bug: the request may have been sent
            log.exception("order_cancel_failed", order_id=order.order_id)
            cause = OutcomeUnknown("internal error while sending", details={"reason": "internal_error"})
            return CancelAllItem(order, ItemState.UNKNOWN, cause.code), cause
        return CancelAllItem(order, ItemState.CANCEL_REQUESTED), None

    async def _verify(
        self, keys: ApiKeyPair, plan: CancelAllPlan, items: list[CancelAllItem]
    ) -> list[CancelAllItem]:
        """D-21: read the plain open orders once; an accepted or unknown cancel whose order is no longer
        listed is ``closed``. A failed read leaves every state as it was (never "failed")."""
        pending = {ItemState.CANCEL_REQUESTED, ItemState.UNKNOWN}
        if not any(item.state in pending for item in items):
            return items
        still_open: set[str] | None = None
        with anyio.move_on_after(self._reconnaissance_deadline_s):
            try:
                if any(item.state is ItemState.UNKNOWN for item in items):
                    await anyio.sleep(self._reconnaissance_delay_s)  # the lists lag behind
                action = plan.action
                page = await self._changes.list_cancellable_orders(
                    keys, action.category, action.symbol, CANCEL_ALL_MAX
                )
                still_open = {order.order_id for order in page.items}
                if page.has_more:  # an order beyond the page may be one of ours: cannot tell
                    still_open = None
            except Exception:
                log.exception("cancel_all_verification_failed")
        if still_open is None:
            return items
        return [
            CancelAllItem(item.order, ItemState.CLOSED)
            if item.state in pending and item.order.order_id not in still_open
            else item
            for item in items
        ]


# --- helpers --------------------------------------------------------------------------------------


def _touched(order: TouchedOrder) -> dict[str, JsonValue]:
    return {
        "order_id": order.order_id,
        "origin": order.origin.value,
        "side": order.side.value,
        "status": order.status.value,
        "qty": order.qty,
        "filled_qty": order.filled_qty,
        "price": order.price,
        "reduce_only": order.reduce_only,
    }


def _seen(found: Order | None) -> dict[str, JsonValue]:
    if found is None:
        return {}
    return {"order_status": found.status.value, "order_qty": found.qty, "order_price": found.price}


def _state(result: ChangeResult) -> dict[str, JsonValue]:
    if isinstance(result, CancelAllResult):
        raise TypeError("cancel_all_orders records its outcome per order")
    payload: dict[str, JsonValue] = {"status": "accepted", "state": result.state.value}
    if result.after is not None:
        payload.update(
            order_status=result.after.status.value, order_qty=result.after.qty, order_price=result.after.price
        )
    return payload


def _cancel_all_outcome(result: CancelAllResult) -> dict[str, JsonValue]:
    unknown = result.count(ItemState.UNKNOWN) > 0
    return {
        "status": "unknown" if unknown else "completed",
        "stopped_by": result.stopped_by,
        "items": [
            {"order_id": item.order.order_id, "state": item.state.value, "code": item.code}
            for item in result.items
        ],
    }


def unsettled_change(
    records: Sequence[AuditRecord], order_id: str, read_at: datetime, *, own_action_id: str
) -> str | None:
    """The action id of a journaled change of ``order_id`` that the order as read at ``read_at`` may not
    reflect: no outcome yet (in flight), an outcome that is unknown or only "requested", or any change
    recorded after the read. A change the exchange definitely refused (or that was never sent) changed
    nothing and does not count."""
    attempts = {
        str(r.payload["action_id"]): r
        for r in records
        if r.event_type == "order_attempt"
        and r.payload.get("tool") in _CHANGE_TOOLS
        and order_id in _touched_ids(r.payload)
    }
    outcomes = {str(r.payload["action_id"]): r for r in records if r.event_type == "order_outcome"}
    for action_id in attempts:
        if action_id == own_action_id:
            continue
        outcome = outcomes.get(action_id)
        if outcome is None:
            return action_id
        status = outcome.payload.get("status")
        if status in _NOTHING_CHANGED:
            continue
        if outcome.ts >= read_at or status == "unknown" or _unsettled_state(outcome.payload, order_id):
            return action_id
    return None


def _touched_ids(attempt: Mapping[str, Any]) -> set[str]:
    single = attempt.get("order")
    ids = {str(single["order_id"])} if isinstance(single, Mapping) else set()
    ids.update(str(o["order_id"]) for o in attempt.get("orders") or ())
    return ids


def _unsettled_state(outcome: Mapping[str, Any], order_id: str) -> bool:
    if outcome.get("state") in _UNSETTLED_STATES:
        return True
    return any(
        str(item.get("order_id")) == order_id and item.get("state") in _UNSETTLED_STATES
        for item in outcome.get("items") or ()
    )


def _cancel_all_unknown(plan: CancelAllPlan, items: list[CancelAllItem]) -> OutcomeUnknown:
    sent = [item.order.order_id for item in items if item.state is not ItemState.NOT_SENT]
    return OutcomeUnknown(
        f"cancel_all_orders on {plan.action.symbol} stopped on an internal error after {len(items)} of "
        f"{len(plan.orders)} orders; some cancels may have been applied",
        next_step=f"Check get_open_orders for {plan.action.symbol} ({plan.action.category.value}) in a few "
        "seconds and tell the user what is still open; calling cancel_all_orders again is safe.",
        details={"reason": "internal_error", "attempted": ",".join(sent)},
    )


def _nothing_happened(result: CancelAllResult) -> bool:
    return all(
        item.state in {ItemState.NOT_SENT, ItemState.NOT_OPEN, ItemState.REJECTED} for item in result.items
    )


def _unknown(
    what: str, order: TouchedOrder, cause: OutcomeUnknown, found: Order | None, looked: str
) -> OutcomeUnknown:
    """The change may or may not have been applied; repeating a cancel or the same amend is harmless."""
    seen = "" if found is None else f"; the order now shows status {found.status.value}"
    return OutcomeUnknown(
        f"the {what} of order {order.order_id} may or may not have been applied ({cause.message}){seen}",
        next_step=f"Check get_open_orders for order_id {order.order_id} in a few seconds and tell the user. "
        f"Repeating this {what}_order call is safe: it cannot add risk "
        f"({'a cancel' if what == 'cancel' else 'it sets the same values'}).",
        details={**cause.details, "order_id": order.order_id, "reconnaissance": looked},
        untrusted_text=cause.untrusted_text,
    )
