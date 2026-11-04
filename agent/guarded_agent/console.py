"""Elicitation in the console: the human answers the server's form, not the model (FR-39, AC-5).

``mcp.Client`` hands every ``InputRequiredResult`` question (and legacy push elicitation) to the
callback below; its answer goes back to the server as ``inputResponses``, a field the model never
writes. Form mode asks accept / decline / cancel first, then each field of the flat schema; URL
mode only prints the link. End of input means cancel.
"""

from collections.abc import Callable, Mapping
from typing import Any

from mcp.client import ClientRequestContext
from mcp.types import ElicitRequestFormParams, ElicitRequestParams, ElicitResult

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]

FieldValue = str | int | float | bool | list[str] | None
_YES = {"y", "yes", "д", "да", "true"}
_NO = {"n", "no", "н", "нет", "false"}


class ConsoleElicitation:
    """An ``ElicitationFnT``; ``ask``/``say`` default to ``input``/``print`` and are swapped in tests."""

    def __init__(self, ask: InputFn = input, say: OutputFn = print) -> None:
        self._ask = ask
        self._say = say

    async def __call__(self, context: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult:
        try:
            return self._handle(params)
        except (EOFError, KeyboardInterrupt):
            return ElicitResult(action="cancel")

    def _handle(self, params: ElicitRequestParams) -> ElicitResult:
        self._say(f"\n=== The server asks you (not the model) ===\n{params.message}")
        if not isinstance(params, ElicitRequestFormParams):
            self._say(f"Open this link yourself: {params.url}")
            return ElicitResult(action="accept" if self._yes_no("Opened it? [y/n] ") else "decline")
        choice = self._ask("[a]ccept / [d]ecline / [c]ancel: ").strip().lower()
        if choice in {"d", "decline"}:
            return ElicitResult(action="decline")
        if choice not in {"a", "accept"}:
            return ElicitResult(action="cancel")
        schema = params.requested_schema
        required = set(schema.get("required", []))
        content: dict[str, FieldValue] = {}
        for name, prop in schema.get("properties", {}).items():
            value = self._field(name, prop, required=name in required)
            if value is not None:
                content[name] = value
        return ElicitResult(action="accept", content=content)

    def _field(self, name: str, prop: Mapping[str, Any], *, required: bool) -> FieldValue:
        label = prop.get("title") or name
        hint = f" ({prop['description']})" if prop.get("description") else ""
        kind = prop.get("type")
        if kind == "boolean":
            return self._yes_no(f"{label}{hint} [y/n]: ")
        choices = prop.get("enum")
        prompt = f"{label}{hint}{' ' + '/'.join(map(str, choices)) if choices else ''}: "
        while True:
            raw = self._ask(prompt).strip()
            if not raw and not required:
                return None
            value = _coerce(raw, kind)
            if value is not None and (not choices or value in choices):
                return value
            self._say("  not a valid value, try again")

    def _yes_no(self, prompt: str) -> bool:
        while True:
            answer = self._ask(prompt).strip().lower()
            if answer in _YES:
                return True
            if answer in _NO:
                return False
            self._say("  please answer y or n")


def _coerce(raw: str, kind: object) -> FieldValue:
    if kind == "integer":
        return int(raw) if raw.lstrip("-").isdigit() else None
    if kind == "number":
        try:
            return float(raw)
        except ValueError:
            return None
    return raw or None
