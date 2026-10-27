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
