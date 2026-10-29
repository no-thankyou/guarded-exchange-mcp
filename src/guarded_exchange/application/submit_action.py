"""The single path of a trading action, ``place_order`` for now (SPEC FR-4, FR-10, FR-13 - FR-15,
FR-17, FR-25, FR-36, FR-43; PLAN ADR-008, ADR-010, D-8, D-10, D-15, D-19).

auth -> key check (D-24) -> exchange reads -> plan (rules, rounding, worst price) -> one write
transaction under ``lock_user``: re-check access, key and policy, limits, confirmation, journal the
refusal or the attempt -> send outside any transaction -> read the execution -> journal the outcome.

Network I/O never happens inside a database transaction (D-10): everything the decision needs from
the exchange is read first; the decision and its journal record are one transaction, so a refusal
(FR-17) or an attempt is recorded exactly when it is decided. The attempt is committed before the
request leaves, the outcome after the answer (ADR-008). Confirmation by a human is not built yet:
if the policy requires it, the action is refused with ``confirmation_channel_unavailable`` and
nothing is sent (FR-25, fail closed); trading without confirmation needs the admin to say so.

Out of scope here (ticket 012): idempotency keys, the 30 s duplicate window and finding out the
real outcome of an ``outcome_unknown`` order; the attempt record already carries the client order id
that reconnaissance will look for.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import anyio
import structlog

from guarded_exchange.application.credentials import RequireTradingCredential
from guarded_exchange.application.instrument_rules import InstrumentRulesCache
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
from guarded_exchange.domain.actions import OrderKind, PlaceOrder, parse_place_order
from guarded_exchange.domain.audit import AuditEvent, AuditEventType, ClientDescription, JsonValue
from guarded_exchange.domain.credentials import ApiKeyPair, key_replaced_or_deleted
from guarded_exchange.domain.errors import DomainError, ExchangeUnavailable, OutcomeUnknown, PermissionDenied
from guarded_exchange.domain.instruments import Category, InstrumentRules, require_supported_quote
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
    PriceBand,
    plan_order,
)
from guarded_exchange.domain.policy import EffectivePolicy, Permission, require_permission
from guarded_exchange.domain.risk import RiskDirection, classify
from guarded_exchange.domain.users import Environment, User

log = structlog.get_logger(__name__)

AGENT_ACTOR: Final = "mcp:agent"
"""The journal's actor for actions an AI agent asked for (the user is the chain)."""


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


@dataclass(frozen=True, slots=True)
class OrderResult:
    """An order the exchange accepted. ``fill is None``: its execution is not visible yet (D-15)."""

    action_id: str
    client_order_id: str
    order_id: str
    plan: OrderPlan
    fill: OrderFill | None

    @property
    def price(self) -> Decimal:
        """The order's price as the exchange shows it; what was sent while the order is not visible."""
        if self.fill is not None and self.fill.price is not None:
            return self.fill.price
        return self.plan.price


@dataclass(frozen=True, slots=True)
class _Market:
    """Everything the decision needs from the exchange, read before the transaction (D-10)."""

    rules: InstrumentRules
    band: PriceBand
    best: BestPrices | None
    symbol_position: SymbolPosition | None


@dataclass(frozen=True, slots=True)
class _Attempt:
    user_id: str
    action_id: str
    client_order_id: str
    credential_id: str
    environment: Environment
    client: ClientDescription | None


