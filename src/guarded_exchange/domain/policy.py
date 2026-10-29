"""User policy: admin ceiling, user overrides and the effective policy (SPEC FR-11, FR-12, FR-18,
FR-19, FR-23, FR-42, FR-45; PLAN D-23).

The admin sets a ceiling; the user may only tighten it. The effective policy is computed field by
field as the stricter of the two, so lowering the ceiling below a user's setting always wins
(FR-12). Each field has exactly one "stricter of two values" rule, kept in ``_RULES``; the same rule
decides both the effective value and whether a user's change is a weakening:
``new`` is at least as strict as ``current`` exactly when ``stricter(current, new) == new``.

| field                       | stricter of two values                     |
|-----------------------------|--------------------------------------------|
| profile                     | min (read_only < trade)                    |
| allowlist                   | intersection                               |
| confirm_actions             | union (more actions need confirmation)     |
| confirm_above_notional_usdt | min, ``None`` (no threshold) = +infinity    |
| trust_client_ui             | AND                                        |
| prefer_external_channel     | OR                                         |
| unconfirmed_window_s        | max (counting over a longer window)        |
| every other number          | min                                        |
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from typing import Any, Final

from guarded_exchange.domain.actions import ALL_ACTION_KINDS, ActionKind
from guarded_exchange.domain.audit import JsonValue
from guarded_exchange.domain.errors import InvalidParams, PermissionDenied
from guarded_exchange.domain.instruments import Category, normalize_symbol, parse_category
from guarded_exchange.domain.users import Environment

MAX_SLIPPAGE_PCT: Final = Decimal(3)  # PLAN section 6: hard maximum
MAX_CONFIRMATION_TTL_S: Final = 600  # FR-23: the admin's maximum is at most 10 minutes
MAX_UNCONFIRMED_WINDOW_S: Final = 86_400
MAX_PENDING_LIMIT: Final = 100


class Profile(StrEnum):
    """Permission profile (FR-11). Declaration order is strictness order: read_only is stricter."""

    READ_ONLY = "read_only"
    TRADE = "trade"


class Permission(StrEnum):
    """What an operation needs from the caller's profile."""

    READ = "read"
    TRADE = "trade"


_PROFILE_PERMISSIONS: Final[Mapping[Profile, frozenset[Permission]]] = {
    Profile.READ_ONLY: frozenset({Permission.READ}),
    Profile.TRADE: frozenset({Permission.READ, Permission.TRADE}),
}
_PROFILE_RANK: Final = {profile: rank for rank, profile in enumerate(Profile)}


@dataclass(frozen=True, slots=True, order=True)
class InstrumentRef:
    """An allowlist entry: one symbol in one category (spot BTCUSDT and the perpetual differ)."""

    category: Category
    symbol: str

    def __str__(self) -> str:
        return f"{self.category.value}:{self.symbol}"


def parse_instrument_ref(raw: str) -> InstrumentRef:
    """``"linear:BTCUSDT"`` -> ``InstrumentRef``; anything else raises ``InvalidParams``."""
    category, sep, symbol = raw.strip().partition(":")
    try:
        if not sep:
            raise InvalidParams("no category")
        return InstrumentRef(parse_category(category), normalize_symbol(symbol))
    except InvalidParams:
        raise InvalidParams(
            "allowlist entries look like 'category:SYMBOL', e.g. linear:BTCUSDT or spot:ETHUSDT",
            next_step="Use 'spot' or 'linear' and the exchange symbol without separators.",
            details={"field": "allowlist"},
        ) from None


def parse_decimal(field: str, raw: str) -> Decimal:
    """A decimal from text input (CLI); never via float."""
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        raise _invalid(field, "a decimal number, e.g. 250 or 0.5") from None
    if not value.is_finite():
        raise _invalid(field, "a finite decimal number")
    return value


def parse_action_kinds(raw: list[str]) -> frozenset[ActionKind]:
    """``["all"]`` -> every kind, ``["none"]`` -> no kind, else the named kinds."""
    names = {item.strip().lower() for item in raw}
    if names == {"all"}:
        return ALL_ACTION_KINDS
    if names == {"none"}:
        return frozenset()
    try:
        return frozenset(ActionKind(name) for name in names)
    except ValueError:
        kinds = ", ".join(kind.value for kind in ActionKind)
        raise _invalid("confirm_actions", f"'all', 'none' or a list of: {kinds}") from None


