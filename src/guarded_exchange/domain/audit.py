"""Audit journal events and their canonical JSON form (SPEC FR-36, FR-37; PLAN ADR-008, D-13).

The journal is append-only. Records are grouped into chains: one per user (``chain_id`` =
user id) and a ``system`` chain for installation and administrative events that have no
owner. Within a chain ``seq`` is gapless and starts at 1. Until the HMAC chain (ticket 024)
records carry no MAC; the canonical JSON defined here is what the MAC will be computed over.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

SYSTEM_CHAIN: Final = "system"

type JsonValue = str | int | bool | Decimal | datetime | Sequence[JsonValue] | Mapping[str, JsonValue] | None


class AuditEventType(StrEnum):
    INSTALLATION_INITIALIZED = "installation_initialized"
    USER_CREATED = "user_created"
    POLICY_CEILING_SET = "policy_ceiling_set"
    POLICY_TIGHTENED = "policy_tightened"
    POLICY_OVERRIDES_RESET = "policy_overrides_reset"
    ACCESS_TOKEN_ISSUED = "access_token_issued"  # noqa: S105 - event names, not secrets
    ACCESS_TOKEN_REVOKED = "access_token_revoked"  # noqa: S105
    CREDENTIAL_ADDED = "credential_added"
    CREDENTIAL_REPLACED = "credential_replaced"
    CREDENTIAL_DELETED = "credential_deleted"
    CREDENTIAL_REJECTED = "credential_rejected"
    CREDENTIAL_PERMISSIONS_CHANGED = "credential_permissions_changed"
    KILL_SWITCH_ON = "kill_switch_on"
    """The kill switch was turned on (by the agent, the user or the admin, FR-16)."""
    KILL_SWITCH_OFF = "kill_switch_off"
    """The kill switch was turned off (CLI only, FR-44)."""
    ORDER_REFUSED = "order_refused"
    """The policy refused a trading action before anything was sent (FR-17, FR-25)."""
    ORDER_ATTEMPT = "order_attempt"
    """Written and committed before the request leaves for the exchange (ADR-008)."""
    ORDER_OUTCOME = "order_outcome"
    """What the exchange answered: accepted, rejected or unknown (FR-27)."""
    ORDER_REPEAT = "order_repeat"
    """A repeated call answered from its idempotency record; nothing was sent (FR-26, FR-36)."""
    CONFIRMATION_REQUESTED = "confirmation_requested"
    """An action that needs a human's confirmation was parked as a pending confirmation (FR-47)."""
    CONFIRMATION_DECIDED = "confirmation_decided"
    """The pending confirmation left ``pending``/``approved`` without being executed or with a human's
    decision: approved, rejected, expired, cancelled, invalidated (FR-24, FR-29, FR-42: with its channel)."""


_CLIENT_TEXT_MAX: Final = 64


@dataclass(frozen=True, slots=True)
class ClientDescription:
    """What the AI client said about itself (MCP ``clientInfo``): recorded, never verified (FR-36).

    Free text from outside: only printable ASCII is kept and it is cut short; it goes to the
    journal, never into a message or a decision.
    """

    name: str
    version: str

    @classmethod
    def sanitized(cls, name: str, version: str) -> "ClientDescription":
        return cls(_printable(name), _printable(version))

    def to_payload(self) -> dict[str, JsonValue]:
        return {"name": self.name, "version": self.version}


def _printable(raw: str) -> str:
    return "".join(ch if " " <= ch <= "~" else "?" for ch in raw[:_CLIENT_TEXT_MAX])


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Something worth recording: what happened, who did it, and the parameters."""

    event_type: str
    actor: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """A stored event. ``payload`` is the decoded canonical JSON (decimals and times as strings)."""

    chain_id: str
    seq: int
    ts: datetime
    event_type: str
    actor: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


def canonical_json(payload: Mapping[str, JsonValue]) -> str:
    """Serialise deterministically: sorted keys, no whitespace, UTF-8, no floats.

    ``Decimal`` becomes a plain string without exponent or trailing zeros (``0.10`` -> ``"0.1"``),
    ``datetime`` must be timezone-aware and becomes ISO-8601 UTC with milliseconds. ``float``,
    NaN/Infinity and non-string keys are rejected: they have no single canonical text.
    """
    return json.dumps(
        _to_json(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError(f"non-finite decimal is not allowed: {value}")
    if value.is_zero():
        return "0"
    # Not ``normalize()``: it rounds to the context precision (28 digits) and loses data.
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def canonical_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed; use timezone-aware UTC")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _to_json(value: object) -> object:
    match value:
        case None | bool() | int() | str():
            return value
        case bytes() | bytearray():
            raise TypeError("bytes are not allowed in audit payloads; encode them as text first")
        case float():
            raise TypeError("float is not allowed in audit payloads; use Decimal")
        case Decimal():
            return canonical_decimal(value)
        case datetime():
            return canonical_time(value)
        case Mapping():
            out: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"payload keys must be strings, got {type(key).__name__}")
                out[key] = _to_json(item)
            return out
        case Sequence():
            return [_to_json(item) for item in value]
    raise TypeError(f"unsupported payload value type: {type(value).__name__}")
