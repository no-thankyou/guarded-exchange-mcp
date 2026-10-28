"""Domain rules for exchange keys: the D-24 allowlist, read-only, expiry, FR-49 "cannot verify"."""

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from guarded_exchange.domain.audit import JsonValue, canonical_json
from guarded_exchange.domain.credentials import (
    EXCHANGE_BYBIT,
    PERMISSION_ALLOWLIST,
    WARNING_NO_IP_WHITELIST,
    ApiKeyPair,
    Credential,
    CredentialStatus,
    KeyPermissions,
    assess_key,
    cap_profile,
    permissions_from_payload,
    permissions_to_payload,
    require_trading_allowed,
    unverifiable_key,
)
from guarded_exchange.domain.errors import (
    CredentialsPermissionsChanged,
    ExchangeCredentialsInvalid,
    InvalidParams,
    PermissionDenied,
)
from guarded_exchange.domain.policy import Profile
from guarded_exchange.domain.users import Environment

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
TRADING = frozenset({"ContractTrade.Order", "ContractTrade.Position", "Spot.SpotTrade"})


def perms(
    names: frozenset[str] = TRADING,
    *,
    read_only: bool = False,
    expires_at: datetime | None = None,
    ip_restricted: bool = True,
) -> KeyPermissions:
    return KeyPermissions(
        read_only=read_only, permissions=names, expires_at=expires_at, ip_restricted=ip_restricted
    )


@pytest.mark.req("FR-49", "NFR-1")
def test_permission_allowlist_is_exactly_d24() -> None:
    """The allowlist is one explicit table (PLAN D-24). Adding a permission - e.g. the open question
    ``Derivatives.DerivativesTrade`` - is one line in ``domain/credentials.py`` plus this test."""
    assert {
        "bybit": frozenset({"ContractTrade.Order", "ContractTrade.Position", "Spot.SpotTrade"})
    } == PERMISSION_ALLOWLIST


@pytest.mark.req("FR-49")
def test_a_key_with_only_allowlisted_permissions_is_accepted_for_trading() -> None:
    assessment = assess_key(EXCHANGE_BYBIT, perms(), NOW)

    assert assessment.profile is Profile.TRADE
    assert assessment.warnings == ()


@pytest.mark.req("FR-49", "NFR-1")
@pytest.mark.parametrize(
    "extra",
    [
        "Wallet.AccountTransfer",  # transfers (spike 004)
        "Wallet.SubMemberTransferList",
        "Wallet.Withdraw",
        "Options.OptionsTrade",
        "Derivatives.DerivativesTrade",  # open question: not allowed until the user decides
        "Exchange.ExchangeHistory",  # convert
        "BrandNewGroup.SomethingBybitAddsTomorrow",  # unknown -> refused (allowlist, not denylist)
        "<unrecognised>",  # a name the adapter could not even parse
    ],
)
def test_any_permission_outside_the_allowlist_rejects_the_key(extra: str) -> None:
    with pytest.raises(ExchangeCredentialsInvalid) as err:
        assess_key(EXCHANGE_BYBIT, perms(TRADING | {extra}), NOW)

    assert err.value.details == {"reason": "permissions_not_allowed", "permissions": extra}
    assert extra in err.value.message


@pytest.mark.req("FR-49")
def test_the_spike_004_demo_key_is_rejected_and_every_extra_permission_is_named() -> None:
    demo_key = TRADING | {
        "Wallet.AccountTransfer",
        "Wallet.SubMemberTransferList",
        "Options.OptionsTrade",
        "Derivatives.DerivativesTrade",
        "Exchange.ExchangeHistory",
    }

    with pytest.raises(ExchangeCredentialsInvalid) as err:
        assess_key(EXCHANGE_BYBIT, perms(demo_key), NOW)

    assert err.value.details["permissions"] == (
        "Derivatives.DerivativesTrade,Exchange.ExchangeHistory,Options.OptionsTrade,"
        "Wallet.AccountTransfer,Wallet.SubMemberTransferList"
    )


@pytest.mark.req("FR-49")
def test_a_read_only_key_is_accepted_and_limits_the_profile() -> None:
    assessment = assess_key(EXCHANGE_BYBIT, perms(read_only=True), NOW)

    assert assessment.profile is Profile.READ_ONLY


@pytest.mark.req("FR-49", "NFR-1")
def test_read_only_does_not_exempt_a_key_from_the_allowlist() -> None:
    with pytest.raises(ExchangeCredentialsInvalid):
        assess_key(EXCHANGE_BYBIT, perms(TRADING | {"Wallet.AccountTransfer"}, read_only=True), NOW)


@pytest.mark.req("FR-49")
def test_a_key_of_an_unknown_exchange_has_no_allowed_permission() -> None:
    with pytest.raises(ExchangeCredentialsInvalid):
        assess_key("other-exchange", perms(), NOW)


@pytest.mark.req("FR-49")
def test_an_expired_key_is_rejected_and_a_future_expiry_is_fine() -> None:
    with pytest.raises(ExchangeCredentialsInvalid, match="expired"):
        assess_key(EXCHANGE_BYBIT, perms(expires_at=NOW), NOW)

    assert assess_key(EXCHANGE_BYBIT, perms(expires_at=NOW + timedelta(days=1)), NOW).profile is Profile.TRADE


@pytest.mark.req("FR-49")
def test_a_key_without_ip_whitelist_is_accepted_with_a_warning() -> None:
    assert assess_key(EXCHANGE_BYBIT, perms(ip_restricted=False), NOW).warnings == (WARNING_NO_IP_WHITELIST,)


