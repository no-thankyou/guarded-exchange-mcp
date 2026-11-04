"""Who is calling and which tools they may see and call (FR-11, PLAN ADR-012, spike 002).

The decision is made by the application (``AuthorizeAccess``); this module only declares what
each tool needs and applies the answer to ``tools/list`` and ``tools/call``. Hiding a tool is a
convenience for the model; the security boundary is the check on every call.
"""

import uuid
from collections.abc import Callable, Mapping
from typing import Final

import structlog
from mcp.types import CallToolResult, Tool

from guarded_exchange.application.policy import AuthorizeAccess
from guarded_exchange.domain.errors import DomainError, PermissionDenied
from guarded_exchange.domain.policy import Permission
from guarded_exchange.interface.mcp.errors import error_result, internal_error_result

log = structlog.get_logger(__name__)

PrincipalProvider = Callable[[], str | None]
"""Returns the calling user's id. Local mode: the configured local user. Remote mode (ticket 026):
the subject of the verified bearer token from ``get_access_token()``; never client-supplied data."""

TOOL_PERMISSIONS: Final[Mapping[str, Permission]] = {
    "get_ticker": Permission.READ,
    "get_orderbook": Permission.READ,
    "get_candles": Permission.READ,
    "get_instrument_rules": Permission.READ,
    "get_my_policy": Permission.READ,
    "get_order_history": Permission.READ,
    "get_trades": Permission.READ,
    "get_balances": Permission.READ,
    "get_positions": Permission.READ,
    "get_open_orders": Permission.READ,
    "place_order": Permission.TRADE,
    "cancel_order": Permission.TRADE,
    "amend_order": Permission.TRADE,
    "cancel_all_orders": Permission.TRADE,
    "get_confirmation_status": Permission.READ,
    "cancel_confirmation": Permission.READ,  # withdraws the user's own request: never an approval
    "set_trading_stop": Permission.TRADE,
    "close_position": Permission.TRADE,
    "set_leverage": Permission.TRADE,
    "activate_kill_switch": Permission.READ,  # FR-44: any user may stop their own trading
}
"""What every registered tool needs. A tool missing here is hidden and refused (fail closed)."""


def fixed_principal(user_id: str) -> PrincipalProvider:
    """Local mode (ADR-004): one principal, resolved at start-up."""
    return lambda: user_id


class ToolGate:
    """Applies ``AuthorizeAccess`` to the MCP surface for the current principal."""

    def __init__(
        self,
        access: AuthorizeAccess,
        principal: PrincipalProvider,
        permissions: Mapping[str, Permission] = TOOL_PERMISSIONS,
    ) -> None:
        self._access = access
        self._principal = principal
        self._permissions = dict(permissions)

    async def visible(self, tools: list[Tool]) -> list[Tool]:
        allowed = await self._access.visible(self._principal(), self._permissions)
        return [tool for tool in tools if tool.name in allowed]

    async def refusal(self, name: str, mcp_request_id: str | None = None) -> CallToolResult | None:
        """``None`` if the call may proceed, else the tool result to return instead.

        Runs before argument validation, so a refused call learns nothing about the tool's schema.
        A failure of the check itself (e.g. the database) refuses too: fail closed, and the model
        gets ``internal_error`` with a ``request_id`` only, never the exception text.
        """
        request_id = uuid.uuid4().hex
        with structlog.contextvars.bound_contextvars(
            request_id=request_id, tool=name, mcp_request_id=mcp_request_id
        ):
            try:
                permission = self._permissions.get(name)
                if permission is None:
                    raise PermissionDenied(
                        f"tool {name!r} is not available",
                        next_step="Call only tools listed by tools/list.",
                    )
                await self._access.require(self._principal(), permission)
            except DomainError as exc:
                log.info("tool_denied", code=exc.code)
                return error_result(exc)
            except Exception:
                log.exception("tool_access_check_failed")
                return internal_error_result(request_id)
        return None
