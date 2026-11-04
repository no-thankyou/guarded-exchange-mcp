"""The client-UI channel on the MCP side: the confirmation form of a trading call (SPEC FR-20, FR-21,
FR-22, FR-42, FR-47 (a); PLAN ADR-005 (13), D-1, D-4, D-5).

Round 1 runs the action's use case as usual. If that parks the action and the policy trusts the client's
UI, and this client declared form elicitation on protocol 2026-07-28, the call answers with an
``InputRequiredResult``: a form built from the **server's** pending record, and a ``requestState`` that
names the pending and the digest of the terms shown. The SDK seals that state (AES-GCM, key from the
master key, bound to the tool and the raw arguments), so the model, which never sees it, cannot mint or
alter one.

Round 2 is recognised **only** by ``ctx.request_state`` and never runs the action's use case: it takes no
idempotency token and creates nothing (D-1). It reads the human's ``ElicitResult`` fail closed - only
``accept`` with ``content.confirm is True`` confirms - and hands it to ``ClientUiConfirmation``, which
holds the decision to the pending, to the policy and to a single CAS. ``inputResponses`` without a
``requestState`` are refused before anything is created.

Nothing here can be reached by the model as a decision: the answer is the client's own elicitation
response, which a tool call cannot produce (AC-4).
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from mcp.server.mcpserver import Context
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    InputResponses,
)
from pydantic import BaseModel

from guarded_exchange.application.client_ui_confirmation import ClientUiConfirmation, UiAnswer
from guarded_exchange.application.confirmation import ConfirmationPending, offering_client_ui
from guarded_exchange.domain.actions import ActionKind
from guarded_exchange.domain.confirmation import ConfirmationStatus as S
from guarded_exchange.domain.confirmation import PendingConfirmation
from guarded_exchange.domain.errors import (
    ConfirmationDeclined,
    ConfirmationExpired,
    ConfirmationNotExecuted,
    InvalidParams,
)
from guarded_exchange.interface.mcp.access import PrincipalProvider
from guarded_exchange.interface.mcp.results import pending_answer, run_tool_or
from guarded_exchange.interface.mcp.schemas_confirmation import ConfirmationOut

CONFIRM_KEY: Final = "confirm"
MRTR_PROTOCOL: Final = "2026-07-28"
"""The only era the UI channel serves (spike 003); earlier connections use an external channel."""
SPACE_HINT: Final = "Press Space to tick the box, then Enter to submit. Submitting without the tick declines."

CONFIRM_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "confirm": {"type": "boolean", "title": "Yes, execute exactly this", "default": False},
    },
    "required": ["confirm"],
}


@dataclass(frozen=True, slots=True)
class ClientUiChannel:
    """What a trading tool needs for the UI channel (``confirmation`` is ``None``: the channel is off)."""

    confirmation: ClientUiConfirmation | None
    principal: PrincipalProvider


def client_offers_form(ctx: Context) -> bool:
    """Capability and protocol, never ``clientInfo`` (not verifiable, FR-36). The SDK's own rule for a
    form-capable client: ``{form}`` or the bare ``{}``; a url-only declaration is not a form."""
    capabilities = ctx.client_capabilities
    elicitation = None if capabilities is None else capabilities.elicitation
    return (
        ctx.protocol_version == MRTR_PROTOCOL
        and elicitation is not None
        and (elicitation.form is not None or elicitation.url is None)
    )


async def run_confirmable(
    tool: ActionKind,
    ctx: Context,
    call: Callable[[], Awaitable[BaseModel]],
    channel: ClientUiChannel,
) -> CallToolResult | InputRequiredResult:
    """``run_tool`` for an action that may need the human: ``call`` is the use case (round 1 only)."""
    if channel.confirmation is None:
        return await run_tool_or(tool.value, ctx.request_id, call(), pending_answer)
    if ctx.request_state is None:
        if ctx.input_responses is not None:
            return await run_tool_or(tool.value, ctx.request_id, _refuse_unasked_answers(), pending_answer)
        with offering_client_ui(client_offers_form(ctx)):
            return await run_tool_or(tool.value, ctx.request_id, call(), _ask_or_report)
    return await run_tool_or(
        tool.value,
        ctx.request_id,
        _second_round(
            tool, channel.confirmation, channel.principal(), ctx.request_state, ctx.input_responses
        ),
        _ask_or_report,
    )


async def _refuse_unasked_answers() -> BaseModel:
    raise InvalidParams(
        "inputResponses without requestState are not accepted; nothing was created",
        next_step="Call the tool again without inputResponses.",
        details={"reason": "input_responses_without_request_state"},
    )


async def _second_round(
    tool: ActionKind,
    confirmation: ClientUiConfirmation,
    user_id: str | None,
    state: str,
    responses: InputResponses | None,
) -> BaseModel:
    pending_id, terms = _read_state(state)
    if responses is None:  # a state-only retry: ask the same question about the same pending (D-1)
        waiting = await confirmation.current(user_id, tool, pending_id, terms)
        if waiting.status is S.PENDING:
            raise ConfirmationPending(waiting, created=False, ask_client_ui=True)
        return _outcome(waiting)
    answer, note = read_answer(responses)
    return _outcome(await confirmation.decide(user_id, tool, pending_id, terms, answer, note=note))


def read_answer(responses: InputResponses) -> tuple[UiAnswer, str]:
    """Fail closed: only ``accept`` with ``content.confirm is True`` is a confirmation (D-4)."""
    answer = responses.get(CONFIRM_KEY)
    if not isinstance(answer, ElicitResult):
        return UiAnswer.DECLINE, "no valid answer to the form"
    if answer.action == "accept":
        if (answer.content or {}).get(CONFIRM_KEY) is True:
            return UiAnswer.CONFIRM, "confirmed in the client UI"
        return UiAnswer.DECLINE, "form submitted without confirming"
    if answer.action == "cancel":
        return UiAnswer.CANCEL, "form closed by the user"
    # An automated or buggy client can send this too: journaled as the client's answer, not the human's (D-6).
    return UiAnswer.DECLINE, "declined by client UI"


def _outcome(pending: PendingConfirmation) -> BaseModel:
    """What the agent hears once the human answered (or someone else decided first, D-2)."""
    if pending.status in {S.REJECTED, S.CANCELLED}:
        raise ConfirmationDeclined(
            f"confirmation {pending.id} was not confirmed ({pending.status.value}); nothing was executed",
            next_step="Do not ask again unless the user says so.",
            details={"status": pending.status.value},
        )
    if pending.status in {S.EXPIRED, S.EXPIRED_AFTER_APPROVAL}:
        raise ConfirmationExpired(
            f"confirmation {pending.id} is {pending.status.value}; nothing was executed",
            next_step="Request the action again only if the user still wants it.",
            details={"status": pending.status.value},
        )
    if pending.status is S.FAILED:
        raise ConfirmationNotExecuted(
            f"confirmation {pending.id} was approved but not executed: it was refused when re-checked",
            next_step="Tell the user; do not retry unchanged.",
            details={"status": pending.status.value},
        )
    return ConfirmationOut.from_domain(pending)


def _ask_or_report(parked: ConfirmationPending) -> CallToolResult | InputRequiredResult:
    if parked.ask_client_ui and parked.pending.status is S.PENDING:
        return ask(parked.pending)
    return pending_answer(parked)


def ask(pending: PendingConfirmation) -> InputRequiredResult:
    """The form, built only from the stored pending (what the executor will hold the action to, FR-21)."""
    state = json.dumps({"id": pending.id, "terms": pending.terms_digest}, separators=(",", ":"))
    return InputRequiredResult(
        input_requests={
            CONFIRM_KEY: ElicitRequest(
                params=ElicitRequestFormParams(message=form_message(pending), requested_schema=CONFIRM_SCHEMA)
            )
        },
        request_state=state,
    )


def form_message(pending: PendingConfirmation) -> str:
    lines = [
        f"Confirm {pending.tool.value} on {pending.environment.value} ({pending.risk.value} risk)",
        "Request:",
        *_tree(pending.action, 1),
        "What will be executed:",
        *_tree(pending.shown, 1),
        f"Valid until {pending.expires_at.isoformat()}",
        SPACE_HINT,
    ]
    return "\n".join(lines)


def _tree(value: object, depth: int) -> list[str]:
    pad = "  " * depth
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            if isinstance(item, dict | list):
                out += [f"{pad}{key}:", *_tree(item, depth + 1)]
            else:
                out.append(f"{pad}{key}: {item}")
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict | list):
                out += [f"{pad}-", *_tree(item, depth + 1)]
            else:
                out.append(f"{pad}- {item}")
        return out
    return [f"{pad}{value}"]


def _read_state(state: str) -> tuple[str, str]:
    """Our own sealed state, verified by the SDK; still parsed defensively (fail closed)."""
    try:
        data = json.loads(state)
        pending_id, terms = data["id"], data["terms"]
    except (ValueError, KeyError, TypeError):
        pending_id = terms = None
    if not isinstance(pending_id, str) or not isinstance(terms, str):
        raise InvalidParams(
            "requestState is not one this server issued",
            next_step="Call the tool again from the start.",
            details={"reason": "bad_request_state"},
        )
    return pending_id, terms
