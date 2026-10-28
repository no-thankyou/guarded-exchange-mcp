# guarded-exchange-mcp

An MCP server that gives AI agents guarded access to a crypto exchange (Bybit V5): least-privilege permissions, risk limits, human confirmation of risky actions in a channel the model does not control, tenant isolation and a tamper-evident audit log.

> Status: early skeleton. Read-only tools work end to end: market data (`get_ticker`, `get_orderbook`, `get_candles`, `get_instrument_rules`) and `get_my_policy`; local storage (SQLite, master key, local user, append-only journal) is initialised by `guarded-exchange init`; the policy (admin ceiling, user tightening) is managed from the CLI; a remote multi-user mode serves the same tools over Streamable HTTP with per-user bearer tokens. Requirements live in [`docs/SPEC.md`](docs/SPEC.md), the design in [`docs/PLAN.md`](docs/PLAN.md), work items in [`docs/tickets/`](docs/tickets/).

## How to run the skeleton

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv installs it if missing). Linux or macOS: `init` serialises itself with `fcntl.flock`, so Windows is out of scope (use the Docker image or WSL).

```bash
uv sync
uv run guarded-exchange init           # once: database, master key, local user (safe to re-run)
uv run guarded-exchange serve          # MCP over stdio; JSON logs go to stderr
```

`init` creates (or migrates) the SQLite database, generates a 256-bit master key in a file with mode `0600` in a directory **outside** the database directory, creates the local user (active, test environment) and records both in the append-only journal. Re-running it changes nothing and never replaces the key; if the installation exists but the key file is gone, `init` refuses instead of generating a new one. **Back up the master key**: without it, secrets stored later cannot be decrypted.

`serve` never migrates (several MCP clients may start server processes at once) and opens the database read-only for its checks. On start it checks SQLite >= 3.35, that the schema revision is exactly the one this version expects, that the append-only triggers on `audit_log` (no UPDATE, no DELETE, no INSERT OR REPLACE over an existing record) are present and exactly as shipped and that the local user exists and is active; otherwise it exits with code 2 and a reason on stderr, e.g. ``guarded-exchange: cannot start: database not found at ...; run `guarded-exchange init` first``.

The server talks to Bybit **Demo Trading** public market data (`https://api-demo.bybit.com`); no API keys are needed for the market data tools.

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

Market data tools (all read-only, public data, `symbol` like `BTCUSDT`, `category` is `spot` or `linear`):

| Tool | Arguments | Returns |
|---|---|---|
| `get_ticker` | `symbol`, `category` | last price, best bid/ask, 24h summary; mark price and funding for linear |
| `get_orderbook` | `symbol`, `category`, `depth` 1-50 (default 10) | best bids and asks as `[price, qty]` pairs |
| `get_candles` | `symbol`, `category`, `interval` (`1m`...`12h`, `1d`, `1w`, `1mo`), `limit` 1-200 (default 50) | OHLCV rows `[start_at, open, high, low, close, volume_base]`, oldest first |
| `get_instrument_rules` | `symbol`, `category` | tick size, qty step, min qty, min order value, max leverage (linear), status; USDT-quoted instruments only; cached in memory for `GX_INSTRUMENT_RULES_TTL_S` |

Errors are tool results with `isError: true` and a stable shape `{code, message, next_step, retry_after_s?, details?, untrusted_text?}`; unexpected server failures return `code: "internal_error"` with a `request_id` and no internals; text that comes from the exchange is returned only in `untrusted_text`. Arguments that do not match a tool's input schema (missing, wrong type, value outside the enum or range) return `code: "invalid_params"` with `details: {field, reason}`; the rejected values are never echoed back.

### Policy

Every user has an effective policy = the stricter, field by field, of the admin's **ceiling** and the user's own settings: permission profile (`read_only` or `trade`), allowed instruments, max order size and leverage, slippage, which actions need confirmation, confirmation lifetime, trust in the AI client's confirmation UI, limits on unconfirmed and pending actions. A new user is `read_only` with an empty allowlist on the test environment; the default limits are 1000 USDT per order and x10 leverage on testnet, 50 USDT and x3 on mainnet.

