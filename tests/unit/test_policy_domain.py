"""Domain policy: strictest() per field, weakening refusal, defaults, bounds, permissions (D-23)."""

import json
from dataclasses import fields, replace
from decimal import Decimal
from typing import Any

import pytest

from guarded_exchange.domain.actions import ALL_ACTION_KINDS, ActionKind
from guarded_exchange.domain.audit import canonical_json
from guarded_exchange.domain.errors import InvalidParams, PermissionDenied
from guarded_exchange.domain.instruments import Category
from guarded_exchange.domain.policy import (
    NO_OVERRIDES,
    UNSET,
    AdminCeiling,
    InstrumentRef,
    Permission,
    PolicyPatch,
    PolicySettings,
    Profile,
    apply_patch,
    ceiling_from_payload,
    default_ceiling,
    granted_permissions,
    parse_action_kinds,
    parse_decimal,
    parse_instrument_ref,
    patch_from_payload,
    patch_to_payload,
    permitted,
    require_permission,
    settings_to_payload,
    strictest,
    validate_override,
    weakened_fields,
)
from guarded_exchange.domain.users import Environment

BTC = InstrumentRef(Category.LINEAR, "BTCUSDT")
ETH = InstrumentRef(Category.LINEAR, "ETHUSDT")
SOL = InstrumentRef(Category.SPOT, "SOLUSDT")
PLACE, CANCEL, LEVERAGE = ActionKind.PLACE_ORDER, ActionKind.CANCEL_ORDER, ActionKind.SET_LEVERAGE
POLICY_FIELDS = [f.name for f in fields(PolicySettings)]


def trading_ceiling(**changes: Any) -> AdminCeiling:
    """Testnet defaults with trading enabled on BTC and ETH."""
    base = replace(
        default_ceiling(Environment.TESTNET), profile=Profile.TRADE, allowlist=frozenset({BTC, ETH})
    )
    return replace(base, **changes)


# One row per field and rule: (field, ceiling value, user value, effective value).
STRICTEST_CASES: list[tuple[str, Any, Any, Any]] = [
    ("profile", Profile.TRADE, Profile.READ_ONLY, Profile.READ_ONLY),
    ("profile", Profile.READ_ONLY, Profile.TRADE, Profile.READ_ONLY),
    ("allowlist", frozenset({BTC, ETH}), frozenset({ETH, SOL}), frozenset({ETH})),  # intersection
    ("allowlist", frozenset({BTC}), frozenset(), frozenset()),
    ("max_order_notional_usdt", Decimal(1000), Decimal("250.5"), Decimal("250.5")),  # min
    ("max_order_notional_usdt", Decimal(100), Decimal(500), Decimal(100)),
    ("max_leverage", Decimal(10), Decimal(3), Decimal(3)),
    ("max_leverage", Decimal(2), Decimal(5), Decimal(2)),
    ("slippage_pct", Decimal(2), Decimal("0.5"), Decimal("0.5")),
    ("slippage_pct", Decimal("0.5"), Decimal(2), Decimal("0.5")),
    ("confirm_actions", frozenset({PLACE}), frozenset({CANCEL}), frozenset({PLACE, CANCEL})),  # union
    ("confirm_actions", frozenset(), frozenset({LEVERAGE}), frozenset({LEVERAGE})),
    ("confirm_above_notional_usdt", Decimal(500), Decimal(100), Decimal(100)),  # min threshold
    ("confirm_above_notional_usdt", None, Decimal(100), Decimal(100)),  # None = no threshold = +inf
    ("confirm_above_notional_usdt", Decimal(100), None, Decimal(100)),
    ("confirmation_ttl_s", 120, 60, 60),
    ("confirmation_ttl_s", 60, 600, 60),
    ("trust_client_ui", True, False, False),  # AND
    ("trust_client_ui", False, True, False),
    ("trust_client_ui", True, True, True),
    ("prefer_external_channel", False, True, True),  # OR
    ("prefer_external_channel", True, False, True),
    ("prefer_external_channel", False, False, False),
    ("unconfirmed_max_actions", 20, 5, 5),
    ("unconfirmed_max_actions", 5, 20, 5),
    ("unconfirmed_window_s", 600, 3600, 3600),  # max: counting over a longer window is stricter
    ("unconfirmed_window_s", 3600, 600, 3600),
    ("max_pending", 10, 3, 3),
    ("max_pending", 3, 10, 3),
]


