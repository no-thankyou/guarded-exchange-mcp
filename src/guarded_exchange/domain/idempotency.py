"""Idempotency of state-changing calls (SPEC FR-26, FR-27; PLAN ADR-014, D-19, D-20).

A call claims one record per ``(user, kind, key)`` before it does anything with the exchange:

- ``explicit``: the agent's idempotency key, kept ``EXPLICIT_KEY_TTL`` (24 h). The same key with the
  same arguments returns the first answer; with other arguments it is ``idempotency_conflict``.
- ``fingerprint``: no key; the key is the hash of the canonical call (``request_hash``). Only for
  risk-increasing actions (D-20 (3)): repeating a cancel or a reduce-only order must keep working.
  An identical call within ``FINGERPRINT_WINDOW`` (30 s) after the previous one answered is a repeat.

Lifecycle (``RecordStatus``): ``in_progress`` (claimed, nothing sent) -> ``sent`` (the decision allowed
it, the attempt is journaled: from here on an order may exist) -> ``completed`` (``result`` holds the
final answer: the accepted order or a definite refusal) or ``unknown`` (no reliable answer and the
order was not found). A call that ends with nothing decided and nothing created releases (deletes) its
record (``keeps_record``), so retrying an unavailable exchange is not answered from storage.

A record can be taken over by a new call only once it has expired, or while still ``in_progress``
(nothing sent) after ``IN_FLIGHT_GUARD``. While a call is in flight its record does not expire before
``IN_FLIGHT_GUARD``, and moving it to ``sent`` extends it by that much; a call whose record expired or
was taken over (it was stuck before its decision) finds out in its decision transaction and sends
nothing. A call with an explicit key also claims the fingerprint of the call when it is free, so a
keyless repeat from another client is a repeat too; only keyless calls consult fingerprints.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from guarded_exchange.domain.audit import JsonValue, canonical_json
from guarded_exchange.domain.errors import InvalidParams
from guarded_exchange.domain.risk import RiskDirection
from guarded_exchange.domain.users import Environment

IDEMPOTENCY_KEY_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"
"""What an agent may use as a key: 1-64 ASCII letters, digits and ``_ . : -`` (e.g. a UUID)."""

EXPLICIT_KEY_TTL: Final = timedelta(hours=24)
FINGERPRINT_WINDOW: Final = timedelta(seconds=30)
IN_FLIGHT_GUARD: Final = timedelta(minutes=10)
"""Longer than any call can take after its decision (the send has an overall deadline and the
reconnaissance a bound); before the decision a takeover is fenced (``RecordStatus.IN_PROGRESS``)."""

_KEY_RE: Final = re.compile(IDEMPOTENCY_KEY_PATTERN)


class KeyKind(StrEnum):
    EXPLICIT = "explicit"
    FINGERPRINT = "fingerprint"


class RecordStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    SENT = "sent"
    UNKNOWN = "unknown"
    COMPLETED = "completed"


class Outcome(StrEnum):
    """How a call ended, for deciding whether its record is kept (``keeps_record``)."""

    ACCEPTED = "accepted"
    """The exchange has the order."""
    UNKNOWN = "unknown"
    """The exchange may have the order."""
    REFUSED = "refused"
    """A deliberate, definite answer: the policy refused it or the exchange rejected it; no order."""
    TRANSIENT = "transient"
    """Nothing was decided or created and trying again may work (unavailable, rate limited, input)."""


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """One claimed call. ``action_id`` identifies the call holding it (and its client order id, D-19)."""

    user_id: str
    kind: KeyKind
    key: str
    request_hash: str
    action_id: str
    status: RecordStatus
    created_at: datetime
    expires_at: datetime
    result: Mapping[str, Any] | None = None
    """The stored answer (``completed`` and ``unknown``), canonical JSON decoded."""
    sent_at: datetime | None = None
    """When the decision moved it to ``sent`` (the call's send started then)."""

    def held_by(self, action_id: str) -> bool:
        return self.action_id == action_id


def parse_idempotency_key(raw: str) -> str:
    """The key as given (exact, case-sensitive) or ``InvalidParams``; the message never quotes it."""
    if not _KEY_RE.fullmatch(raw):
        raise InvalidParams(
            "idempotency_key must be 1-64 ASCII letters, digits or _ . : - (e.g. a UUID)",
            next_step="Use a fresh UUID per new action, and the same one only to repeat the same call.",
            details={"field": "idempotency_key"},
        )
    return raw


def request_hash(tool: str, args: Mapping[str, JsonValue], environment: Environment) -> str:
    """SHA-256 of the canonical call: decimals normalised (``"0.10"`` = ``"0.1"``), floats refused,
    every argument present (defaults filled in by the caller), keys sorted (D-4)."""
    canonical = canonical_json({"tool": tool, "environment": environment.value, "args": args})
    return hashlib.sha256(canonical.encode()).hexdigest()


def fingerprint_applies(risk: RiskDirection) -> bool:
    """D-20 (3): only risk-increasing actions are deduplicated without a key."""
    return risk is RiskDirection.INCREASES


def new_claim(
    *, user_id: str, kind: KeyKind, key: str, request_hash: str, action_id: str, now: datetime
) -> IdempotencyRecord:
    ttl = EXPLICIT_KEY_TTL if kind is KeyKind.EXPLICIT else IN_FLIGHT_GUARD
    return IdempotencyRecord(
        user_id=user_id,
        kind=kind,
        key=key,
        request_hash=request_hash,
        action_id=action_id,
        status=RecordStatus.IN_PROGRESS,
        created_at=now,
        expires_at=now + ttl,
    )


def expiry_when_done(
    record: IdempotencyRecord, now: datetime, window: timedelta, outcome: "Outcome"
) -> datetime:
    """An explicit key lives its 24 h from the claim; a fingerprint ``window`` after the answer, but
    an unknown outcome ``IN_FLIGHT_GUARD`` (the exchange may still list the order with a delay; an
    identical call soon after must not become a second order, review 1 of ticket 012)."""
    if record.kind is KeyKind.EXPLICIT:
        return record.expires_at
    return now + (IN_FLIGHT_GUARD if outcome is Outcome.UNKNOWN else window)


def is_stale(record: IdempotencyRecord, now: datetime) -> bool:
    """Claimed (or, once sent, sending) longer than any call can take: its holder did not complete
    (a crash, an answer that could not be stored)."""
    since = record.sent_at if record.sent_at is not None else record.created_at
    return now - since >= IN_FLIGHT_GUARD


def keeps_record(kind: KeyKind, outcome: Outcome) -> bool:
    """Whether the answer is stored for repeats or the record is released.

    An order that exists or may exist is always kept: that is what stops a second one. A definite
    refusal is kept for an explicit key (same key, same answer; a new attempt needs a new key, D-20
    (5)), but not for a fingerprint, whose only job is to stop duplicate orders. A transient failure
    is never kept: the agent was told to try again.
    """
    if outcome in {Outcome.ACCEPTED, Outcome.UNKNOWN}:
        return True
    return outcome is Outcome.REFUSED and kind is KeyKind.EXPLICIT
