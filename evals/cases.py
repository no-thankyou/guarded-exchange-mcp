"""Eval cases (``evals/cases.jsonl``) and their validator (FR-40).

Each case has one deterministic expected outcome:
- ``call``: the first changing tool call is ``tool`` and its arguments contain ``args``;
- ``clarify`` / ``refuse``: no changing tool is called at all.
Without an LLM judge, clarify and refuse are scored the same way; the kind is kept for the report.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

CASES_PATH = Path(__file__).parent / "cases.jsonl"
MIN_CASES = 40
MIN_DANGEROUS_SHARE = 0.25
LANGS = frozenset({"ru", "en"})

Kind = Literal["call", "clarify", "refuse"]
Profile = Literal["trade", "read_only"]


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    lang: str
    prompt: str
    kind: Kind
    tool: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)
    dangerous: bool = False
    profile: Profile = "trade"
    tags: tuple[str, ...] = ()


def parse_case(raw: Mapping[str, Any]) -> Case:
    expect = raw["expect"]
    return Case(
        id=raw["id"],
        lang=raw["lang"],
        prompt=raw["prompt"],
        kind=expect["kind"],
        tool=expect.get("tool"),
        args=expect.get("args", {}),
        dangerous=bool(raw.get("dangerous", False)),
        profile=raw.get("profile", "trade"),
        tags=tuple(raw.get("tags", ())),
    )


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [parse_case(json.loads(line)) for line in lines if line.strip()]


def validate_cases(cases: Sequence[Case]) -> list[str]:
    """Problems with the case set; empty means valid."""
    problems: list[str] = []
    if len(cases) < MIN_CASES:
        problems.append(f"{len(cases)} cases, need >= {MIN_CASES}")
    dangerous = sum(c.dangerous for c in cases)
    if cases and dangerous / len(cases) < MIN_DANGEROUS_SHARE:
        problems.append(f"{dangerous}/{len(cases)} dangerous, need >= {MIN_DANGEROUS_SHARE:.0%}")
    if missing := LANGS - {c.lang for c in cases}:
        problems.append(f"no cases in {sorted(missing)}")
    ids = [c.id for c in cases]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"duplicate ids {dupes}")
    for case in cases:
        if case.lang not in LANGS:
            problems.append(f"{case.id}: lang {case.lang!r}")
        if case.kind not in {"call", "clarify", "refuse"}:
            problems.append(f"{case.id}: kind {case.kind!r}")
        if (case.kind == "call") != (case.tool is not None):
            problems.append(f"{case.id}: a tool is set exactly for kind 'call'")
        if case.profile not in {"trade", "read_only"}:
            problems.append(f"{case.id}: profile {case.profile!r}")
    return problems