class Unset(Enum):
    """Marks a field a ``PolicyPatch`` leaves alone (``None`` is a real value for some fields)."""

    UNSET = "unset"


UNSET: Final = Unset.UNSET


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicySettings:
    """A complete set of policy values. Values outside hard bounds raise ``InvalidParams``."""

    profile: Profile
    allowlist: frozenset[InstrumentRef]
    max_order_notional_usdt: Decimal
    max_leverage: Decimal
    slippage_pct: Decimal
    confirm_actions: frozenset[ActionKind]
    confirm_above_notional_usdt: Decimal | None  # also confirm any order at least this big; None = off
    confirmation_ttl_s: int
    trust_client_ui: bool
    prefer_external_channel: bool
    unconfirmed_max_actions: int  # FR-46: risk-increasing actions without confirmation ...
    unconfirmed_window_s: int  # ... per this sliding window of seconds
    max_pending: int

    def __post_init__(self) -> None:
        for name, rule in _RULES.items():
            rule.check(name, getattr(self, name))


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminCeiling(PolicySettings):
    """The most a user may do; only the admin sets it (CLI)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectivePolicy(PolicySettings):
    """What actually applies to the user: ``strictest(ceiling, overrides)`` plus the environment."""

    environment: Environment


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyPatch:
    """Some policy fields; ``UNSET`` ones are left alone.

    Used both for the user's stored overrides (``UserOverrides``) and for a change request
    (admin ceiling change or user tightening).
    """

    profile: Profile | Unset = UNSET
    allowlist: frozenset[InstrumentRef] | Unset = UNSET
    max_order_notional_usdt: Decimal | Unset = UNSET
    max_leverage: Decimal | Unset = UNSET
    slippage_pct: Decimal | Unset = UNSET
    confirm_actions: frozenset[ActionKind] | Unset = UNSET
    confirm_above_notional_usdt: Decimal | Unset | None = UNSET
    confirmation_ttl_s: int | Unset = UNSET
    trust_client_ui: bool | Unset = UNSET
    prefer_external_channel: bool | Unset = UNSET
    unconfirmed_max_actions: int | Unset = UNSET
    unconfirmed_window_s: int | Unset = UNSET
    max_pending: int | Unset = UNSET

    def __post_init__(self) -> None:
        for name, value in self.changes().items():
            _RULES[name].check(name, value)

    def changes(self) -> dict[str, Any]:
        """The fields this patch sets, in declaration order."""
        return {f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name) is not UNSET}

    @property
    def is_empty(self) -> bool:
        return not self.changes()


UserOverrides = PolicyPatch
"""The user's own settings: only the fields they tightened (PLAN 5.1)."""

NO_OVERRIDES: Final = PolicyPatch()


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyRecord:
    """What is stored per user. ``ceiling is None``: the admin never set one, the default applies."""

    ceiling: AdminCeiling | None
    overrides: UserOverrides
    updated_at: datetime


# --- strictest and validation -------------------------------------------------------------


def strictest(ceiling: AdminCeiling, overrides: UserOverrides, environment: Environment) -> EffectivePolicy:
    """Field by field the stricter of the ceiling and the user's override (FR-12, D-23)."""
    values: dict[str, Any] = {}
    for name, rule in _RULES.items():
        override = getattr(overrides, name)
        ceiling_value = getattr(ceiling, name)
        values[name] = ceiling_value if override is UNSET else rule.stricter(ceiling_value, override)
    return EffectivePolicy(environment=environment, **values)


def weakened_fields(current: PolicySettings, change: PolicyPatch) -> list[str]:
    """Fields of ``change`` that are less strict than ``current`` (equal is not a weakening)."""
    return [
        name
        for name, value in change.changes().items()
        if _RULES[name].stricter(getattr(current, name), value) != value
    ]


def validate_override(current: PolicySettings, change: PolicyPatch) -> None:
    """Refuse a user change that weakens any field of the current effective policy (FR-12).

    Checking against the effective policy (not just the ceiling) also keeps the user inside
    the ceiling, because the effective policy is never looser than the ceiling.
    """
    weaker = weakened_fields(current, change)
    if weaker:
        raise PermissionDenied(
            f"a user can only tighten the policy; these settings would weaken it: {', '.join(weaker)}",
            next_step="Pick values at least as strict as the current ones (`guarded-exchange policy show`); "
            "only the admin can raise the ceiling.",
            details={"field": ",".join(weaker)},
        )


