"""The autouse guard in conftest makes accidental calls to external APIs fail loudly (NFR-2)."""

import socket

import httpx2
import pytest

from tests.conftest import ExternalNetworkError

pytestmark = pytest.mark.req("NFR-2")


def test_dns_lookup_of_external_host_is_blocked() -> None:
    with pytest.raises(ExternalNetworkError):
        socket.getaddrinfo("api.bybit.com", 443)


def test_connect_to_external_address_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(ExternalNetworkError):
        sock.connect(("1.1.1.1", 443))


async def test_real_http_client_cannot_reach_the_exchange() -> None:
    async with httpx2.AsyncClient() as http:
        with pytest.raises(ExternalNetworkError):
            await http.get("https://api-demo.bybit.com/v5/market/time")