The agent can only **read** its policy (`get_my_policy`); there is no MCP tool that changes it. Changes go through the CLI:

```bash
uv run guarded-exchange policy show                                   # ceiling, your settings, effective
uv run guarded-exchange admin policy set-ceiling --profile trade --allow linear:BTCUSDT --allow spot:ETHUSDT
uv run guarded-exchange policy tighten --max-leverage 3 --confirm-above-notional-usdt 100
uv run guarded-exchange policy tighten --max-leverage 20              # refused: would weaken max_leverage
```

`admin policy set-ceiling` (optionally `--user NAME`) may raise or lower the ceiling within hard bounds (slippage <= 3%, confirmation lifetime <= 600 s); `policy tighten` refuses any value looser than the current effective one and names the fields. **Tightening is one-way:** there is no command to undo it, not even for the admin (raising the ceiling does not lift a stricter user setting); today the only way back is editing the `policies` table directly with SQL, so tighten deliberately. If the admin lowers the ceiling below a user's setting, the ceiling applies. Both write to the user's journal. `tools/list` shows a user only the tools their profile allows (the result is `cacheScope: private`), and every `tools/call` is checked again, so a hidden tool called directly gets `permission_denied`.

### Remote mode (Streamable HTTP + bearer tokens)

Several users can share one server over HTTP. The admin creates users and issues each one an opaque bearer token from the CLI; the server stores only its SHA-256 hash, so the token is printed once and cannot be shown again:

```bash
uv run guarded-exchange admin user create alice          # active, test environment, read-only policy
uv run guarded-exchange admin token issue --user alice   # prints the token id and the token (once)
uv run guarded-exchange serve --transport http           # http://127.0.0.1:8000/mcp
claude mcp add --transport http guarded-exchange http://127.0.0.1:8000/mcp --header "Authorization: Bearer <token>"
uv run guarded-exchange admin token revoke <token-id>    # the next request with it gets 401
uv run guarded-exchange admin user list
```

Every HTTP request is authenticated: no token, an unknown or revoked token, or a token of a revoked user gets `401` before any MCP processing; the token and the user's status are looked up in the database on each request, so revoking applies immediately. The caller's identity comes **only** from the token: `clientInfo`, other headers and tool arguments cannot change it, and each user sees only their own policy. Host and Origin headers are checked against `GX_HTTP_ALLOWED_HOSTS` / `GX_HTTP_ALLOWED_ORIGINS` (DNS rebinding protection, `421` otherwise). Issuing and revoking tokens is recorded in the user's journal by token id; the token itself never reaches the database, the journal or the logs. Every HTTP response carries an `X-Request-Id` that matches `http_request_id` in the server logs. Binding to a non-loopback address logs a warning: tokens travel in clear text without a TLS proxy. Revocation applies to the next request; a response stream that is already open runs to its end.

Static bearer tokens are a deliberate simplification of the MCP authorization spec (which recommends OAuth 2.1): the server sits behind a port (`Authenticator`), so an OAuth resource-server adapter can replace it without touching the use cases. Clients that only speak OAuth cannot connect yet. The server speaks plain HTTP: to expose it beyond the machine, bind `GX_HTTP_HOST` to another address **behind a TLS-terminating reverse proxy** and list the public host in `GX_HTTP_ALLOWED_HOSTS`. Revoking a whole user, mainnet and tenant isolation tests are the next tickets (027).

### Configuration

Environment variables with the `GX_` prefix. A dotenv file is read only when given explicitly: `guarded-exchange serve --env-file path/to/gx.env` or `GX_ENV_FILE=path/to/gx.env` (the option wins). A `.env` in the working directory is deliberately ignored: MCP clients start the server from arbitrary directories. A missing explicit file stops the server with an error.