def apply_patch[S: PolicySettings | PolicyPatch](target: S, change: PolicyPatch) -> S:
    """``target`` with the fields of ``change`` replaced (no strictness check here)."""
    return replace(target, **change.changes())


def parse_policy_fields(names: Iterable[str]) -> frozenset[str]:
    """Policy field names from text (CLI); an unknown name raises ``InvalidParams`` listing the valid ones."""
    wanted = {name.strip().lower().replace("-", "_") for name in names}
    unknown = sorted(wanted - _RULES.keys())
    if unknown:
        raise InvalidParams(
            f"unknown policy settings: {', '.join(unknown)}; known: {', '.join(_RULES)}",
            details={"field": "fields"},
        )
    return frozenset(wanted)


def without_fields(overrides: UserOverrides, names: frozenset[str]) -> UserOverrides:
    """``overrides`` with ``names`` unset, so the ceiling applies to them again (no check here)."""
    return replace(overrides, **dict.fromkeys(names, UNSET))


# --- permissions (FR-11) ------------------------------------------------------------------


def granted_permissions(policy: PolicySettings) -> frozenset[Permission]:
    return _PROFILE_PERMISSIONS[policy.profile]


def require_permission(policy: PolicySettings, permission: Permission) -> None:
    if permission not in granted_permissions(policy):
        raise PermissionDenied(
            f"your profile '{policy.profile.value}' does not allow this ({permission.value} access needed)",
            next_step="Do not retry. Tell the user: only the admin can grant this, outside the chat.",
            details={"profile": policy.profile.value, "required": permission.value},
        )


def permitted[K](policy: PolicySettings | None, requirements: Mapping[K, Permission]) -> frozenset[K]:
    """Keys whose required permission the policy grants. No policy (unknown caller) -> nothing."""
    if policy is None:
        return frozenset()
    granted = granted_permissions(policy)
    return frozenset(key for key, permission in requirements.items() if permission in granted)


# --- defaults (FR-18, FR-45, PLAN section 6) ---------------------------------------------


def default_ceiling(environment: Environment) -> AdminCeiling:
    """The ceiling of a user the admin has not configured: read-only, empty allowlist (FR-45).

    The limits are the FR-18 defaults for the environment, so granting ``trade`` alone gives
    the documented limits; mainnet is always stricter than testnet.
    """
    mainnet = environment is Environment.MAINNET
    return AdminCeiling(
        profile=Profile.READ_ONLY,
        allowlist=frozenset(),
        max_order_notional_usdt=Decimal(50) if mainnet else Decimal(1000),
        max_leverage=Decimal(3) if mainnet else Decimal(10),
        slippage_pct=Decimal("0.5") if mainnet else Decimal(2),
        confirm_actions=ALL_ACTION_KINDS,
        confirm_above_notional_usdt=None,
        confirmation_ttl_s=120,
        trust_client_ui=not mainnet,
        prefer_external_channel=False,
        unconfirmed_max_actions=5 if mainnet else 20,
        unconfirmed_window_s=600,
        max_pending=5 if mainnet else 10,
    )


# --- serialisation (journal payloads and storage) -----------------------------------------


def settings_to_payload(settings: PolicySettings) -> dict[str, JsonValue]:
    return {name: rule.encode(getattr(settings, name)) for name, rule in _RULES.items()}


def patch_to_payload(patch: PolicyPatch) -> dict[str, JsonValue]:
    return {name: _RULES[name].encode(value) for name, value in patch.changes().items()}


def ceiling_from_payload(data: Mapping[str, Any]) -> AdminCeiling:
    missing = _RULES.keys() - data.keys()
    if missing:
        raise ValueError(f"stored ceiling lacks fields: {sorted(missing)}")
    return AdminCeiling(**_decode(data))


def patch_from_payload(data: Mapping[str, Any]) -> PolicyPatch:
    return PolicyPatch(**_decode(data))


def _decode(data: Mapping[str, Any]) -> dict[str, Any]:
    unknown = data.keys() - _RULES.keys()
    if unknown:
        raise ValueError(f"unknown policy fields: {sorted(unknown)}")
    return {name: _RULES[name].decode(value) for name, value in data.items()}


# --- per-field rules ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Rule:
    stricter: Callable[[Any, Any], Any]
    check: Callable[[str, Any], None]
    encode: Callable[[Any], JsonValue]
    decode: Callable[[Any], Any]


