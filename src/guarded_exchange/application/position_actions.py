"""Close a position, set its stop-loss / take-profit, set the leverage (SPEC FR-6 - FR-8, FR-14, FR-27,
FR-43; PLAN D-21, ADR-014; ticket 014). Perpetuals only: spot is refused with a plain message.

Each is an ``ActionPath``: the same auth, idempotency, decision under ``lock_user``, kill switch,
journal and "never report a possibly-applied change as failed" as ``place_order``. What is their own:

- **Risk (FR-43).** Closing and setting SL/TP reduce risk: no order-size limit, no allowlist (a big
  position must stay stoppable), and the kill switch lets them through. Raising the leverage increases
  it: the kill switch blocks it and the allowlist and the maximum leverage apply.
- **Close is a reduce-only market order** (Limit IOC at the worst price within the policy's slippage):
  the side and a quantity cut to the position come from the position read in this call, and it is
  planned, sent and confirmed by the ``place_order`` machinery (client order id, reconnaissance).
- **No client id for SL/TP and leverage (D-21).** After an accepted request the position is read
  back: ``applied`` only when it shows the values, else ``requested``. After an unknown outcome the
  position is read (never a second POST): it shows the target -> result by reconnaissance, else
  ``outcome_unknown``. "Leverage already at this value" (110043) is success, decided by the adapter.
- **No fingerprint window (D-20 (3)).** These set absolute values or only shrink: repeating cannot add
  risk. An idempotency key works as for orders.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import anyio
import structlog

from guarded_exchange.application.confirmation import ConfirmationSupport
from guarded_exchange.application.credentials import RequireTradingCredential
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
from guarded_exchange.application.order_results import (
    OrderResult,
    ResultSource,
    answer_payload,
    restore_answer,
)
from guarded_exchange.application.ports import (
    Clock,
    ExchangeClient,
    IdGenerator,
    PositionActions,
    TradingExchange,
    UnitOfWorkFactory,
)
from guarded_exchange.application.position_results import (
    LeverageResult,
    PositionChangeResult,
    PositionChangeState,
    StopResult,
    position_answer_payload,
    restore_position_answer,
)
from guarded_exchange.application.submit_action import (
    AGENT_ACTOR,
    DEFAULT_FILL_READ_DEADLINE_S,
    DEFAULT_RECONNAISSANCE_DEADLINE_S,
    DEFAULT_RECONNAISSANCE_DELAY_S,
    DEFAULT_SEND_DEADLINE_S,
    ActionPath,
    Attempt,
    MarketView,
    SubmitPlaceOrder,
    TradingAction,
    failure,
    refusal_outcome,
)
from guarded_exchange.domain.account import OrderSide, PositionSide, SymbolPosition
from guarded_exchange.domain.actions import (
    ClosePosition,
    OrderKind,
    PlaceOrder,
    SetLeverage,
    SetTradingStop,
    parse_close_position,
    parse_set_leverage,
    parse_set_trading_stop,
)
from guarded_exchange.domain.audit import ClientDescription, JsonValue
from guarded_exchange.domain.confirmation import ConfirmationStatus, PendingConfirmation
from guarded_exchange.domain.credentials import ApiKeyPair
from guarded_exchange.domain.errors import (
    DomainError,
    ExchangeUnavailable,
    OutcomeUnknown,
    PositionNotFound,
)
from guarded_exchange.domain.idempotency import IdempotencyRecord, Outcome
from guarded_exchange.domain.instruments import Category, require_supported_quote
from guarded_exchange.domain.limits import (
    check_leverage_limits,
)
from guarded_exchange.domain.orders import OrderPlan
from guarded_exchange.domain.policy import EffectivePolicy
from guarded_exchange.domain.position_actions import (
    LeveragePlan,
    StopPlan,
    plan_leverage,
    plan_trading_stop,
    stop_applied,
)
from guarded_exchange.domain.risk import RiskDirection
from guarded_exchange.domain.users import Environment

__all__ = [
    "ClosePositionInput",
    "SetLeverageInput",
    "SetTradingStopInput",
    "SubmitClosePosition",
    "SubmitSetLeverage",
    "SubmitSetTradingStop",
]

log = structlog.get_logger(__name__)

_NO_THRESHOLD: Final = Decimal(0)
"""SL/TP and leverage have no order value: they are confirmed by their kind only (FR-19)."""


@dataclass(frozen=True, slots=True)
class SetTradingStopInput:
    symbol: str
    category: str
    take_profit: str | None = None
    stop_loss: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class ClosePositionInput:
    symbol: str
    category: str
    qty: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class SetLeverageInput:
    symbol: str
    category: str
    leverage: str
    idempotency_key: str | None = None


def _no_position(symbol: str) -> PositionNotFound:
    return PositionNotFound(
        f"there is no open {symbol} position",
        next_step="Check get_positions; this action needs an open position on the symbol.",
        details={"symbol": symbol, "reason": "no_position"},
    )


class _PositionPath[A: TradingAction, M, P, R: PositionChangeResult](ActionPath[A, M, P, R]):
    """What SL/TP and leverage share: storage of answers, no repeat window, and finding out what the
    position shows after the request."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        credentials: RequireTradingCredential,
        *,
        trading: TradingExchange,
        actions: PositionActions,
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
            clock=clock,
            ids=ids,
            exchange=exchange,
            environment=environment,
            send_deadline_s=send_deadline_s,
            reconnaissance_delay_s=reconnaissance_delay_s,
            reconnaissance_deadline_s=reconnaissance_deadline_s,
            fill_read_deadline_s=read_back_deadline_s,
            confirmations=confirmations,
        )
        self._trading = trading
        self._actions = actions

    def _deduplicated_without_key(self, action: A) -> bool:
        return False  # D-20 (3)

    def _store(self, answer: R | DomainError) -> dict[str, JsonValue]:
        return position_answer_payload(answer)

    def _in_progress(self, held: IdempotencyRecord) -> OutcomeUnknown:
        return OutcomeUnknown(
            "an earlier call with the same idempotency_key sent this change and has not answered yet (or did "
            "not complete); this call sent nothing",
            next_step="Repeat exactly this call in a few seconds to get the first call's answer, or check "
            "get_positions.",
            details={"reason": "in_progress", "replayed": "true"},
        )

    async def _read_state(self, keys: ApiKeyPair, symbol: str) -> SymbolPosition | None:
        """The position right after an accepted change; a failed read is "not visible", not an error."""
        try:
            with anyio.move_on_after(self._fill_read_deadline_s):
                return await self._trading.get_symbol_position(keys, symbol)
            log.warning("position_read_back_timed_out", symbol=symbol)
        except Exception:
            log.exception("position_read_back_failed", symbol=symbol)
        return None

    async def _reconnoitre(self, keys: ApiKeyPair, symbol: str) -> tuple[SymbolPosition | None, str]:
        """FR-27/D-21: the position after an unknown outcome; never a resend."""
        found: SymbolPosition | None = None
        looked = "not_found"
        with anyio.move_on_after(self._reconnaissance_deadline_s) as bound:
            try:
                await anyio.sleep(self._reconnaissance_delay_s)
                found = await self._trading.get_symbol_position(keys, symbol)
            except Exception:
                looked = "failed"
                log.exception("position_change_reconnaissance_failed", symbol=symbol)
        if bound.cancelled_caught:
            looked = "failed"
        if found is not None:
            looked = "found"
        return found, looked

    async def _change(
        self,
        attempt: Attempt,
        keys: ApiKeyPair,
        symbol: str,
        send: Callable[[], Awaitable[None]],
        *,
        known: Callable[[SymbolPosition | None, ResultSource], R | None],
        requested: Callable[[SymbolPosition | None], R],
        what: str,
    ) -> R:
        """Send one change and find out what is known. Shielded: once the request may be on its way,
        the outcome is always recorded."""
        with anyio.CancelScope(shield=True):
            try:
                await self._send_within_deadline(send)
            except OutcomeUnknown as exc:
                found, looked = await self._reconnoitre(keys, symbol)
                learned = known(found, ResultSource.RECONNAISSANCE)
                if learned is None:
                    unknown = _unknown(what, symbol, exc, looked)
                    outcome = {**failure("unknown", exc), "reconnaissance": looked}
                    await self._try_record_outcome(attempt, outcome, Outcome.UNKNOWN, unknown)
                    raise unknown from exc
                sent_error: dict[str, JsonValue] = {"code": exc.code, "details": dict(exc.details)}
                accepted = {"status": "accepted", **learned.observed(), "reconnaissance": "found"}
                accepted["send_error"] = sent_error
                await self._try_record_outcome(attempt, accepted, Outcome.ACCEPTED, learned)
                return learned
            except ExchangeUnavailable as exc:  # raised only before anything was sent
                await self._try_record_outcome(attempt, failure("not_sent", exc), Outcome.TRANSIENT, exc)
                raise
            except DomainError as exc:
                await self._try_record_outcome(attempt, failure("rejected", exc), refusal_outcome(exc), exc)
                raise
            except Exception as exc:  # a bug: the request may have been sent
                log.exception("position_change_send_failed", action_id=attempt.action_id)
                cause = OutcomeUnknown("internal error while sending", details={"reason": "internal_error"})
                unknown = _unknown(what, symbol, cause, "skipped")
                await self._try_record_outcome(
                    attempt, {"status": "unknown", "error_type": type(exc).__name__}, Outcome.UNKNOWN, unknown
                )
                raise unknown from exc
            except BaseException as exc:
                unknown = _unknown(what, symbol, OutcomeUnknown("send interrupted"), "skipped")
                await self._try_record_outcome(
                    attempt, {"status": "unknown", "error_type": type(exc).__name__}, Outcome.UNKNOWN, unknown
                )
                raise
            # Accepted: from here on nothing may turn into an error. Worst case: requested, not seen.
            after = await self._read_state(keys, symbol)
            confirmed = known(after, ResultSource.PLACED)
            result = requested(after) if confirmed is None else confirmed
            await self._try_record_outcome(
                attempt, {"status": "accepted", **result.observed()}, Outcome.ACCEPTED, result
            )
        return result


