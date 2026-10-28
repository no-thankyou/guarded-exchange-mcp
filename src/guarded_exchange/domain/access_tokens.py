"""Bearer access tokens of the remote mode (SPEC FR-29, FR-33; PLAN ADR-004).

A token is an opaque random secret shown to the admin once, at issue time. Only its SHA-256
hash is stored: a copy of the database does not let anyone call the server. The secret has
256 random bits, so an unsalted hash is enough (no dictionary to attack) and lets the server
find the token by an indexed lookup instead of comparing against every stored hash.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final

TOKEN_PREFIX: Final = "gxt_"  # noqa: S105 - a public prefix, not a secret
"""Makes a leaked token recognisable (secret scanners, logs) and rejects obvious garbage early."""

MAX_TOKEN_LENGTH: Final = 128
"""Anything longer is refused before hashing: no reason to hash megabytes from an anonymous caller."""

_TOKEN_RE: Final = re.compile(rf"^{TOKEN_PREFIX}[A-Za-z0-9_-]{{43,{MAX_TOKEN_LENGTH - len(TOKEN_PREFIX)}}}$")


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A stored token: never the secret itself, only its hash."""

    id: str
    user_id: str
    token_hash: str
    created_at: datetime
    revoked_at: datetime | None = None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


def is_well_formed(raw: str) -> bool:
    """Cheap shape check before any hashing or database lookup (fail closed on anything odd)."""
    return _TOKEN_RE.fullmatch(raw) is not None


def hash_token(raw: str) -> str:
    """SHA-256 of the secret, lowercase hex: what the database stores and is searched by."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_matches(stored: AccessToken, raw: str) -> bool:
    """Constant-time comparison of the presented secret with the stored hash."""
    return hmac.compare_digest(stored.token_hash, hash_token(raw))
