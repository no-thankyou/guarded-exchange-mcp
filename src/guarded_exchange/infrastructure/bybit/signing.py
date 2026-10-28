"""Bybit V5 request signing (HMAC-SHA256), https://bybit-exchange.github.io/docs/v5/guide#authentication.

``sign = HMAC_SHA256(secret, timestamp + api_key + recv_window + payload)`` as lowercase hex, where
``payload`` is the exact query string of a GET or the exact JSON body of a POST. The secret is
used here and nowhere else; headers built here must never be logged.
"""

import hashlib
import hmac
from typing import Final

from guarded_exchange.domain.credentials import ApiKeyPair

RECV_WINDOW_MS: Final = "5000"


def sign(secret: str, timestamp_ms: str, api_key: str, recv_window: str, payload: str) -> str:
    message = f"{timestamp_ms}{api_key}{recv_window}{payload}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(
    keys: ApiKeyPair, *, timestamp_ms: int, payload: str, recv_window: str = RECV_WINDOW_MS
) -> dict[str, str]:
    timestamp = str(timestamp_ms)
    return {
        "X-BAPI-API-KEY": keys.api_key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": sign(keys.api_secret, timestamp, keys.api_key, recv_window, payload),
    }
