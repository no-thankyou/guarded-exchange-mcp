"""AC-3 is checked, not hoped for: CI fails if a requirement in its range has no marked test (ticket 021).

The range is the SPEC's (FR-13...FR-28 and FR-41...FR-50) without what SPEC 7.1 moved to v2 (FR-46, FR-50).
Markers are read statically from the test sources (``pytest.mark.req(...)`` as decorator or ``pytestmark``),
so the check does not depend on which tests were selected for the run."""

import ast
from collections.abc import Iterable
from pathlib import Path

TESTS = Path(__file__).parents[1]
AC3_RANGE = [*range(13, 29), *range(41, 51)]
DEFERRED_TO_V2 = {46, 50}
REQUIRED = frozenset(f"FR-{n}" for n in AC3_RANGE if n not in DEFERRED_TO_V2)


def _number(requirement: str) -> int:
    return int(requirement.split("-")[1])


def requirements_in(source: str) -> set[str]:
    """Every string argument of a ``pytest.mark.req(...)`` call in ``source``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("mark.req"):
            found.update(
                a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
            )
    return found


def uncovered(required: Iterable[str], sources: Iterable[str]) -> list[str]:
    covered: set[str] = set()
    for source in sources:
        covered |= requirements_in(source)
    return sorted(set(required) - covered, key=_number)


def test_every_requirement_of_ac3_has_a_marked_test() -> None:
    sources = [p.read_text() for p in TESTS.rglob("test_*.py")]

    assert uncovered(REQUIRED, sources) == []


def test_the_check_fails_when_a_requirement_loses_its_test() -> None:
    marked = (
        "import pytest\n"
        'pytestmark = pytest.mark.req("FR-13", "AC-4")\n\n'
        '@pytest.mark.req("FR-14")\n'
        "def test_x(): ...\n"
    )

    assert uncovered({"FR-13", "FR-14", "FR-15"}, [marked]) == ["FR-15"]
    assert uncovered(REQUIRED, []) == sorted(REQUIRED, key=_number)


def test_the_deferred_requirements_are_exactly_the_ones_spec_7_1_defers() -> None:
    assert REQUIRED.isdisjoint({"FR-46", "FR-50"})
    assert {"FR-13", "FR-28", "FR-41", "FR-45", "FR-47", "FR-49"} <= REQUIRED
