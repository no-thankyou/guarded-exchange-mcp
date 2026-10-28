"""Exchange API keys and the rules for accepting them (SPEC FR-30, FR-31, FR-49, NFR-1; PLAN D-9, D-12,
D-24, ADR-009).

A key is accepted only if every permission the exchange reports for it is on an explicit
allowlist (``PERMISSION_ALLOWLIST``): anything else, including a permission nobody has heard of,
rejects it. A read-only key is accepted and caps the user's profile at ``read_only``. If the
permissions cannot be checked at all, the key is rejected, except on the test environment after
an explicit attestation by the admin (FR-49, GP-3). Permissions are re-checked periodically
(``RECHECK_INTERVAL``); a key whose permissions grew beyond the allowlist blocks trading.

The secret half of a key never leaves ``ApiKeyPair`` in clear text: it is excluded from ``repr``
and only the cipher adapter ever reads it.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from guarded_exchange.domain.audit import JsonValue
from guarded_exchange.domain.errors import (
    CredentialsPermissionsChanged,
    ExchangeCredentialsInvalid,
    InvalidParams,
    PermissionDenied,
)
from guarded_exchange.domain.policy import Profile
from guarded_exchange.domain.users import Environment

EXCHANGE_BYBIT: Final = "bybit"

PERMISSION_ALLOWLIST: Final = MappingProxyType(
    {
        EXCHANGE_BYBIT: frozenset(
            {
                # PLAN D-24: derivatives orders and positions (USDT perpetuals), spot trading.
                "ContractTrade.Order",
                "ContractTrade.Position",
                "Spot.SpotTrade",
                # Open question (PLAN "Spike 004"): whether linear trading on a unified account
                # needs "Derivatives.DerivativesTrade". Adding it is the user's decision; if so,
                # it is this one line.
            }
        ),
    }
)
"""Exchange -> the only permissions (``"Group.Name"``) a stored key may carry (D-24, NFR-1).