# --- set_trading_stop -----------------------------------------------------------------------------


class SubmitSetTradingStop(_PositionPath[SetTradingStop, StopPlan, StopPlan, StopResult]):
    """Set or move the stop-loss and/or take-profit of the open position."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        credentials: RequireTradingCredential,
        rules: InstrumentRulesCache,
        **kw: Any,
    ) -> None:
        super().__init__(uow, credentials, **kw)
        self._rules = rules

    async def __call__(
        self, user_id: str | None, request: SetTradingStopInput, client: ClientDescription | None = None
    ) -> StopResult:
        action = parse_set_trading_stop(
            symbol=request.symbol,
            category=request.category,
            take_profit=request.take_profit,
            stop_loss=request.stop_loss,
        )
        return await self._submit(user_id, action, request.idempotency_key, client)

    def _payload(self, action: SetTradingStop) -> dict[str, JsonValue]:
        return {
            "symbol": action.symbol,
            "category": action.category.value,
            "take_profit": action.take_profit,
            "stop_loss": action.stop_loss,
        }

    def _restore(self, payload: Mapping[str, Any]) -> StopResult | DomainError:
        answer = restore_position_answer(payload)
        if not isinstance(answer, StopResult | DomainError):
            raise ValueError("stored answer of another action")
        return answer

    def _risk_before_reads(self, action: SetTradingStop) -> RiskDirection:
        return RiskDirection.REDUCES  # FR-43

    def _risk(self, action: SetTradingStop, market: StopPlan) -> RiskDirection:
        return RiskDirection.REDUCES

    async def _read(self, action: SetTradingStop, keys: ApiKeyPair, policy: EffectivePolicy) -> StopPlan:
        state = await self._trading.get_symbol_position(keys, action.symbol)
        # Any status: a position on an instrument being delisted must stay protectable.
        rules = await self._rules.get(action.symbol, action.category, any_status=True)
        require_supported_quote(rules.instrument)
        return plan_trading_stop(action, rules, state.position)

    def _check(self, action: SetTradingStop, market: StopPlan, policy: EffectivePolicy) -> StopPlan:
        return market

    def _confirm_notional(self, action: SetTradingStop, plan: StopPlan) -> Decimal:
        return _NO_THRESHOLD

    def _parse_payload(self, payload: Mapping[str, Any]) -> SetTradingStop:
        return parse_set_trading_stop(
            symbol=payload["symbol"],
            category=payload["category"],
            take_profit=payload["take_profit"],
            stop_loss=payload["stop_loss"],
        )

    def _attempt_details(self, plan: StopPlan) -> dict[str, JsonValue]:
        return {
            "risk": plan.risk.value,
            "position": {
                "side": plan.position_side.value,
                "size": plan.position_size,
                "take_profit": plan.current_take_profit,
                "stop_loss": plan.current_stop_loss,
            },
        }

    async def _execute(self, attempt: Attempt, plan: StopPlan, keys: ApiKeyPair) -> StopResult:
        action = plan.action

        def result(
            state: PositionChangeState, found: SymbolPosition | None, source: ResultSource
        ) -> StopResult:
            position = None if found is None else found.position
            return StopResult(
                attempt.action_id,
                plan,
                state,
                None if position is None else position.take_profit,
                None if position is None else position.stop_loss,
                found is not None,
                source,
            )

        def known(found: SymbolPosition | None, source: ResultSource) -> StopResult | None:
            if found is None or not stop_applied(plan, found.position):
                return None
            return result(PositionChangeState.APPLIED, found, source)

        return await self._change(
            attempt,
            keys,
            action.symbol,
            lambda: self._actions.set_trading_stop(keys, action.symbol, action.take_profit, action.stop_loss),
            known=known,
            requested=lambda found: result(PositionChangeState.REQUESTED, found, ResultSource.PLACED),
            what="set_trading_stop",
        )


# --- set_leverage ---------------------------------------------------------------------------------


class SubmitSetLeverage(_PositionPath[SetLeverage, LeveragePlan, LeveragePlan, LeverageResult]):
    """Set the leverage of a perpetual; a higher value than now increases risk (FR-43)."""

    async def __call__(
        self, user_id: str | None, request: SetLeverageInput, client: ClientDescription | None = None
    ) -> LeverageResult:
        action = parse_set_leverage(
            symbol=request.symbol, category=request.category, leverage=request.leverage
        )
        return await self._submit(user_id, action, request.idempotency_key, client)

    def _payload(self, action: SetLeverage) -> dict[str, JsonValue]:
        return {"symbol": action.symbol, "category": action.category.value, "leverage": action.leverage}

    def _restore(self, payload: Mapping[str, Any]) -> LeverageResult | DomainError:
        answer = restore_position_answer(payload)
        if not isinstance(answer, LeverageResult | DomainError):
            raise ValueError("stored answer of another action")
        return answer

    def _risk_before_reads(self, action: SetLeverage) -> None:
        return None  # it depends on the leverage now (FR-43)

    def _risk(self, action: SetLeverage, market: LeveragePlan) -> RiskDirection:
        return market.risk

    async def _read(self, action: SetLeverage, keys: ApiKeyPair, policy: EffectivePolicy) -> LeveragePlan:
        state = await self._trading.get_symbol_position(keys, action.symbol)
        return plan_leverage(action, state.leverage)

    def _check(self, action: SetLeverage, market: LeveragePlan, policy: EffectivePolicy) -> LeveragePlan:
        check_leverage_limits(market, policy)
        return market

    def _confirm_notional(self, action: SetLeverage, plan: LeveragePlan) -> Decimal:
        return _NO_THRESHOLD

    def _parse_payload(self, payload: Mapping[str, Any]) -> SetLeverage:
        return parse_set_leverage(
            symbol=payload["symbol"], category=payload["category"], leverage=payload["leverage"]
        )

    def _attempt_details(self, plan: LeveragePlan) -> dict[str, JsonValue]:
        return {"risk": plan.risk.value, "leverage": {"from": plan.current, "to": plan.action.leverage}}

    async def _execute(self, attempt: Attempt, plan: LeveragePlan, keys: ApiKeyPair) -> LeverageResult:
        action = plan.action

        def known(found: SymbolPosition | None, source: ResultSource) -> LeverageResult | None:
            if found is None or found.leverage != action.leverage:
                return None
            return LeverageResult(
                attempt.action_id, plan, PositionChangeState.APPLIED, found.leverage, source
            )

        def requested(found: SymbolPosition | None) -> LeverageResult:
            seen = None if found is None else found.leverage
            return LeverageResult(attempt.action_id, plan, PositionChangeState.REQUESTED, seen)

        return await self._change(
            attempt,
            keys,
            action.symbol,
            lambda: self._actions.set_leverage(keys, action.symbol, action.leverage),
            known=known,
            requested=requested,
            what="set_leverage",
        )


# --- close_position -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CloseRead:
    order: PlaceOrder
    """The reduce-only market order that closes (part of) the position as read."""
    market: MarketView
    """What the ``place_order`` machinery read from the exchange for that order."""


class SubmitClosePosition(ActionPath[ClosePosition, _CloseRead, OrderPlan, OrderResult]):
    """Close the position fully or partly with a reduce-only market order (ADR-010), sent by the
    ``place_order`` machinery: its rounding, worst price, client order id and reconnaissance."""

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
        send_deadline_s: float = DEFAULT_SEND_DEADLINE_S,
        reconnaissance_delay_s: float = DEFAULT_RECONNAISSANCE_DELAY_S,
        reconnaissance_deadline_s: float = DEFAULT_RECONNAISSANCE_DEADLINE_S,
        fill_read_deadline_s: float = DEFAULT_FILL_READ_DEADLINE_S,
        confirmations: ConfirmationSupport | None = None,
    ) -> None:
        common: dict[str, Any] = {
            "confirmations": confirmations,
            "clock": clock,
            "ids": ids,
            "exchange": exchange,
            "environment": environment,
            "send_deadline_s": send_deadline_s,
            "reconnaissance_delay_s": reconnaissance_delay_s,
            "reconnaissance_deadline_s": reconnaissance_deadline_s,
            "fill_read_deadline_s": fill_read_deadline_s,
        }
        super().__init__(uow, credentials, actor=AGENT_ACTOR, **common)
        self._trading = trading
        self._placer = SubmitPlaceOrder(
            uow, credentials, rules, market=market, trading=trading, actor=AGENT_ACTOR, **common
        )

    async def __call__(
        self, user_id: str | None, request: ClosePositionInput, client: ClientDescription | None = None
    ) -> OrderResult:
        action = parse_close_position(symbol=request.symbol, category=request.category, qty=request.qty)
        return await self._submit(user_id, action, request.idempotency_key, client)

    def _payload(self, action: ClosePosition) -> dict[str, JsonValue]:
        return {"symbol": action.symbol, "category": action.category.value, "qty": action.qty}

    def _deduplicated_without_key(self, action: ClosePosition) -> bool:
        return False  # it only shrinks the position: a repeat cannot add risk (D-20 (3))

    def _risk_before_reads(self, action: ClosePosition) -> RiskDirection:
        return RiskDirection.REDUCES

    def _risk(self, action: ClosePosition, market: _CloseRead) -> RiskDirection:
        return RiskDirection.REDUCES

    def _client_order_id(self, action_id: str) -> str:
        return self._trading.client_order_id(action_id)

    def _store(self, answer: OrderResult | DomainError) -> dict[str, JsonValue]:
        return answer_payload(answer)

    def _restore(self, payload: Mapping[str, Any]) -> OrderResult | DomainError:
        return restore_answer(payload)

    def _in_progress(self, held: IdempotencyRecord) -> OutcomeUnknown:
        return self._placer._in_progress(held)  # the same order-sending path, the same words

    def _superseded(self, attempt: Attempt, holder: IdempotencyRecord | None) -> OutcomeUnknown:
        return self._placer._superseded(attempt, holder)

    async def _read(self, action: ClosePosition, keys: ApiKeyPair, policy: EffectivePolicy) -> _CloseRead:
        state = await self._trading.get_symbol_position(keys, action.symbol)
        position = state.position
        if position is None:
            raise _no_position(action.symbol)
        side = OrderSide.SELL if position.side is PositionSide.LONG else OrderSide.BUY
        order = PlaceOrder(
            symbol=action.symbol,
            category=Category.LINEAR,
            side=side,
            order_type=OrderKind.MARKET,
            qty=position.size if action.qty is None else action.qty,
            price=None,
            reduce_only=True,
        )
        market = await self._placer._read_market(order, keys)  # rules (any status), band, best price
        self._placer._plan(order, market, policy)  # early: rule violations are input errors
        return _CloseRead(order, market)

    def _check(self, action: ClosePosition, market: _CloseRead, policy: EffectivePolicy) -> OrderPlan:
        return self._placer._plan(market.order, market.market, policy)  # reduces: no size limits (G-2)

    def _confirm_notional(self, action: ClosePosition, plan: OrderPlan) -> Decimal:
        return plan.notional

    def _parse_payload(self, payload: Mapping[str, Any]) -> ClosePosition:
        return parse_close_position(
            symbol=payload["symbol"], category=payload["category"], qty=payload["qty"]
        )

    def _confirmed_plan(
        self, action: ClosePosition, market: _CloseRead, policy: EffectivePolicy, shown: Mapping[str, Any]
    ) -> OrderPlan:
        """FR-48, GP-2: the reduce-only market order the human saw, cut to the position as it is now."""
        return self._placer._confirmed_plan(market.order, market.market, policy, shown)

    async def _reconcile(
        self, pending: PendingConfirmation, action: ClosePosition
    ) -> tuple[ConfirmationStatus, dict[str, JsonValue]]:
        return await self._placer._reconcile(pending, action)

    def _attempt_details(self, plan: OrderPlan) -> dict[str, JsonValue]:
        return self._placer._attempt_details(plan)

    async def _execute(self, attempt: Attempt, plan: OrderPlan, keys: ApiKeyPair) -> OrderResult:
        return await self._placer._send(attempt, plan, keys)


def _unknown(what: str, symbol: str, cause: OutcomeUnknown, looked: str) -> OutcomeUnknown:
    """The change may or may not have been applied; repeating it is harmless (absolute values)."""
    return OutcomeUnknown(
        f"{what} on {symbol} may or may not have been applied ({cause.message}); the position did not "
        "show it",
        next_step=f"Check get_positions for {symbol} in a few seconds and tell the user. Repeating this "
        f"{what} call is safe: it sets the same values and cannot add risk.",
        details={**cause.details, "symbol": symbol, "reconnaissance": looked},
        untrusted_text=cause.untrusted_text,
    )