@pytest.mark.req("FR-12", "FR-15", "FR-19", "FR-23", "FR-42")
@pytest.mark.parametrize(("field", "ceiling_value", "user_value", "expected"), STRICTEST_CASES)
def test_strictest_takes_the_stricter_value_of_each_field(
    field: str, ceiling_value: Any, user_value: Any, expected: Any
) -> None:
    ceiling = trading_ceiling(**{field: ceiling_value})

    effective = strictest(ceiling, PolicyPatch(**{field: user_value}), Environment.TESTNET)

    assert getattr(effective, field) == expected
    # every other field is the ceiling's, and the environment is the user's
    others = {name: getattr(ceiling, name) for name in POLICY_FIELDS if name != field}
    assert {name: getattr(effective, name) for name in others} == others
    assert effective.environment is Environment.TESTNET


def test_every_policy_field_has_a_strictest_case() -> None:
    assert {case[0] for case in STRICTEST_CASES} == set(POLICY_FIELDS)


def test_no_overrides_means_the_ceiling() -> None:
    ceiling = trading_ceiling()

    effective = strictest(ceiling, NO_OVERRIDES, Environment.MAINNET)

    assert settings_to_payload(effective) == settings_to_payload(ceiling)
    assert effective.environment is Environment.MAINNET


# One weakening per field, relative to ``trading_ceiling()``.
WEAKENINGS: list[tuple[str, Any]] = [
    ("profile", Profile.TRADE),  # only weaker than read_only; see the read-only current below
    ("allowlist", frozenset({BTC, SOL})),
    ("max_order_notional_usdt", Decimal("1000.01")),
    ("max_leverage", Decimal(11)),
    ("slippage_pct", Decimal("2.5")),
    ("confirm_actions", frozenset({PLACE})),
    ("confirm_above_notional_usdt", None),  # current has a threshold, None removes it
    ("confirmation_ttl_s", 121),
    ("trust_client_ui", True),
    ("prefer_external_channel", False),
    ("unconfirmed_max_actions", 21),
    ("unconfirmed_window_s", 599),
    ("max_pending", 11),
]


def strict_current() -> AdminCeiling:
    """A current policy against which each WEAKENINGS value is looser."""
    return trading_ceiling(
        profile=Profile.READ_ONLY,
        confirm_above_notional_usdt=Decimal(100),
        trust_client_ui=False,
        prefer_external_channel=True,
    )


@pytest.mark.req("FR-12", "FR-44")
@pytest.mark.parametrize(("field", "value"), WEAKENINGS)
def test_user_cannot_weaken_any_field_and_the_error_names_it(field: str, value: Any) -> None:
    with pytest.raises(PermissionDenied) as caught:
        validate_override(strict_current(), PolicyPatch(**{field: value}))

    assert caught.value.code == "permission_denied"
    assert caught.value.details == {"field": field}
    assert field in caught.value.message


def test_every_policy_field_has_a_weakening_case() -> None:
    assert [case[0] for case in WEAKENINGS] == POLICY_FIELDS


@pytest.mark.req("FR-12")
def test_all_weakened_fields_are_named_together_and_strict_ones_are_not() -> None:
    change = PolicyPatch(max_leverage=Decimal(50), max_pending=1, slippage_pct=Decimal(3))

    with pytest.raises(PermissionDenied) as caught:
        validate_override(strict_current(), change)

    assert caught.value.details == {"field": "max_leverage,slippage_pct"}


@pytest.mark.req("FR-12")
def test_equal_or_stricter_values_are_accepted() -> None:
    current = strict_current()
    same = PolicyPatch(**{name: getattr(current, name) for name in POLICY_FIELDS})
    stricter = PolicyPatch(
        allowlist=frozenset({BTC}),
        max_order_notional_usdt=Decimal("0.5"),
        confirm_actions=ALL_ACTION_KINDS,
        confirm_above_notional_usdt=Decimal(10),
        unconfirmed_window_s=7200,
        max_pending=0,
    )

    assert weakened_fields(current, same) == []
    validate_override(current, stricter)


@pytest.mark.req("FR-12")
def test_admin_lowering_the_ceiling_below_the_users_setting_makes_the_ceiling_apply() -> None:
    user = PolicyPatch(max_order_notional_usdt=Decimal(500), allowlist=frozenset({BTC, ETH}))
    lowered = trading_ceiling(max_order_notional_usdt=Decimal(200), allowlist=frozenset({ETH}))

    effective = strictest(lowered, user, Environment.TESTNET)

    assert effective.max_order_notional_usdt == Decimal(200)
    assert effective.allowlist == frozenset({ETH})


# --- defaults (FR-18, FR-45) --------------------------------------------------------------


