"""Running the cases several times with a hard budget (FR-40, ADR-016).

Only the first changing decision is scored: a changing tool call (``readOnlyHint`` false) is
recorded and the run stops there, so nothing changing ever executes. Read tools run for real
against the in-process server. Cost is counted from each model call's ``usage``; once the spend
reaches the budget the next model call is refused and the report covers what finished.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from mcp import Client
from mcp.server import MCPServer

from evals.cases import Case, Profile
from evals.scoring import Grade, grade
from guarded_agent.loop import Agent, McpToolSession, ToolSession
from guarded_agent.provider import AssistantTurn, LLMProvider, Message, ToolCall, ToolResult, ToolSpec, Usage

DEFAULT_BUDGET_USD = Decimal("0.50")
MAX_STEPS = 4
_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Pricing:
    """USD per 1M tokens."""

    input: Decimal
    cached_input: Decimal
    cache_write: Decimal
    output: Decimal

    def cost(self, usage: Usage) -> Decimal:
        return (
            usage.input_tokens * self.input
            + usage.cached_input_tokens * self.cached_input
            + usage.cache_write_tokens * self.cache_write
            + usage.output_tokens * self.output
        ) / _MILLION


# Standard tier, checked 2026-10-27 (docs/research/clients-and-agent.md, 2.9).
PRICES: dict[str, Pricing] = {
    "claude-haiku-5-5": Pricing(Decimal("0.10"), Decimal("0.01"), Decimal("0.125"), Decimal("0.50")),
    "gpt-6-luna": Pricing(Decimal("0.10"), Decimal("0.01"), Decimal("0.125"), Decimal("0.50")),
    "fake": Pricing(Decimal(0), Decimal(0), Decimal(0), Decimal(0)),
}


class BudgetExceeded(Exception):
    pass


@dataclass
class Meter:
    budget: Decimal
    pricing: Pricing
    spent: Decimal = Decimal(0)
    model_calls: int = 0
    usage: Usage = field(default_factory=Usage)

    def add(self, usage: Usage) -> None:
        self.spent += self.pricing.cost(usage)
        self.model_calls += 1
        u = self.usage
        self.usage = Usage(
            u.input_tokens + usage.input_tokens,
            u.cached_input_tokens + usage.cached_input_tokens,
            u.cache_write_tokens + usage.cache_write_tokens,
            u.output_tokens + usage.output_tokens,
        )


class MeteredProvider:
    """Refuses a model call once the budget is spent (the last call may overshoot by itself)."""

    def __init__(self, provider: LLMProvider, meter: Meter) -> None:
        self._provider = provider
        self._meter = meter

    @property
    def model(self) -> str:
        return self._provider.model

    async def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AssistantTurn:
        if self._meter.spent >= self._meter.budget:
            raise BudgetExceeded(f"spent ${self._meter.spent:.4f} of ${self._meter.budget}")
        turn = await self._provider.complete(system, messages, tools)
        self._meter.add(turn.usage)
        return turn


class FirstChange(Exception):
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        super().__init__(name)
        self.call = ToolCall(id="", name=name, arguments=arguments)


class StopAtFirstChange:
    """``ToolSession`` that passes read tools through and stops the run at the first changing call."""

    def __init__(self, inner: ToolSession) -> None:
        self._inner = inner
        self._read_only: set[str] = set()

    async def list_tools(self) -> list[ToolSpec]:
        tools = await self._inner.list_tools()
        self._read_only = {t.name for t in tools if t.read_only}
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in self._read_only:  # unknown tools count as changing: fail closed
            raise FirstChange(name, arguments)
        return await self._inner.call_tool(name, arguments)


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: Case
    run: int
    first_change: ToolCall | None
    grade: Grade
    final_text: str


@dataclass
class EvalReport:
    model: str
    runs: int
    cases: int
    results: list[CaseResult]
    meter: Meter
    stopped_by_budget: bool = False


ServerFactory = Callable[[Profile], Awaitable[MCPServer[Any]]]


async def run_case(case: Case, provider: LLMProvider, server: MCPServer[Any]) -> tuple[ToolCall | None, str]:
    over_budget: BudgetExceeded | None = None
    async with Client(server) as client:
        agent = Agent(provider, StopAtFirstChange(McpToolSession(client)), max_steps=MAX_STEPS)
        try:
            run = await agent.ask(case.prompt)
        except FirstChange as change:
            return change.call, ""
        except BudgetExceeded as error:  # raised outside the client: inside it becomes an ExceptionGroup
            over_budget = error
    if over_budget is not None:
        raise over_budget
    return None, run.text


async def run_evals(
    cases: Sequence[Case],
    provider: LLMProvider,
    server_factory: ServerFactory,
    *,
    runs: int = 3,
    budget: Decimal = DEFAULT_BUDGET_USD,
    pricing: Pricing | None = None,
) -> EvalReport:
    price = pricing or PRICES.get(provider.model)
    if price is None:
        raise ValueError(f"no pricing for {provider.model!r}: the budget could not be enforced")
    meter = Meter(budget=budget, pricing=price)
    metered = MeteredProvider(provider, meter)
    report = EvalReport(model=provider.model, runs=runs, cases=len(cases), results=[], meter=meter)
    try:
        for run in range(1, runs + 1):
            for case in cases:
                change, text = await run_case(case, metered, await server_factory(case.profile))
                report.results.append(CaseResult(case, run, change, grade(case, change), text))
    except BudgetExceeded:
        report.stopped_by_budget = True
    return report
