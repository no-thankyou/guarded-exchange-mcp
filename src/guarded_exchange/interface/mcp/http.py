"""Remote mode: Streamable HTTP with bearer tokens (PLAN ADR-003, ADR-004; ticket 026).

- ``BearerTokenVerifier`` adapts the application's ``Authenticator`` port to the SDK's
  ``TokenVerifier``. The SDK's bearer middleware calls it on **every** HTTP request; no token,
  an unknown, malformed or revoked token, or a revoked user -> ``None`` -> HTTP 401 before any
  MCP processing. A failure of the check itself (e.g. the database) also refuses (fail closed).
- The principal of a remote call is ``AccessToken.subject`` (our user id) of the verified token,
  read through ``get_access_token()`` (a contextvar set by the SDK's auth middleware). Nothing
  the client sends - ``clientInfo``, other headers, tool arguments - can change it (FR-33).
- ``subject`` also binds ``requestState`` (MRTR, ticket 020) and legacy HTTP sessions to the user.
- The secret never leaves the verifier: the ``AccessToken`` kept in the request context carries a
  placeholder instead of it, and nothing here logs it.
"""

from collections.abc import Sequence
from typing import Final

import structlog
import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette

from guarded_exchange.application.ports import Authenticator
from guarded_exchange.interface.mcp.guarded_server import GuardedMCPServer
from guarded_exchange.interface.mcp.server import UseCases, create_server

log = structlog.get_logger(__name__)

CLIENT_ID: Final = "guarded-exchange-bearer"
"""Constant ``client_id`` of every verified token: the user is told apart by ``subject`` only."""

REDACTED: Final = "redacted"
MCP_PATH: Final = "/mcp"


class BearerTokenVerifier:
    """SDK ``TokenVerifier`` over our ``Authenticator`` port."""

    def __init__(self, authenticator: Authenticator) -> None:
        self._authenticator = authenticator

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            user_id = await self._authenticator.authenticate(token)
        except Exception as exc:
            # Fail closed: the request gets 401. Only the exception type is logged - the message
            # of a driver error could quote parameters, and the parameter here is the hash.
            log.error("bearer_check_failed", error_type=type(exc).__name__)
            return None
        if user_id is None:
            log.info("bearer_rejected")
            return None
        return AccessToken(token=REDACTED, client_id=CLIENT_ID, scopes=[], subject=user_id)


def token_principal() -> str | None:
    """``PrincipalProvider`` of the remote mode: the verified token's user, else nobody."""
    token = get_access_token()
    return None if token is None else token.subject


def auth_settings(public_url: str) -> AuthSettings:
    """Bearer auth without an OAuth authorization server.

    ``resource_server_url=None``: no Protected Resource Metadata is published (there is no
    authorization server to point clients to). ``issuer_url`` is required by the SDK model but
    unused without an auth server provider; it names this server.
    """
    return AuthSettings(issuer_url=AnyHttpUrl(public_url), resource_server_url=None)


def create_http_server(
    use_cases: UseCases, authenticator: Authenticator, *, public_url: str, version: str = ""
) -> GuardedMCPServer:
    """The same tools as on stdio; the principal comes from the bearer token only."""
    return create_server(
        use_cases,
        principal=token_principal,
        version=version,
        token_verifier=BearerTokenVerifier(authenticator),
        auth=auth_settings(public_url),
    )


def http_app(
    mcp: GuardedMCPServer, *, allowed_hosts: Sequence[str], allowed_origins: Sequence[str]
) -> Starlette:
    """The ASGI app serving MCP at ``/mcp``. Host/Origin checks against DNS rebinding are always on.

    Its lifespan runs the SDK session manager; an ASGI server (uvicorn) starts it, tests enter
    ``mcp.session_manager.run()`` themselves.
    """
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts),
        allowed_origins=list(allowed_origins),
    )
    return mcp.streamable_http_app(streamable_http_path=MCP_PATH, transport_security=security)


async def serve_http(app: Starlette, *, host: str, port: int) -> None:
    """Serve until interrupted. Logs go through the root logger (our JSON handler on stderr)."""
    config = uvicorn.Config(app, host=host, port=port, log_config=None, server_header=False)
    await uvicorn.Server(config).serve()
