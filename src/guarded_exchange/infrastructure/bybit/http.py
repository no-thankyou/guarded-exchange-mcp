"""Factory for the ``httpx2.AsyncClient`` used by the Bybit adapter."""

import httpx2


def make_http_client(
    base_url: str,
    *,
    connect_timeout_s: float,
    read_timeout_s: float,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """Explicit split timeouts; ``transport`` lets tests plug in ``httpx2.MockTransport``."""
    timeout = httpx2.Timeout(
        connect=connect_timeout_s,
        read=read_timeout_s,
        write=read_timeout_s,
        pool=connect_timeout_s,
    )
    return httpx2.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        headers={"User-Agent": "guarded-exchange-mcp"},
        follow_redirects=False,
    )
