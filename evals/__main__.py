"""``uv run python -m evals``: run the tool-selection evals and write the report.

    uv run python -m evals --provider fake                 # offline smoke run, $0
    uv run python -m evals --provider anthropic --runs 3   # real run (ANTHROPIC_API_KEY), budget $0.50
    uv run python -m evals --provider openai --runs 3      # real run (OPENAI_API_KEY)

Writes ``<out>.md`` and ``<out>.json`` (default ``evals/report``). Exits 2 if the case set is invalid.
"""

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

from evals.cases import load_cases, validate_cases
from evals.report import to_json, to_markdown
from evals.runner import DEFAULT_BUDGET_USD, run_evals
from evals.world import build_server
from guarded_agent.cli import make_provider
from guarded_agent.provider import LLMProvider, ScriptedProvider


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument("--provider", choices=["fake", "anthropic", "openai"], default="fake")
    parser.add_argument("--model", default=None)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--budget", type=Decimal, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--out", type=Path, default=Path("evals/report"))
    args = parser.parse_args(argv)

    cases = load_cases()
    if problems := validate_cases(cases):
        print("invalid cases:\n" + "\n".join(problems), file=sys.stderr)
        raise SystemExit(2)
    provider: LLMProvider = (
        ScriptedProvider() if args.provider == "fake" else make_provider(args.provider, args.model)
    )
    report = asyncio.run(run_evals(cases, provider, build_server, runs=args.runs, budget=args.budget))
    markdown = to_markdown(report)
    args.out.with_suffix(".md").write_text(markdown, encoding="utf-8")
    args.out.with_suffix(".json").write_text(json.dumps(to_json(report), indent=2, ensure_ascii=False) + "\n")
    print(markdown)


if __name__ == "__main__":
    main()
