"""Users of the server and the defaults a new user starts with (SPEC FR-45, NFR-1)."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from guarded_exchange.domain.errors import InvalidParams

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class Environment(StrEnum):
    """Exchange environment. ``testnet`` is the exchange's test environment (Bybit Demo Trading)."""

    TESTNET = "testnet"
    MAINNET = "mainnet"


class UserStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    status: UserStatus
    environment: Environment
    created_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE


def validate_user_name(raw: str) -> str:
    """Return the canonical user name or raise ``InvalidParams`` (lowercase, 1-64 chars)."""
    name = raw.strip().lower()
    if not _NAME_RE.fullmatch(name):
        raise InvalidParams(
            "user name must be 1-64 characters: lowercase letters, digits, '_', '.', '-'",
            next_step="Pick a name like 'local' or 'alice'.",
            details={"field": "name"},
        )
    return name


def new_user(user_id: str, name: str, created_at: datetime) -> User:
    """A new user is active and starts on the test environment (FR-45: testnet by default).

    The rest of the FR-45 defaults (read-only profile, empty allowlist) belong to the policy,
    which is not part of the user record.
    """
    return User(
        id=user_id,
        name=validate_user_name(name),
        status=UserStatus.ACTIVE,
        environment=Environment.TESTNET,
        created_at=created_at,
    )
