"""Known Bybit V5 REST hosts and the exchange environment each one serves (ticket 007).

Signed requests carry an API key and an HMAC of the request, so the server must never send them
to a host it does not know: the configured base URL is checked against this table at start-up
(``config.Settings``), over https only. Source: https://bybit-exchange.github.io/docs/v5/guide
(checked 2026-10-28). Regional mainnet hosts are deliberately absent: add one line when needed.
"""

from types import MappingProxyType
from typing import Final

from guarded_exchange.domain.users import Environment

BYBIT_HOSTS: Final = MappingProxyType(
    {
        "api.bybit.com": Environment.MAINNET,
        "api.bytick.com": Environment.MAINNET,
        "api-demo.bybit.com": Environment.TESTNET,  # Demo Trading (PLAN ADR-001)
        "api-testnet.bybit.com": Environment.TESTNET,  # classic testnet (fallback)
    }
)

LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})
"""Accepted only with the explicit test flag: a local stub stands in for the exchange (testnet)."""
