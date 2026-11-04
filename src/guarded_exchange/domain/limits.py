"""Risk limits and the confirmation rule applied to an order plan (SPEC FR-13 - FR-15, FR-17, FR-19,
FR-25, 7.1; PLAN ADR-010).

Limits apply to risk-increasing actions only (G-2): a reduce-only order is bounded by the position
(``domain.risk``), not by the order size, the allowlist or leverage. Every refusal names the limit
and the allowed value (FR-17); the caller journals it.
"""

from decimal import Decimal

from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.audit import canonical_decimal
from guarded_exchange.domain.errors import ConfirmationChannelUnavailable, LimitExceeded, PermissionDenied
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.order_changes import AmendPlan
from guarded_exchange.domain.orders import OrderPlan
from guarded_exchange.domain.policy import EffectivePolicy, InstrumentRef
from guarded_exchange.domain.risk import RiskDirection
from guarded_exchange.domain.users import Environment

_ADMIN_ONLY = (
    "Do not retry or work around it (e.g. by splitting the order). Tell the user; only the admin can "
    "change the limits, outside the chat."
)


def require_trading_environment(environment: Environment) -> None:
    """v1 trades on the test environment only (SPEC 7.1); mainnet trading is v2."""
    if environment is not Environment.TESTNET:
        raise PermissionDenied(
            "trading is available on the test environment only in this version",
            next_step="Do not retry. Tell the user: mainnet trading is not supported yet.",
            details={"reason": "mainnet_trading_unavailable"},
        )


def check_limits(plan: OrderPlan, policy: EffectivePolicy, *, leverage: Decimal | None) -> None:
    """Raise ``LimitExceeded`` if a risk-increasing ``plan`` breaks the policy (FR-13, FR-14, FR-15).

    ``leverage`` is the account's current leverage on the perpetual (``None``: unknown); a new
    perpetual order trades at it, so it must not exceed the policy's maximum. Unknown -> refused.
    """
    if plan.risk is RiskDirection.REDUCES:
        return
    action = plan.action
    _check_increasing(
        InstrumentRef(action.category, action.symbol),
        policy,
        notional=plan.notional,
        notional_price=plan.notional_price,
        price_basis=_price_basis(plan),
        leverage=leverage,
    )


def check_amend_limits(plan: AmendPlan, policy: EffectivePolicy, *, leverage: Decimal | None) -> None:
    """The same limits for an amend that increases risk (FR-43): allowlist, the order's new value
    ``qty x price`` and, on a perpetual, the current leverage. A risk-reducing amend faces none."""
    if plan.risk is RiskDirection.REDUCES:
        return
    action = plan.action
    _check_increasing(
        InstrumentRef(action.category, action.symbol),
        policy,
        notional=plan.notional,
        notional_price=plan.price,
        price_basis="limit price",
        leverage=leverage,
    )


def _check_increasing(
    ref: InstrumentRef,
    policy: EffectivePolicy,
    *,
    notional: Decimal,
    notional_price: Decimal,
    price_basis: str,
    leverage: Decimal | None,
) -> None:
    if ref not in policy.allowlist:
        allowed = ", ".join(str(item) for item in sorted(policy.allowlist)) or "none"
        raise LimitExceeded(
            f"{ref} is not in your allowed instruments ({allowed})",
            next_step=_ADMIN_ONLY,
            details={"limit": "allowlist", "instrument": str(ref)},
        )
    maximum = policy.max_order_notional_usdt
    if notional > maximum:
        raise LimitExceeded(
            f"order value {canonical_decimal(notional)} USDT (qty x {price_basis} "
            f"{canonical_decimal(notional_price)}) exceeds your limit of {canonical_decimal(maximum)} "
            "USDT per order",
            next_step="Reduce qty so that the order value stays within the limit; do not split one order "
            "into several to get around it.",
            details={
                "limit": "max_order_notional_usdt",
                "allowed": canonical_decimal(maximum),
                "notional": canonical_decimal(notional),
                "price": canonical_decimal(notional_price),
            },
        )
    if ref.category is Category.LINEAR:
        if leverage is None:
            raise LimitExceeded(
                f"the current leverage of {ref.symbol} could not be determined",
                next_step="Retry in a few seconds; check get_positions.",
                details={"limit": "max_leverage", "allowed": canonical_decimal(policy.max_leverage)},
            )
        if leverage > policy.max_leverage:
            raise LimitExceeded(
                f"{ref.symbol} is set to leverage x{canonical_decimal(leverage)}, above your limit of "
                f"x{canonical_decimal(policy.max_leverage)}",
                next_step="Do not retry. Tell the user to lower the leverage of this perpetual on the "
                "exchange.",
                details={
                    "limit": "max_leverage",
                    "allowed": canonical_decimal(policy.max_leverage),
                    "leverage": canonical_decimal(leverage),
                },
            )


def _price_basis(plan: OrderPlan) -> str:
    if plan.worst_price is None:
        return "limit price"
    if plan.notional_price != plan.price:
        return "best bid"  # a market sell: it can fill up to the bid
    return "worst price"


def requires_confirmation(policy: EffectivePolicy, kind: ActionKind, notional: Decimal) -> bool:
    """FR-19: the kind is in ``confirm_actions``, or the order is at least the confirmation threshold."""
    threshold = policy.confirm_above_notional_usdt
    return kind in policy.confirm_actions or (threshold is not None and notional >= threshold)


def no_confirmation_channel(kind: ActionKind) -> ConfirmationChannelUnavailable:
    """FR-25: no trusted confirmation channel is enabled: fail closed, nothing is sent or parked."""
    return ConfirmationChannelUnavailable(
        f"your policy requires confirming {kind.value}, and no confirmation channel is available",
        next_step="Do not retry and do not try to confirm it yourself. Tell the user: the action needs "
        "their confirmation, and no channel to ask for it is enabled; the admin can enable CLI confirmation "
        "(GX_CONFIRM_CLI_ENABLED=true) or allow the action without confirmation "
        "(`admin policy set-ceiling --confirm ...`), outside the chat.",
        details={"action": kind.value, "reason": "no_trusted_channel"},
    )
