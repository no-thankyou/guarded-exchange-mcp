# guarded-exchange-mcp

An MCP server that gives AI agents guarded access to a crypto exchange (Bybit V5): least-privilege permissions, risk limits, human confirmation of risky actions in a channel the model does not control, tenant isolation and a tamper-evident audit log.

> Status: early skeleton. Read-only tools `get_ticker` and `get_my_policy` work end to end; local storage (SQLite, master key, local user, append-only journal) is initialised by `guarded-exchange init`; the policy (admin ceiling, user tightening) is managed from the CLI. Requirements live in [`docs/SPEC.md`](docs/SPEC.md), the design in [`docs/PLAN.md`](docs/PLAN.md), work items in [`docs/tickets/`](docs/tickets/).

## How to run the skeleton

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv installs it if missing). Linux or macOS: `init` serialises itself with `fcntl.flock`, so Windows is out of scope (use the Docker image or WSL).

```bash
uv sync
uv run guarded-exchange init           # once: database, master key, local user (safe to re-run)
uv run guarded-exchange serve          # MCP over stdio; JSON logs go to stderr
```

`init` creates (or migrates) the SQLite database, generates a 256-bit master key in a file with mode `0600` in a directory **outside** the database directory, creates the local user (active, test environment) and records both in the append-only journal. Re-running it changes nothing and never replaces the key; if the installation exists but the key file is gone, `init` refuses instead of generating a new one. **Back up the master key**: without it, secrets stored later cannot be decrypted.

`serve` never migrates (several MCP clients may start server processes at once) and opens the database read-only for its checks. On start it checks SQLite >= 3.35, that the schema revision is exactly the one this version expects, that the append-only triggers on `audit_log` (no UPDATE, no DELETE, no INSERT OR REPLACE over an existing record) are present and exactly as shipped and that the local user exists and is active; otherwise it exits with code 2 and a reason on stderr, e.g. ``guarded-exchange: cannot start: database not found at ...; run `guarded-exchange init` first``.

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

Errors are tool results with `isError: true` and a stable shape `{code, message, next_step, retry_after_s?, details?, untrusted_text?}`; unexpected server failures return `code: "internal_error"` with a `request_id` and no internals; text that comes from the exchange is returned only in `untrusted_text`.

### Policy

Every user has an effective policy = the stricter, field by field, of the admin's **ceiling** and the user's own settings: permission profile (`read_only` or `trade`), allowed instruments, max order size and leverage, slippage, which actions need confirmation, confirmation lifetime, trust in the AI client's confirmation UI, limits on unconfirmed and pending actions. A new user is `read_only` with an empty allowlist on the test environment; the default limits are 1000 USDT per order and x10 leverage on testnet, 50 USDT and x3 on mainnet.

The agent can only **read** its policy (`get_my_policy`); there is no MCP tool that changes it. Changes go through the CLI:

```bash
uv run guarded-exchange policy show                                   # ceiling, your settings, effective
uv run guarded-exchange admin policy set-ceiling --profile trade --allow linear:BTCUSDT --allow spot:ETHUSDT
uv run guarded-exchange policy tighten --max-leverage 3 --confirm-above-notional-usdt 100
uv run guarded-exchange policy tighten --max-leverage 20              # refused: would weaken max_leverage
```

`admin policy set-ceiling` (optionally `--user NAME`) may raise or lower the ceiling within hard bounds (slippage <= 3%, confirmation lifetime <= 600 s); `policy tighten` refuses any value looser than the current effective one and names the fields. If the admin lowers the ceiling below a user's setting, the ceiling applies. Both write to the user's journal. `tools/list` shows a user only the tools their profile allows (the result is `cacheScope: private`), and every `tools/call` is checked again, so a hidden tool called directly gets `permission_denied`.

### Configuration

Environment variables with the `GX_` prefix. A dotenv file is read only when given explicitly: `guarded-exchange serve --env-file path/to/gx.env` or `GX_ENV_FILE=path/to/gx.env` (the option wins). A `.env` in the working directory is deliberately ignored: MCP clients start the server from arbitrary directories. A missing explicit file stops the server with an error.

| Variable | Default | Meaning |
|---|---|---|
| `GX_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `GX_BYBIT_BASE_URL` | `https://api-demo.bybit.com` | Bybit REST base URL |
| `GX_BYBIT_CONNECT_TIMEOUT_S` / `GX_BYBIT_READ_TIMEOUT_S` | `3` / `5` | HTTP timeouts, seconds |
| `GX_BYBIT_GET_MAX_ATTEMPTS` | `3` | attempts for read (GET) requests; writes are never retried automatically |
| `GX_BYBIT_GET_BACKOFF_BASE_S` | `0.2` | exponential backoff base, seconds |
| `GX_DB_PATH` | `$XDG_DATA_HOME/guarded-exchange/guarded-exchange.db` (`~/.local/share/...`) | SQLite database file; absolute path |
| `GX_MASTER_KEY_FILE` | `$XDG_CONFIG_HOME/guarded-exchange/master.key` (`~/.config/...`) | master key file; absolute, must be outside the database directory |
| `GX_LOCAL_USER` | `local` | name of the local-mode user (the only principal in local mode) |
| `GX_SQLITE_BUSY_TIMEOUT_MS` | `5000` | how long a writer waits for another process's write lock |

## Development

```bash
uv run ruff check . && uv run ruff format --check .   # lint + format
uv run mypy                                           # mypy --strict
uv run lint-imports                                   # architecture (layer) contracts
uv run pytest --cov                                   # tests; no external network access
uv run pre-commit install                             # optional: run the checks on commit
```

Storage tests use real SQLite files in a temporary directory, including a two-process write test. Tests never reach external APIs: the Bybit adapter is tested on recorded responses via `httpx2.MockTransport`, the stdio end-to-end test points the real server at a loopback stub, and an autouse pytest guard fails any connection or DNS lookup to a non-loopback host.

Requirement-to-test traceability is generated from `@pytest.mark.req(...)` markers:

```bash
uv run pytest --collect-only -q --traceability=docs/traceability.md
```

### Architecture

```
src/guarded_exchange/
  domain/          pure Python: value objects (Decimal money), rules, domain exceptions with error codes
  application/     use cases + ports (typing.Protocol), e.g. GetTicker -> ExchangeClient,
                   InitializeInstallation -> UnitOfWorkFactory, Clock, IdGenerator, MasterKeyStore,
                   ReadPolicy / SetCeiling / TightenPolicy / AuthorizeAccess (who may call what)
  infrastructure/  adapters: bybit/ (httpx2 client, DTO mapper, retCode mapping), db/ (SQLAlchemy Core
                   repositories, unit of work, Alembic migrations), master key file, clock, logging
  interface/       entrypoints: mcp/ (MCPServer, thin tools, error mapping, per-principal tool filter),
                   cli/ (typer)
  config.py        pydantic-settings
  bootstrap.py     composition root: the only place that reads config and wires adapters
```

Dependencies point inwards; `import-linter` enforces it in CI.