@pytest.mark.req("FR-49")
@pytest.mark.parametrize("attested", [False, True])
def test_unverifiable_permissions_on_mainnet_are_always_refused(attested: bool) -> None:
    with pytest.raises(ExchangeCredentialsInvalid, match="mainnet"):
        unverifiable_key(Environment.MAINNET, attested=attested)


@pytest.mark.req("FR-49")
def test_unverifiable_permissions_on_testnet_need_the_admins_attestation() -> None:
    with pytest.raises(ExchangeCredentialsInvalid, match="cannot be checked") as err:
        unverifiable_key(Environment.TESTNET, attested=False)
    assert "--admin-attest" in (err.value.next_step or "")

    accepted = unverifiable_key(Environment.TESTNET, attested=True)
    assert accepted.profile is Profile.TRADE
    assert accepted.warnings == ("permissions_not_verified",)


def _credential(permissions: KeyPermissions | None, **changes: object) -> Credential:
    base = Credential(
        id="c-1",
        user_id="u-1",
        exchange=EXCHANGE_BYBIT,
        environment=Environment.TESTNET,
        status=CredentialStatus.ACTIVE,
        permissions=permissions,
        permissions_checked_at=NOW,
        admin_attested=permissions is None,
        created_at=NOW,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.mark.req("FR-49")
def test_trading_is_blocked_when_the_stored_permissions_left_the_allowlist() -> None:
    grown = _credential(perms(TRADING | {"Wallet.AccountTransfer"}))

    with pytest.raises(CredentialsPermissionsChanged) as err:
        require_trading_allowed(grown, NOW)

    assert err.value.code == "credentials_permissions_changed"
    assert err.value.details == {"permissions": "Wallet.AccountTransfer"}


@pytest.mark.req("FR-49")
def test_trading_needs_a_valid_read_write_key() -> None:
    require_trading_allowed(_credential(perms()), NOW)
    require_trading_allowed(_credential(None), NOW)  # admin attested, never verified
    with pytest.raises(PermissionDenied, match="read-only"):
        require_trading_allowed(_credential(perms(read_only=True)), NOW)
    with pytest.raises(ExchangeCredentialsInvalid, match="expired"):
        require_trading_allowed(_credential(perms(expires_at=NOW - timedelta(seconds=1))), NOW)


@pytest.mark.req("FR-49")
def test_a_read_only_key_caps_the_profile() -> None:
    assert cap_profile(Profile.TRADE, _credential(perms(read_only=True))) is Profile.READ_ONLY
    assert cap_profile(Profile.TRADE, _credential(perms())) is Profile.TRADE
    assert cap_profile(Profile.TRADE, None) is Profile.TRADE
    assert cap_profile(Profile.READ_ONLY, _credential(perms())) is Profile.READ_ONLY


def test_recheck_is_due_after_an_hour_or_when_never_checked() -> None:
    credential = _credential(perms())

    assert not credential.needs_recheck(NOW + timedelta(minutes=59, seconds=59))
    assert credential.needs_recheck(NOW + timedelta(hours=1))
    assert _credential(None, permissions_checked_at=None).needs_recheck(NOW)


def test_permissions_survive_the_storage_payload() -> None:
    original = perms(
        read_only=True, expires_at=datetime(2027, 1, 29, 6, 13, 6, tzinfo=UTC), ip_restricted=False
    )

    assert permissions_from_payload(_json_round_trip(permissions_to_payload(original))) == original
    assert permissions_to_payload(None) is None


def _json_round_trip(payload: Mapping[str, JsonValue] | None) -> dict[str, Any]:
    assert payload is not None
    result: dict[str, Any] = json.loads(canonical_json(payload))
    return result


@pytest.mark.req("FR-31")
def test_a_key_pair_never_shows_up_in_repr() -> None:
    keys = ApiKeyPair(api_key="KEYID0123456789", api_secret="very-secret-value-0123456789")  # noqa: S106 - test value

    assert "KEYID" not in repr(keys)
    assert "very-secret" not in repr(keys)
    assert "very-secret" not in str(keys)


@pytest.mark.req("FR-31")
@pytest.mark.parametrize(
    ("api_key", "api_secret", "field"),
    [
        ("short", "long-enough-secret-123", "api_key"),
        ("LONGENOUGHKEY123", "has a space inside it", "api_secret"),
        ("LONGENOUGHKEY123", "", "api_secret"),
        ("KEY\nWITHNEWLINE", "long-enough-secret-123", "api_key"),
    ],
)
def test_malformed_key_input_is_refused_without_quoting_it(api_key: str, api_secret: str, field: str) -> None:
    with pytest.raises(InvalidParams) as err:
        ApiKeyPair(api_key=api_key, api_secret=api_secret)

    assert err.value.details == {"field": field}
    assert api_secret not in err.value.message or not api_secret
    assert "has a space" not in err.value.message


@pytest.mark.req("FR-29", "FR-49")
def test_a_deleted_key_never_passes_the_trading_check() -> None:
    with pytest.raises(ExchangeCredentialsInvalid) as err:
        require_trading_allowed(_credential(perms(), status=CredentialStatus.DELETED, deleted_at=NOW), NOW)

    assert err.value.details == {"reason": "key_replaced_or_deleted"}


@pytest.mark.req("FR-49", "NFR-1")
@pytest.mark.parametrize(
    "changes",
    [
        {"admin_attested": False},  # unverified and not attested: should not exist, refuse anyway
        {"admin_attested": True, "environment": Environment.MAINNET},  # attestation is testnet-only
    ],
    ids=["not-attested", "attested-on-mainnet"],
)
def test_unverified_permissions_pass_only_for_an_attested_testnet_key(changes: dict[str, object]) -> None:
    with pytest.raises(ExchangeCredentialsInvalid) as err:
        require_trading_allowed(_credential(None, **changes), NOW)

    assert err.value.details == {"reason": "permissions_unverified"}
