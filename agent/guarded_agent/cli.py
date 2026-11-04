"""``guarded-agent``: a console chat with the server over stdio (default) or Streamable HTTP.

    uv run guarded-agent --provider anthropic            # spawns `python -m guarded_exchange serve`
    uv run guarded-agent --provider openai --url http://127.0.0.1:8000/mcp   # token: GX_TOKEN

Keys come from ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` (and optional ``ANTHROPIC_WORKSPACE_ID``).
"""

import argparse
import asyncio
import json
import os
import shlex
import sys
from contextlib import AsyncExitStack

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from guarded_agent.console import ConsoleElicitation
from guarded_agent.loop import Agent, McpToolSession
from guarded_agent.provider import LLMProvider, ToolCall

DEFAULT_SERVER = f"{shlex.quote(sys.executable)} -m guarded_exchange serve"


def make_provider(name: str, model: str | None) -> LLMProvider:
    """Imports lazily so one provider works without the other's SDK configured."""
    if name == "openai":
        from openai import AsyncOpenAI  # noqa: PLC0415

        from guarded_agent.openai_provider import DEFAULT_OPENAI_MODEL, OpenAIProvider  # noqa: PLC0415

        return OpenAIProvider(AsyncOpenAI(), model=model or DEFAULT_OPENAI_MODEL)
    from anthropic import AsyncAnthropic  # noqa: PLC0415

    from guarded_agent.anthropic_provider import DEFAULT_ANTHROPIC_MODEL, AnthropicProvider  # noqa: PLC0415

    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    headers = {"anthropic-workspace-id": workspace} if workspace else None
    return AnthropicProvider(AsyncAnthropic(default_headers=headers), model=model or DEFAULT_ANTHROPIC_MODEL)


def _show_call(call: ToolCall) -> None:
    print(f"  -> {call.name} {json.dumps(call.arguments, ensure_ascii=False)}")


async def chat(args: argparse.Namespace) -> None:
    provider = make_provider(args.provider, args.model)
    async with AsyncExitStack() as stack:
        if args.url:
            token = os.environ.get("GX_TOKEN")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            http = await stack.enter_async_context(httpx2.AsyncClient(headers=headers))
            target = streamable_http_client(args.url, http_client=http)
            client = Client(target, elicitation_callback=ConsoleElicitation())
        else:
            command, *rest = shlex.split(args.server)
            server = StdioServerParameters(command=command, args=rest, env=dict(os.environ))
            client = Client(server, elicitation_callback=ConsoleElicitation())
        await stack.enter_async_context(client)
        agent = Agent(provider, McpToolSession(client), max_steps=args.max_steps, on_tool_call=_show_call)
        print(f"guarded-agent ({provider.model}). Empty line or Ctrl-D to quit.")
        while True:
            try:
                text = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except EOFError:
                return
            if not text:
                return
            run = await agent.ask(text)
            print(f"agent> {run.text}" + ("\n(step limit reached)" if run.hit_step_limit else ""))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="guarded-agent", description=__doc__.splitlines()[0])
    parser.add_argument("--provider", choices=["anthropic", "openai"], default="anthropic")
    parser.add_argument("--model", default=None, help="default: claude-haiku-5-5 / gpt-6-luna")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="stdio server command")
    parser.add_argument("--url", default=None, help="Streamable HTTP endpoint instead of stdio")
    parser.add_argument("--max-steps", type=int, default=8)
    asyncio.run(chat(parser.parse_args(argv)))
