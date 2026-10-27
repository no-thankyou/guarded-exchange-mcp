# guarded-exchange-mcp

An MCP server that gives AI agents guarded access to a crypto exchange (Bybit V5): least-privilege permissions, risk limits, human confirmation of risky actions in a channel the model does not control, tenant isolation and a tamper-evident audit log.

> Status: early skeleton. Only the read-only `get_ticker` tool works end to end. Requirements live in [`docs/SPEC.md`](docs/SPEC.md), the design in [`docs/PLAN.md`](docs/PLAN.md), work items in [`docs/tickets/`](docs/tickets/).

## How to run the skeleton

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv installs it if missing).

```bash
uv sync
uv run guarded-exchange serve          # MCP over stdio; JSON logs go to stderr
```

The server talks to Bybit **Demo Trading** public market data (`https://api-demo.bybit.com`); no API keys are needed for `get_ticker`.

Connect it to Claude Code:

```bash
claude mcp add guarded-exchange -- uv run --directory "$(pwd)" guarded-exchange serve
```

Or inspect it with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run --directory "$(pwd)" guarded-exchange serve
```

Example call: `get_ticker {"symbol": "BTCUSDT", "category": "linear"}` returns a compact structured result (decimals as strings, absent fields omitted):

```json
{"symbol": "BTCUSDT", "category": "linear", "last_price": "80610.1", "bid_price": "80610", "ask_price": "80610.1",
 "change_24h_pct": "0.7311", "mark_price": "80610.84", "funding_rate_pct": "0.01", "observed_at": "2026-10-27T18:57:44.688Z", "...": "..."}
```

Errors are tool results with `isError: true` and a stable shape `{code, message, next_step, details?, untrusted_text?}`; text that comes from the exchange is returned only in `untrusted_text`.

### Configuration

Environment variables (or a `.env` file in the working directory):

| Variable | Default | Meaning |
|---|---|---|
| `GX_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `GX_BYBIT_BASE_URL` | `https://api-demo.bybit.com` | Bybit REST base URL |
| `GX_BYBIT_CONNECT_TIMEOUT_S` / `GX_BYBIT_READ_TIMEOUT_S` | `3` / `5` | HTTP timeouts, seconds |
| `GX_BYBIT_GET_MAX_ATTEMPTS` | `3` | attempts for read (GET) requests; writes are never retried automatically |
| `GX_BYBIT_GET_BACKOFF_BASE_S` | `0.2` | exponential backoff base, seconds |

## Development

```bash
uv run ruff check . && uv run ruff format --check .   # lint + format
uv run mypy                                           # mypy --strict
uv run lint-imports                                   # architecture (layer) contracts
uv run pytest --cov                                   # tests; no external network access
uv run pre-commit install                             # optional: run the checks on commit
```

Tests never reach external APIs: the Bybit adapter is tested on recorded responses via `httpx2.MockTransport`, the stdio end-to-end test points the real server at a loopback stub, and an autouse pytest guard fails any connection or DNS lookup to a non-loopback host.

Requirement-to-test traceability is generated from `@pytest.mark.req(...)` markers:

```bash
uv run pytest --collect-only -q --traceability=docs/traceability.md
```

### Architecture

```
src/guarded_exchange/
  domain/          pure Python: value objects (Decimal money), rules, domain exceptions with error codes
  application/     use cases + ports (typing.Protocol), e.g. GetTicker -> ExchangeClient
  infrastructure/  adapters: bybit/ (httpx2 client, DTO mapper, retCode mapping), logging
  interface/       entrypoints: mcp/ (MCPServer, thin tools, error mapping), cli/ (typer)
  config.py        pydantic-settings
  bootstrap.py     composition root: the only place that reads config and wires adapters
```

Dependencies point inwards; `import-linter` enforces it in CI.
