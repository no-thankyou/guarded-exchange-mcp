"""System implementation of the ``TokenGenerator`` port."""

import secrets

from guarded_exchange.domain.access_tokens import TOKEN_PREFIX


class SecretTokenGenerator:
    def new_token(self) -> str:
        # 32 bytes = 256 bits from the OS CSPRNG -> 43 URL-safe characters.
        return TOKEN_PREFIX + secrets.token_urlsafe(32)
