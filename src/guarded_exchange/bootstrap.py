"""Composition root: the only module that reads configuration and wires adapters to use cases."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import anyio
import httpx2
import structlog
from mcp.server import MCPServer

from guarded_exchange.application.read_market import GetTicker
from guarded_exchange.config import Settings
from guarded_exchange.infrastructure.bybit.client import BybitClient, RetryPolicy
from guarded_exchange.infrastructure.bybit.http import make_http_client
from guarded_exchange.infrastructure.logging import configure_logging
from guarded_exchange.interface.cli.main import Transport, build_cli
from guarded_exchange.interface.mcp.server import UseCases, create_server, serve_stdio


def _version() -> str:
    try:
        return version("guarded-exchange-mcp")
    except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
        return "0.0.0"


def build_use_cases(settings: Settings, http: httpx2.AsyncClient) -> UseCases:
    exchange = BybitClient(
        http,
        retry=RetryPolicy(
            max_attempts=settings.bybit_get_max_attempts,
            base_delay_s=settings.bybit_get_backoff_base_s,
        ),
    )
    return UseCases(get_ticker=GetTicker(exchange))


@asynccontextmanager
async def app_server(
    settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
) -> AsyncIterator[MCPServer[Any]]:
    """Build a fully wired server and close its resources on exit.

    ``transport`` is a test seam (``httpx2.MockTransport``); production passes ``None``.
    """
    async with make_http_client(
        str(settings.bybit_base_url),
        connect_timeout_s=settings.bybit_connect_timeout_s,
        read_timeout_s=settings.bybit_read_timeout_s,
        transport=transport,
    ) as http:
        yield create_server(build_use_cases(settings, http), version=_version())


async def _serve_stdio(settings: Settings) -> None:
    async with app_server(settings) as mcp:
        await serve_stdio(mcp)


def serve(transport: Transport) -> None:
    settings = Settings()
    configure_logging(settings.log_level)  # before MCPServer(): it calls logging.basicConfig
    structlog.get_logger(__name__).info(
        "server_starting",
        transport=transport,
        exchange_base_url=str(settings.bybit_base_url),
        version=_version(),
    )
    anyio.run(_serve_stdio, settings)


def cli() -> None:
    build_cli(serve)()