Not a denylist: withdrawals, transfers, options, convert, and every permission the exchange may
add in the future are refused because they are not listed here.
"""

RECHECK_INTERVAL: Final = timedelta(hours=1)
"""Permissions older than this are re-checked before a state-changing action (PLAN D-24)."""

WARNING_NO_IP_WHITELIST: Final = "no_ip_whitelist"

_KEY_PART_RE: Final = re.compile(r"^[\x21-\x7e]{8,256}$")  # printable ASCII, no spaces


class CredentialUnreadable(ExchangeCredentialsInvalid):
    """A stored key does not decrypt: wrong master key, or the row was copied or tampered with."""

    def __init__(self) -> None:
        super().__init__(
            "the stored exchange key cannot be decrypted",
            next_step="Tell the user to replace the key (`guarded-exchange keys replace`); if every key "
            "fails, the master key file is not the one these keys were stored with.",
            details={"reason": "undecryptable"},
        )


class CredentialStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"  # soft delete (D-9): the row stays for references from pending actions


@dataclass(frozen=True, slots=True)
class ApiKeyPair:
    """An exchange API key and its secret, as typed by the user. Never shown, logged or journaled.

    Both halves are excluded from ``repr``: the key id is not a secret by itself, but FR-31 says a
    stored key is never displayed again, so neither half is printed by accident.
    """

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        # The message never quotes the value: it may be a pasted secret.
        for name, value in (("api_key", self.api_key), ("api_secret", self.api_secret)):
            if not _KEY_PART_RE.fullmatch(value):
                raise InvalidParams(
                    f"{name} must be 8-256 printable characters without spaces",
                    next_step="Copy the key and the secret again from the exchange's API management page.",
                    details={"field": name},
                )


@dataclass(frozen=True, slots=True)
class CredentialBinding:
    """What a stored secret is bound to (ADR-009): the AAD of its encryption.

    A ciphertext copied into another credential's row (another user, another exchange or
    environment, D-12) does not decrypt, so agent B can never trade on user A's key.
    """

    credential_id: str
    user_id: str
    exchange: str
    environment: Environment


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """An encrypted ``ApiKeyPair`` as stored: AES-256-GCM ciphertext, its nonce and key version."""

    ciphertext: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    key_version: int


@dataclass(frozen=True, slots=True)
class KeyPermissions:
    """What the exchange reports about a key, in exchange-neutral form.

    ``permissions`` holds ``"Group.Name"`` strings of non-empty groups only (an empty group means
    no permission, spike 004). ``expires_at`` ``None`` means the key does not expire.
    """

    read_only: bool
    permissions: frozenset[str]
    expires_at: datetime | None
    ip_restricted: bool


@dataclass(frozen=True, slots=True)
class KeyAssessment:
    """An accepted key: the profile it allows and warnings worth showing to the user."""

    profile: Profile
    warnings: tuple[str, ...]


def disallowed_permissions(exchange: str, permissions: KeyPermissions) -> list[str]:
    """Permissions outside the allowlist, sorted. Unknown exchange -> every permission."""
    allowed = PERMISSION_ALLOWLIST.get(exchange, frozenset())
    return sorted(permissions.permissions - allowed)


def assess_key(exchange: str, permissions: KeyPermissions, now: datetime) -> KeyAssessment:
    """Accept or reject a key by its reported permissions (FR-49, D-24).

    Raises ``ExchangeCredentialsInvalid`` for a permission outside the allowlist or an expired
    key. ``readOnly`` does not exempt a key from the allowlist: anything unexpected is refused.
    """
    extra = disallowed_permissions(exchange, permissions)
    if extra:
        raise ExchangeCredentialsInvalid(
            f"the key has permissions this server does not accept: {', '.join(extra)}",
            next_step="Create a key with only Contract Orders/Positions and Spot Trade (or read-only), "
            "without withdrawal, transfer, options or convert permissions, and add it again.",
            details={"reason": "permissions_not_allowed", "permissions": ",".join(extra)},
        )
    if permissions.expires_at is not None and permissions.expires_at <= now:
        raise ExchangeCredentialsInvalid(
            "the key has expired",
            next_step="Create a new key on the exchange and replace this one.",
            details={"reason": "expired"},
        )
    warnings = () if permissions.ip_restricted else (WARNING_NO_IP_WHITELIST,)
    return KeyAssessment(
        profile=Profile.READ_ONLY if permissions.read_only else Profile.TRADE, warnings=warnings
    )


def unverifiable_key(environment: Environment, *, attested: bool) -> KeyAssessment:
    """Decide on a key whose permissions cannot be checked (FR-49, GP-3).

    Mainnet: always refused. Test environment: accepted only if the admin attested in the service
    channel that the key has no withdrawal or transfer permission.
    """
    if environment is Environment.MAINNET:
        raise ExchangeCredentialsInvalid(
            "the key's permissions cannot be checked on the exchange right now; on mainnet such a key "
            "is never accepted",
            next_step="Try again later; if it keeps failing, check the exchange status.",
            details={"reason": "permissions_unverifiable"},
        )
    if not attested:
        raise ExchangeCredentialsInvalid(
            "the key's permissions cannot be checked on the exchange right now",
            next_step="Try again later. On the test environment the admin may accept it with "
            "--admin-attest after confirming the key has no withdrawal or transfer permission.",
            details={"reason": "permissions_unverifiable"},
        )
    return KeyAssessment(profile=Profile.TRADE, warnings=("permissions_not_verified",))


@dataclass(frozen=True, slots=True)
class Credential:
    """A stored exchange key (without its secret). One active per user, exchange and environment (D-12).

    ``permissions`` is ``None`` only for a key accepted on admin attestation that has never been
    checked successfully since.
    """

    id: str
    user_id: str
    exchange: str
    environment: Environment
    status: CredentialStatus
    permissions: KeyPermissions | None
    permissions_checked_at: datetime | None
    admin_attested: bool
    created_at: datetime
    deleted_at: datetime | None = None

    @property
    def binding(self) -> CredentialBinding:
        return CredentialBinding(
            credential_id=self.id, user_id=self.user_id, exchange=self.exchange, environment=self.environment
        )

    @property
    def is_active(self) -> bool:
        return self.status is CredentialStatus.ACTIVE

    @property
    def read_only(self) -> bool:
        return self.permissions is not None and self.permissions.read_only

    def needs_recheck(self, now: datetime, interval: timedelta = RECHECK_INTERVAL) -> bool:
        checked = self.permissions_checked_at
        return checked is None or now - checked >= interval


def require_trading_allowed(credential: Credential, now: datetime) -> None:
    """Raise unless the key may be used for a state-changing action right now (FR-49).

    Permissions grown beyond the allowlist -> ``CredentialsPermissionsChanged``; expired ->
    ``ExchangeCredentialsInvalid``; read-only -> ``PermissionDenied``; deleted or replaced ->
    ``ExchangeCredentialsInvalid``. A key accepted on the admin's attestation on the test environment
    and never verified since may trade (that is what the admin attested); unverified otherwise -> refused.
    """
    if not credential.is_active:
        raise key_replaced_or_deleted()
    if credential.permissions is None:
        if credential.admin_attested and credential.environment is Environment.TESTNET:
            return
        raise ExchangeCredentialsInvalid(
            "the exchange key's permissions were never verified",
            next_step="Tell the user to replace the key (`guarded-exchange keys replace`), outside the chat.",
            details={"reason": "permissions_unverified"},
        )
    extra = disallowed_permissions(credential.exchange, credential.permissions)
    if extra:
        raise CredentialsPermissionsChanged(
            f"the exchange key now has permissions this server does not accept: {', '.join(extra)}; "
            "state-changing actions are blocked",
            next_step="Do not retry. Tell the user to remove these permissions on the exchange or replace "
            "the key (`guarded-exchange keys replace`), outside the chat.",
            details={"permissions": ",".join(extra)},
        )
    if credential.permissions.expires_at is not None and credential.permissions.expires_at <= now:
        raise ExchangeCredentialsInvalid(
            "the exchange key has expired",
            next_step="Tell the user to create a new key and run `guarded-exchange keys replace`.",
            details={"reason": "expired"},
        )
    if credential.permissions.read_only:
        raise PermissionDenied(
            "the exchange key is read-only; trading is not possible with it",
            next_step="Do not retry. Tell the user; a trading key can be added outside the chat.",
            details={"reason": "read_only_key"},
        )


def key_replaced_or_deleted() -> ExchangeCredentialsInvalid:
    return ExchangeCredentialsInvalid(
        "the exchange key was replaced or deleted",
        next_step="Retry once: the action will use the user's current key, if there is one.",
        details={"reason": "key_replaced_or_deleted"},
    )


def cap_profile(profile: Profile, credential: Credential | None) -> Profile:
    """A read-only key limits the profile to ``read_only`` (FR-49); otherwise the profile stands."""
    return Profile.READ_ONLY if credential is not None and credential.read_only else profile


def permissions_to_payload(permissions: KeyPermissions | None) -> dict[str, JsonValue] | None:
    """Journal and storage form: names only, never the key or its secret."""
    if permissions is None:
        return None
    return {
        "read_only": permissions.read_only,
        "permissions": sorted(permissions.permissions),
        "expires_at": permissions.expires_at,
        "ip_restricted": permissions.ip_restricted,
    }


def permissions_from_payload(data: Mapping[str, Any]) -> KeyPermissions:
    """Inverse of ``permissions_to_payload`` (as decoded from canonical JSON)."""
    expires_raw = data["expires_at"]
    expires_at = None if expires_raw is None else datetime.fromisoformat(str(expires_raw))
    if expires_at is not None and expires_at.tzinfo is None:
        raise ValueError("stored expires_at has no time zone")
    return KeyPermissions(
        read_only=bool(data["read_only"]),
        permissions=frozenset(str(p) for p in data["permissions"]),
        expires_at=expires_at,
        ip_restricted=bool(data["ip_restricted"]),
    )