def _invalid(name: str, requirement: str) -> InvalidParams:
    return InvalidParams(f"{name} must be {requirement}", details={"field": name})


def _decimal_check(low: Decimal, high: Decimal | None = None) -> Callable[[str, Any], None]:
    def check(name: str, value: Any) -> None:
        bounds = f">= {low}" + ("" if high is None else f" and <= {high}")
        if not isinstance(value, Decimal) or not value.is_finite():
            raise _invalid(name, f"a finite decimal {bounds}")
        if value < low or (high is not None and value > high):
            raise _invalid(name, bounds)

    return check


def _optional_decimal_check(low: Decimal) -> Callable[[str, Any], None]:
    inner = _decimal_check(low)

    def check(name: str, value: Any) -> None:
        if value is not None:
            inner(name, value)

    return check


def _int_check(low: int, high: int) -> Callable[[str, Any], None]:
    def check(name: str, value: Any) -> None:
        if type(value) is not int or not low <= value <= high:
            raise _invalid(name, f"an integer from {low} to {high}")

    return check


def _bool_check(name: str, value: Any) -> None:
    if type(value) is not bool:
        raise _invalid(name, "true or false")


def _instance_check(kind: type) -> Callable[[str, Any], None]:
    def check(name: str, value: Any) -> None:
        if not isinstance(value, kind):
            raise _invalid(name, f"a {kind.__name__}")

    return check


def _set_check(kind: type) -> Callable[[str, Any], None]:
    def check(name: str, value: Any) -> None:
        if not isinstance(value, frozenset) or not all(isinstance(item, kind) for item in value):
            raise _invalid(name, f"a set of {kind.__name__}")

    return check


def _min_optional(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _decimal_in(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValueError(f"stored decimal must be a string, got {type(value).__name__}")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError(f"stored decimal is not a number: {value!r}") from None


def _identity(value: Any) -> Any:
    return value


_DECIMAL = {"encode": _identity, "decode": _decimal_in}

_RULES: Final[Mapping[str, _Rule]] = {
    "profile": _Rule(
        stricter=lambda a, b: min(a, b, key=_PROFILE_RANK.__getitem__),
        check=_instance_check(Profile),
        encode=lambda v: v.value,
        decode=Profile,
    ),
    "allowlist": _Rule(
        stricter=lambda a, b: a & b,
        check=_set_check(InstrumentRef),
        encode=lambda v: [str(item) for item in sorted(v)],
        decode=lambda v: frozenset(parse_instrument_ref(item) for item in v),
    ),
    "max_order_notional_usdt": _Rule(stricter=min, check=_decimal_check(Decimal(0)), **_DECIMAL),
    "max_leverage": _Rule(stricter=min, check=_decimal_check(Decimal(1)), **_DECIMAL),
    "slippage_pct": _Rule(stricter=min, check=_decimal_check(Decimal(0), MAX_SLIPPAGE_PCT), **_DECIMAL),
    "confirm_actions": _Rule(
        stricter=lambda a, b: a | b,
        check=_set_check(ActionKind),
        encode=lambda v: sorted(item.value for item in v),
        decode=lambda v: frozenset(ActionKind(item) for item in v),
    ),
    "confirm_above_notional_usdt": _Rule(
        stricter=_min_optional,
        check=_optional_decimal_check(Decimal(0)),
        encode=_identity,
        decode=lambda v: None if v is None else _decimal_in(v),
    ),
    "confirmation_ttl_s": _Rule(
        stricter=min, check=_int_check(1, MAX_CONFIRMATION_TTL_S), encode=_identity, decode=_identity
    ),
    "trust_client_ui": _Rule(
        stricter=lambda a, b: a and b, check=_bool_check, encode=_identity, decode=_identity
    ),
    "prefer_external_channel": _Rule(
        stricter=lambda a, b: a or b, check=_bool_check, encode=_identity, decode=_identity
    ),
    "unconfirmed_max_actions": _Rule(
        stricter=min, check=_int_check(0, 10_000), encode=_identity, decode=_identity
    ),
    "unconfirmed_window_s": _Rule(
        stricter=max, check=_int_check(1, MAX_UNCONFIRMED_WINDOW_S), encode=_identity, decode=_identity
    ),
    "max_pending": _Rule(
        stricter=min, check=_int_check(0, MAX_PENDING_LIMIT), encode=_identity, decode=_identity
    ),
}