| Variable | Default | Meaning |
|---|---|---|
| `GX_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `GX_BYBIT_BASE_URL` | `https://api-demo.bybit.com` | Bybit REST base URL |
| `GX_BYBIT_CONNECT_TIMEOUT_S` / `GX_BYBIT_READ_TIMEOUT_S` | `3` / `5` | HTTP timeouts, seconds |
| `GX_BYBIT_GET_MAX_ATTEMPTS` | `3` | attempts for read (GET) requests; writes are never retried automatically |
| `GX_BYBIT_GET_BACKOFF_BASE_S` | `0.2` | exponential backoff base, seconds |
| `GX_INSTRUMENT_RULES_TTL_S` | `600` | how long instrument rules are cached in memory, seconds (1-86400) |
| `GX_DB_PATH` | `$XDG_DATA_HOME/guarded-exchange/guarded-exchange.db` (`~/.local/share/...`) | SQLite database file; absolute path |
| `GX_MASTER_KEY_FILE` | `$XDG_CONFIG_HOME/guarded-exchange/master.key` (`~/.config/...`) | master key file; absolute, must be outside the database directory |
| `GX_LOCAL_USER` | `local` | name of the local-mode user (the only principal in local mode) |
| `GX_SQLITE_BUSY_TIMEOUT_MS` | `5000` | how long a writer waits for another process's write lock |
| `GX_HTTP_HOST` / `GX_HTTP_PORT` | `127.0.0.1` / `8000` | where `serve --transport http` listens |
| `GX_HTTP_ALLOWED_HOSTS` | `["127.0.0.1:*", "localhost:*", "[::1]:*"]` | JSON list of accepted `Host` headers (`host:*` = any port) |
| `GX_HTTP_ALLOWED_ORIGINS` | `["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]` | JSON list of accepted `Origin` headers (browsers) |

## Development

```bash
uv run ruff check . && uv run ruff format --check .   # lint + format
uv run mypy                                           # mypy --strict
uv run lint-imports                                   # architecture (layer) contracts
uv run pytest --cov                                   # tests; no external network access
uv run pre-commit install                             # optional: run the checks on commit
```

Storage tests use real SQLite files in a temporary directory, including a two-process write test. Authentication is tested over the SDK's real Streamable HTTP stack in-process (`httpx2.ASGITransport`, no sockets). Tests never reach external APIs: the Bybit adapter is tested on recorded responses via `httpx2.MockTransport`, the stdio end-to-end test points the real server at a loopback stub, and an autouse pytest guard fails any connection or DNS lookup to a non-loopback host.

Requirement-to-test traceability is generated from `@pytest.mark.req(...)` markers:

```bash
uv run pytest --collect-only -q --traceability=docs/traceability.md
```

### Architecture

```
src/guarded_exchange/
  domain/          pure Python: value objects (Decimal money), rules, domain exceptions with error codes
  application/     use cases + ports (typing.Protocol), e.g. GetTicker -> ExchangeClient, InstrumentRulesCache,
                   InitializeInstallation -> UnitOfWorkFactory, Clock, IdGenerator, MasterKeyStore,
                   ReadPolicy / SetCeiling / TightenPolicy / AuthorizeAccess (who may call what),
                   CreateUser / IssueAccessToken / RevokeAccessToken, AuthenticateBearerToken -> Authenticator
  infrastructure/  adapters: bybit/ (httpx2 client, DTO mapper, retCode mapping), db/ (SQLAlchemy Core
                   repositories, unit of work, Alembic migrations), master key file, clock, logging
  interface/       entrypoints: mcp/ (GuardedMCPServer: per-principal tool filter and checks, thin tools,
                   error mapping; http.py: bearer TokenVerifier, Streamable HTTP app), cli/ (typer)
  config.py        pydantic-settings
  bootstrap.py     composition root: the only place that reads config and wires adapters
```

Dependencies point inwards; `import-linter` enforces it in CI.