@pytest.mark.req("FR-45")
@pytest.mark.parametrize("environment", list(Environment))
def test_new_user_ceiling_is_read_only_with_an_empty_allowlist(environment: Environment) -> None:
    ceiling = default_ceiling(environment)

    assert ceiling.profile is Profile.READ_ONLY
    assert ceiling.allowlist == frozenset()
    assert granted_permissions(ceiling) == {Permission.READ}


@pytest.mark.req("FR-18", "FR-19", "FR-23", "FR-42")
def test_default_limits_per_environment() -> None:
    testnet, mainnet = default_ceiling(Environment.TESTNET), default_ceiling(Environment.MAINNET)

    assert (testnet.max_order_notional_usdt, testnet.max_leverage) == (Decimal(1000), Decimal(10))
    assert (mainnet.max_order_notional_usdt, mainnet.max_leverage) == (Decimal(50), Decimal(3))
    for ceiling in (testnet, mainnet):
        assert ceiling.confirm_actions == ALL_ACTION_KINDS  # FR-19: confirm all trading actions
        assert ceiling.confirm_above_notional_usdt is None
        assert ceiling.confirmation_ttl_s == 120  # FR-23: 2 minutes
        assert ceiling.prefer_external_channel is False
    assert testnet.trust_client_ui is True  # FR-42
    assert mainnet.trust_client_ui is False
    assert (testnet.max_pending, mainnet.max_pending) == (10, 5)  # FR-19
    assert (testnet.slippage_pct, mainnet.slippage_pct) == (Decimal(2), Decimal("0.5"))  # PLAN section 6
    assert (testnet.unconfirmed_max_actions, mainnet.unconfirmed_max_actions) == (20, 5)


@pytest.mark.req("FR-18")
def test_mainnet_defaults_are_at_least_as_strict_as_testnet_in_every_field() -> None:
    testnet, mainnet = default_ceiling(Environment.TESTNET), default_ceiling(Environment.MAINNET)
    as_patch = PolicyPatch(**{name: getattr(mainnet, name) for name in POLICY_FIELDS})

    assert weakened_fields(testnet, as_patch) == []
    assert weakened_fields(mainnet, PolicyPatch(**{n: getattr(testnet, n) for n in POLICY_FIELDS})) != []


# --- hard bounds and values ----------------------------------------------------------------


@pytest.mark.req("FR-23")
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("slippage_pct", Decimal("3.01")),  # PLAN: hard maximum 3%
        ("slippage_pct", Decimal(-1)),
        ("confirmation_ttl_s", 601),  # FR-23: never more than 10 minutes
        ("confirmation_ttl_s", 0),
        ("max_leverage", Decimal("0.5")),
        ("max_order_notional_usdt", Decimal(-1)),
        ("max_order_notional_usdt", Decimal("NaN")),
        ("max_order_notional_usdt", 1000.0),  # float is never money
        ("confirm_above_notional_usdt", Decimal(-5)),
        ("max_pending", -1),
        ("max_pending", True),  # bool is not an int here
        ("unconfirmed_window_s", 0),
        ("trust_client_ui", 1),
        ("profile", "trade"),
        ("allowlist", frozenset({"linear:BTCUSDT"})),
        ("confirm_actions", frozenset({"place_order"})),
    ],
)
def test_values_outside_hard_bounds_are_invalid_params_naming_the_field(field: str, value: Any) -> None:
    with pytest.raises(InvalidParams) as in_ceiling:
        trading_ceiling(**{field: value})
    with pytest.raises(InvalidParams) as in_patch:
        PolicyPatch(**{field: value})

    assert in_ceiling.value.details == {"field": field}
    assert in_patch.value.details == {"field": field}


def test_admin_may_raise_the_ceiling_up_to_the_hard_bounds() -> None:
    raised = apply_patch(
        default_ceiling(Environment.TESTNET),
        PolicyPatch(confirmation_ttl_s=600, slippage_pct=Decimal(3), max_leverage=Decimal(50)),
    )

    assert (raised.confirmation_ttl_s, raised.slippage_pct) == (600, Decimal(3))
    assert raised.max_leverage == Decimal(50)
    assert isinstance(raised, AdminCeiling)


def test_patch_reports_only_the_fields_it_sets() -> None:
    patch = PolicyPatch(max_pending=3, confirm_above_notional_usdt=None)

    assert patch.changes() == {"confirm_above_notional_usdt": None, "max_pending": 3}
    assert not patch.is_empty
    assert PolicyPatch().is_empty
    assert PolicyPatch().max_pending is UNSET