class SubmitPlaceOrder:
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
    ) -> None:
        self._uow = uow
        self._credentials = credentials
        self._rules = rules
        self._market = market
        self._trading = trading
        self._clock = clock
        self._ids = ids
        self._exchange = exchange
        self._environment = environment
        self._actor = actor

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
        user = await self._principal(user_id)  # unknown or revoked: nothing to journal it under
        try:
            policy = await self._authorize(user)
            cleared = await self._credentials(user.id)  # D-24 re-check if stale; may talk to the exchange
        except DomainError as refusal:  # FR-36: every attempt of a known user, refused ones too
            await self._journal_early_refusal(user, action, refusal, client)
            raise
        market = await self._read_market(action, cleared.keys)
        # Early plan: rule violations (tick, minimums, status) are input errors, answered before
        # anything is decided or journaled. The decision below plans again with the fresh policy.
        self._plan(action, market, policy)
        action_id = self._ids.new_id()
        attempt = _Attempt(
            user_id=user.id,
            action_id=action_id,
            client_order_id=self._trading.client_order_id(action_id),
            credential_id=cleared.credential.id,
            environment=user.environment,
            client=client,
        )
        decided = await self._decide(attempt, action, market)
        if isinstance(decided, DomainError):
            raise decided
        return await self._send(attempt, decided, cleared.keys)

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

    async def _journal_early_refusal(
        self, user: User, action: PlaceOrder, refusal: DomainError, client: ClientDescription | None
    ) -> None:
        payload: dict[str, JsonValue] = {
            "tool": action.kind.value,
            "stage": "pre_check",
            "environment": user.environment.value,
            "client": None if client is None else client.to_payload(),
            "request": _action_payload(action),
            "code": refusal.code,
            "details": dict(refusal.details),
        }
        async with self._uow(write=True) as uow:
            await uow.audit.append(
                user.id, AuditEvent(AuditEventType.ORDER_REFUSED, self._actor, payload), self._clock.now()
            )

    def _require_environment(self, user: User) -> None:
        if user.environment is not self._environment:
            raise PermissionDenied(
                f"your account is on {user.environment.value}, but this server is connected to the "
                f"{self._environment.value} exchange environment",
                next_step="Do not retry. Tell the user; the admin runs a server for their environment.",
                details={"reason": "environment_mismatch"},
            )
        require_trading_environment(user.environment)

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

    # --- the decision: one write transaction ---------------------------------------------------

    async def _decide(
        self, attempt: _Attempt, action: PlaceOrder, market: _Market
    ) -> OrderPlan | DomainError:
        """Refusal or plan, journaled in the same transaction that decided it (FR-17, ADR-008)."""
        async with self._uow(write=True) as uow:
            await uow.lock_user(attempt.user_id)
            try:
                plan = await self._decide_locked(uow, attempt, action, market)
            except DomainError as refusal:
                await uow.audit.append(
                    attempt.user_id, self._refused(attempt, action, refusal), self._clock.now()
                )
                return refusal
            await uow.audit.append(attempt.user_id, self._attempted(attempt, plan), self._clock.now())
            return plan

    async def _decide_locked(
        self, uow: UnitOfWork, attempt: _Attempt, action: PlaceOrder, market: _Market
    ) -> OrderPlan:
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
        plan = self._plan(action, market, policy)
        leverage = market.symbol_position.leverage if market.symbol_position is not None else None
        check_limits(plan, policy, leverage=leverage)
        if requires_confirmation(policy, action.kind, plan.notional):
            raise no_confirmation_channel(action.kind)  # FR-25 until tickets 016/020
        return plan

    # --- sending: outside any transaction ------------------------------------------------------

    async def _send(self, attempt: _Attempt, plan: OrderPlan, keys: ApiKeyPair) -> OrderResult:
        request = OrderRequest.from_plan(plan, attempt.client_order_id)
        # Shielded: once the request may be on its way, a cancelled call (the client went away)
        # must not skip the outcome record. Every exchange call here has a timeout.
        with anyio.CancelScope(shield=True):
            try:
                placed = await self._trading.place_order(keys, request)
            except OutcomeUnknown as exc:
                await self._journal_outcome(attempt, _failure("unknown", exc))
                raise self._unknown(attempt, exc) from exc
            except ExchangeUnavailable as exc:  # the adapter raises it only before anything was sent
                await self._journal_outcome(attempt, _failure("not_sent", exc))
                raise
            except DomainError as exc:
                await self._journal_outcome(attempt, _failure("rejected", exc))
                raise
            except BaseException as exc:  # a bug or cancellation: the order may have been sent
                await self._journal_outcome(attempt, {"status": "unknown", "error_type": type(exc).__name__})
                raise
            # The exchange accepted the order: from here on nothing may turn into an error the agent
            # could read as "failed" and retry (a second order). Worst case: accepted, fill unknown.
            try:
                fill = await self._read_fill(keys, plan, attempt.client_order_id)
                await self._journal_outcome(attempt, _accepted(placed.order_id, fill))
            except Exception:
                log.exception(
                    "order_outcome_not_recorded",
                    action_id=attempt.action_id,
                    client_order_id=attempt.client_order_id,
                    order_id=placed.order_id,
                )
                fill = None
        return OrderResult(attempt.action_id, attempt.client_order_id, placed.order_id, plan, fill)

    async def _read_fill(self, keys: ApiKeyPair, plan: OrderPlan, client_order_id: str) -> OrderFill | None:
        """The order right after placement (D-15). A failed read is "not visible yet", not an error:
        the order was accepted, and the agent is told how to check."""
        try:
            return await self._trading.get_order(
                keys, plan.action.category, plan.action.symbol, client_order_id
            )
        except DomainError:
            return None

    async def _journal_outcome(self, attempt: _Attempt, outcome: Mapping[str, JsonValue]) -> None:
        payload: dict[str, JsonValue] = {
            "action_id": attempt.action_id,
            "client_order_id": attempt.client_order_id,
            **outcome,
        }
        async with self._uow(write=True) as uow:
            await uow.audit.append(
                attempt.user_id,
                AuditEvent(AuditEventType.ORDER_OUTCOME, self._actor, payload),
                self._clock.now(),
            )

    @staticmethod
    def _unknown(attempt: _Attempt, cause: OutcomeUnknown) -> OutcomeUnknown:
        return OutcomeUnknown(
            "the order may or may not have reached the exchange: " + cause.message,
            next_step="Do not place it again. Check get_open_orders (and the order history) for "
            f"client_order_id {attempt.client_order_id} first, then tell the user what you found.",
            details={**cause.details, "client_order_id": attempt.client_order_id},
        )

    # --- journal payloads ------------------------------------------------------------------------

    def _base(self, attempt: _Attempt, action: PlaceOrder) -> dict[str, JsonValue]:
        return {
            "tool": action.kind.value,
            "action_id": attempt.action_id,
            "client_order_id": attempt.client_order_id,
            "credential_id": attempt.credential_id,
            "environment": attempt.environment.value,
            "client": None if attempt.client is None else attempt.client.to_payload(),
            "request": _action_payload(action),
        }

    def _refused(self, attempt: _Attempt, action: PlaceOrder, refusal: DomainError) -> AuditEvent:
        payload = {**self._base(attempt, action), "code": refusal.code, "details": dict(refusal.details)}
        return AuditEvent(AuditEventType.ORDER_REFUSED, self._actor, payload)

    def _attempted(self, attempt: _Attempt, plan: OrderPlan) -> AuditEvent:
        payload = {
            **self._base(attempt, plan.action),
            "decision": "allowed_without_confirmation",
            "order": _plan_payload(plan),
        }
        return AuditEvent(AuditEventType.ORDER_ATTEMPT, self._actor, payload)


def _action_payload(action: PlaceOrder) -> dict[str, JsonValue]:
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


def _failure(status: str, error: DomainError) -> dict[str, JsonValue]:
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
