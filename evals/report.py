"""Metrics and the markdown/JSON report of an eval run (FR-40)."""

from collections import defaultdict
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from evals.runner import CaseResult, EvalReport

_COST_STEP = Decimal("0.0001")


def _rate(results: Sequence[CaseResult], ok: Callable[[CaseResult], bool]) -> dict[str, Any]:
    hits = sum(ok(r) for r in results)
    return {"hits": hits, "total": len(results), "rate": round(hits / len(results), 4) if results else None}


def metrics(report: EvalReport) -> dict[str, Any]:
    calls = [r for r in report.results if r.case.kind == "call"]
    held = [r for r in report.results if r.case.kind != "call"]
    return {
        "tool_accuracy": _rate(
            calls, lambda r: r.first_change is not None and r.first_change.name == r.case.tool
        ),
        "args_accuracy": _rate(calls, lambda r: r.grade.passed),
        "refuse_or_clarify_accuracy": _rate(held, lambda r: r.grade.passed),
        "dangerous_pass_rate": _rate(
            [r for r in report.results if r.case.dangerous], lambda r: r.grade.passed
        ),
        "overall_pass_rate": _rate(report.results, lambda r: r.grade.passed),
    }


def to_json(report: EvalReport) -> dict[str, Any]:
    meter = report.meter
    return {
        "model": report.model,
        "runs": report.runs,
        "cases": report.cases,
        "case_runs_scored": len(report.results),
        "stopped_by_budget": report.stopped_by_budget,
        "cost_usd": str(meter.spent.quantize(_COST_STEP)),
        "budget_usd": str(meter.budget),
        "model_calls": meter.model_calls,
        "tokens": {
            "input": meter.usage.input_tokens,
            "cached_input": meter.usage.cached_input_tokens,
            "cache_write": meter.usage.cache_write_tokens,
            "output": meter.usage.output_tokens,
        },
        "metrics": metrics(report),
        "results": [
            {
                "case": r.case.id,
                "run": r.run,
                "outcome": r.grade.outcome.value,
                "tool": r.first_change.name if r.first_change else None,
                "args": dict(r.first_change.arguments) if r.first_change else None,
            }
            for r in report.results
        ],
    }


def to_markdown(report: EvalReport) -> str:
    data = to_json(report)
    lines = [
        f"# Tool-selection evals: `{report.model}`",
        "",
        f"{report.cases} cases x {report.runs} runs, {data['case_runs_scored']} case-runs scored, "
        f"{data['model_calls']} model calls, cost ${data['cost_usd']} of ${data['budget_usd']} budget.",
    ]
    if report.stopped_by_budget:
        lines += ["", "**Stopped by budget: partial report.**"]
    lines += ["", "| Metric | Rate | Hits / total |", "|---|---|---|"]
    for name, m in data["metrics"].items():
        rate = "-" if m["rate"] is None else f"{m['rate']:.0%}"
        lines.append(f"| {name.replace('_', ' ')} | {rate} | {m['hits']} / {m['total']} |")
    failures = _failures(report.results)
    if failures:
        lines += [
            "",
            "## Failed case-runs",
            "",
            "| Case | Kind | Failed runs | Outcomes |",
            "|---|---|---|---|",
        ]
        lines += [f"| {cid} | {kind} | {n} | {outcomes} |" for cid, kind, n, outcomes in failures]
    return "\n".join(lines) + "\n"


def _failures(results: Sequence[CaseResult]) -> list[tuple[str, str, int, str]]:
    by_case: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        if not r.grade.passed:
            by_case[r.case.id].append(r)
    return [
        (cid, rs[0].case.kind, len(rs), ", ".join(sorted({r.grade.outcome.value for r in rs})))
        for cid, rs in sorted(by_case.items())
    ]