def test_apply_patch_merges_overrides() -> None:
    merged = apply_patch(PolicyPatch(max_pending=3), PolicyPatch(max_leverage=Decimal(2)))

    assert merged.changes() == {"max_leverage": Decimal(2), "max_pending": 3}


# --- serialisation ------------------------------------------------------------------------


@pytest.mark.req("FR-36")
def test_settings_and_patches_survive_canonical_json_exactly() -> None:
    ceiling = trading_ceiling(
        max_order_notional_usdt=Decimal("1234.5678901234567890123456789"),
        confirm_above_notional_usdt=Decimal("99.9"),
        confirm_actions=frozenset({PLACE, LEVERAGE}),
    )
    patch = PolicyPatch(
        allowlist=frozenset({SOL}), confirm_above_notional_usdt=None, max_leverage=Decimal("2.5")
    )

    stored_ceiling = json.loads(canonical_json(settings_to_payload(ceiling)))
    stored_patch = json.loads(canonical_json(patch_to_payload(patch)))

    assert ceiling_from_payload(stored_ceiling) == ceiling
    assert patch_from_payload(stored_patch) == patch
    assert stored_ceiling["allowlist"] == ["linear:BTCUSDT", "linear:ETHUSDT"]
    assert stored_ceiling["max_order_notional_usdt"] == "1234.5678901234567890123456789"
    assert stored_patch == {
        "allowlist": ["spot:SOLUSDT"],
        "confirm_above_notional_usdt": None,
        "max_leverage": "2.5",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"max_pending": 3, "surprise": 1},
        {"max_leverage": 2.5},  # float in storage
        {"max_leverage": "abc"},
    ],
)
def test_corrupt_stored_patch_is_refused(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"policy|decimal"):
        patch_from_payload(payload)


def test_stored_ceiling_must_be_complete() -> None:
    payload = settings_to_payload(trading_ceiling())
    del payload["max_pending"]

    with pytest.raises(ValueError, match="max_pending"):
        ceiling_from_payload(payload)


# --- permissions (FR-11) ------------------------------------------------------------------


@pytest.mark.req("FR-11")
def test_profiles_grant_permissions() -> None:
    read_only = strictest(default_ceiling(Environment.TESTNET), NO_OVERRIDES, Environment.TESTNET)
    trader = strictest(trading_ceiling(), NO_OVERRIDES, Environment.TESTNET)
    tools = {"get_ticker": Permission.READ, "place_order": Permission.TRADE}

    assert permitted(read_only, tools) == {"get_ticker"}
    assert permitted(trader, tools) == {"get_ticker", "place_order"}
    assert permitted(None, tools) == frozenset()
    require_permission(trader, Permission.TRADE)
    require_permission(read_only, Permission.READ)
    with pytest.raises(PermissionDenied) as caught:
        require_permission(read_only, Permission.TRADE)
    assert caught.value.details == {"profile": "read_only", "required": "trade"}


@pytest.mark.req("FR-11", "FR-12")
def test_user_choosing_read_only_loses_trading_even_under_a_trade_ceiling() -> None:
    effective = strictest(trading_ceiling(), PolicyPatch(profile=Profile.READ_ONLY), Environment.TESTNET)

    assert granted_permissions(effective) == {Permission.READ}


# --- parsing text input -------------------------------------------------------------------


def test_instrument_refs_parse_and_print_as_category_symbol() -> None:
    assert parse_instrument_ref(" Linear:btcusdt ") == BTC
    assert str(SOL) == "spot:SOLUSDT"


@pytest.mark.parametrize("raw", ["BTCUSDT", "inverse:BTCUSD", "linear:", "linear:BTC-USDT"])
def test_bad_instrument_refs_are_invalid_params(raw: str) -> None:
    with pytest.raises(InvalidParams) as caught:
        parse_instrument_ref(raw)
    assert caught.value.details == {"field": "allowlist"}


def test_action_kinds_and_decimals_parse_from_text() -> None:
    assert parse_action_kinds(["all"]) == ALL_ACTION_KINDS
    assert parse_action_kinds(["none"]) == frozenset()
    assert parse_action_kinds(["place_order", "CANCEL_ORDER"]) == {PLACE, CANCEL}
    assert parse_decimal("max_leverage", " 2.50 ") == Decimal("2.50")
    with pytest.raises(InvalidParams):
        parse_action_kinds(["withdraw"])
    for bad in ("abc", "inf", "NaN"):
        with pytest.raises(InvalidParams):
            parse_decimal("max_leverage", bad)
