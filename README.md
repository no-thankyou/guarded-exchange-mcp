# guarded-exchange-mcp

An MCP server that gives AI agents guarded access to a crypto exchange (Bybit V5): least-privilege permissions, risk limits, human confirmation of risky actions in a channel the model does not control, tenant isolation and a tamper-evident audit log.

> Status: early skeleton. Read-only tools work end to end: market data (`get_ticker`, `get_orderbook`, `get_candles`, `get_instrument_rules`), the user's account (`get_balances`, `get_positions`, `get_open_orders`), its order and trade history (`get_order_history`, `get_trades`) and `get_my_policy`; the first trading tool, `place_order`, works on the test environment with risk limits, a journal, idempotency keys and reconnaissance of unknown outcomes, but without human confirmation yet (an order whose policy requires confirmation is refused); local storage (SQLite, master key, local user, append-only journal) is initialised by `guarded-exchange init`; the policy (admin ceiling, user tightening) and exchange API keys (encrypted, permission-checked) are managed from the CLI; a remote multi-user mode serves the same tools over Streamable HTTP with per-user bearer tokens. Requirements live in [`docs/SPEC.md`](docs/SPEC.md), the design in [`docs/PLAN.md`](docs/PLAN.md), work items in [`docs/tickets/`](docs/tickets/).

## How to run the skeleton

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv installs it if missing). Linux or macOS: `init` serialises itself with `fcntl.flock`, so Windows is out of scope (use the Docker image or WSL).

```bash
uv sync
uv run guarded-exchange init           # once: database, master key, local user (safe to re-run)
uv run guarded-exchange serve          # MCP over stdio; JSON logs go to stderr
```

`init` creates (or migrates) the SQLite database, generates a 256-bit master key in a file with mode `0600` in a directory **outside** the database directory, creates the local user (active, test environment) and records both in the append-only journal. Re-running it changes nothing and never replaces the key; if the installation exists but the key file is gone, `init` refuses instead of generating a new one. **Back up the master key**: without it, secrets stored later cannot be decrypted.

`serve` never migrates (several MCP clients may start server processes at once) and opens the database read-only for its checks. On start it checks SQLite >= 3.35, that the schema revision is exactly the one this version expects, that the append-only triggers on `audit_log` (no UPDATE, no DELETE, no INSERT OR REPLACE over an existing record) are present and exactly as shipped, that the master key file is a regular file (not a symlink) with mode `0600` holding a valid 256-bit key, and that the local user exists and is active; otherwise it exits with code 2 and a reason on stderr, e.g. ``guarded-exchange: cannot start: database not found at ...; run `guarded-exchange init` first``.

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

Account tools (read-only; signed with the calling user's own exchange key, which never leaves the server):

| Tool | Arguments | Returns |
|---|---|---|
| `get_balances` | - | totals in USD (equity, wallet balance, available for new orders) and each asset held, largest USD value first (at most 50, the rest counted in `omitted_coins`) |
| `get_positions` | `symbol` (optional), `limit` 1-50 (default 20) | open USDT perpetual positions: side, size, entry/mark/liquidation price, unrealised PnL, leverage, TP/SL; `has_more`, `omitted` |
| `get_open_orders` | `category`, `symbol` (optional), `limit` 1-50 (default 20) | open orders (plain and conditional), newest first, each with `origin`: `system` (client order id with our `gx-` prefix) or `external` (placed manually or by another bot); `qty` in `qty_unit` (`base`, `quote` for a spot market buy sized in USDT, or `unknown`); `has_more`, `omitted` |
| `get_order_history` | `category`, `symbol` (optional), `start_time` / `end_time` (optional, ISO 8601), `cursor` (optional), `limit` 1-50 (default 20) | closed orders (filled, cancelled, partially filled then cancelled, rejected, triggered, deactivated) over at most 7 days, newest first, with `origin`, `updated_at` and the reject reason in `untrusted_text`; `period_start`/`period_end`, `has_more`, `next_cursor`, `omitted` |
| `get_trades` | same as `get_order_history` | executions (fills) over at most 7 days, newest first: `kind` (`trade`, `liquidation`, `auto_deleverage`, `funding`, `settlement`, ...), price, qty (base), value, fee and its currency, maker/taker, `closed_size` for perpetuals, `origin` of the filled order; `period_start`/`period_end`, `has_more`, `next_cursor`, `omitted` |

An order's client order id is free text whoever placed the order chose, so it is returned only in the order's `untrusted_text` (FR-41); every other string in these answers is a number, a UTC time, one of our enum values or an identifier checked against a strict pattern. The `gx-` prefix is a fast filter, not a proof: anyone with access to the account can use it; checking against the journal of orders this server sent comes with trading. Without a stored key the tools answer `exchange_credentials_invalid` with `next_step` "add a key with `guarded-exchange keys add`, outside the chat"; an account in hedge mode gets `unsupported_position_mode` (v1 is one-way only); an exchange rate limit (`10006`) gets `rate_limited` with `retry_after_s` from `X-Bapi-Limit-Reset-Timestamp`. A read-only key is enough for all three tools.

Without `symbol`, linear positions and orders are read with `settleCoin=USDT`, so USDC-settled perpetuals are not in the answer. Rows v1 cannot represent (a symbol outside `^[A-Z0-9]{2,30}$`, e.g. a dated future `BTCUSDT-26DEC25`) are left out and counted in `omitted`; the rest of the page is still returned, `omitted` counts only rows of the requested page (the first `limit`), while `has_more` also counts rows beyond it, omitted or not. A spot market order's `qty_unit` comes from the exchange's `marketUnit` and is never guessed as base. A balance holding an asset code that cannot be shown as plain text gets `exchange_rejected` with `details.reason: "unsupported_coin_code"` and "Do not retry". `retry_after_s` is at least 1 s and is measured from the exchange's own response `time`, not the local clock. A rate limit while adding a key or re-checking its permissions counts as "cannot verify right now" (like an unavailable exchange), not as a rejected key; before trading, a stale key whose re-check hits the rate limit is refused with `rate_limited` (`retry_after_s`, `details.reason: "permissions_recheck_failed"`).

History follows Bybit's limits (`/v5/order/history`, `/v5/execution/list`, checked 2026-11-03): at most 7 days per call and no older than 730 days; no bounds means the last 7 days, only `start_time` the 7 days after it, only `end_time` the 7 days before it, an end in the future is cut to now, and both bounds are always sent to the exchange so the answer's `period_start`/`period_end` is exactly what was read. A period outside these rules is `invalid_params` before anything is sent, with a `next_step` that names a valid 7-day window. What the exchange keeps: cancelled, rejected and deactivated orders without fills only for 24 hours, orders older than 7 days only if they had fills. Pagination: `next_cursor` is this server's opaque cursor (URL-safe base64 of the tool, category, symbol, period and the exchange's own `nextPageCursor`), so the next page is read with exactly the first page's parameters; pass it with the same `category` and no `symbol`/period. The exchange cursor goes back verbatim (Bybit returns it percent-encoded; its SDK sends it unencoded), and only unreserved characters and `%XX` escapes are let into the signed query, so a forged cursor cannot add parameters (`invalid_params`). A page shorter than `limit` is the last one even if Bybit still sends a cursor; a full page may be followed by an empty one. Rows v1 cannot represent (dated futures, hedge-mode orders, an unknown fill side) are counted in `omitted`. Free text - the client order id and the exchange's reject reason - is returned only in `untrusted_text`. `origin` is still the `gx-` prefix check; reconciling it against the journal of sent orders is ticket 012.

Errors are tool results with `isError: true` and a stable shape `{code, message, next_step, retry_after_s?, details?, untrusted_text?}`; unexpected server failures return `code: "internal_error"` with a `request_id` and no internals; text that comes from the exchange is returned only in `untrusted_text`. Arguments that do not match a tool's input schema (missing, wrong type, value outside the enum or range) return `code: "invalid_params"` with `details: {field, reason}`; the rejected values are never echoed back.

### Policy

Every user has an effective policy = the stricter, field by field, of the admin's **ceiling** and the user's own settings: permission profile (`read_only` or `trade`), allowed instruments, max order size and leverage, slippage, which actions need confirmation, confirmation lifetime, trust in the AI client's confirmation UI, limits on unconfirmed and pending actions. A new user is `read_only` with an empty allowlist on the test environment; the default limits are 1000 USDT per order and x10 leverage on testnet, 50 USDT and x3 on mainnet.

The agent can only **read** its policy (`get_my_policy`); there is no MCP tool that changes it. Changes go through the CLI:

```bash
uv run guarded-exchange policy show                                   # ceiling, your settings, effective
uv run guarded-exchange admin policy set-ceiling --profile trade --allow linear:BTCUSDT --allow spot:ETHUSDT
uv run guarded-exchange policy tighten --max-leverage 3 --confirm-above-notional-usdt 100
uv run guarded-exchange policy tighten --max-leverage 20              # refused: would weaken max_leverage
uv run guarded-exchange admin policy reset-overrides --user local --field profile   # asks, then undoes it
```

`admin policy set-ceiling` (optionally `--user NAME`) may raise or lower the ceiling within hard bounds (slippage <= 3%, confirmation lifetime <= 600 s); `policy tighten` refuses any value looser than the current effective one and names the fields. The user cannot undo their own tightening (raising the ceiling does not lift a stricter user setting either); the admin can: `admin policy reset-overrides --user NAME` removes all of the user's own settings, or only those named with `--field` (repeatable), so those fields follow the ceiling again. It lists what it will remove, asks for confirmation and then removes only the listed settings; with no answer on input (closed stdin) it refuses unless `--yes` is given. A piped `y` counts as an answer. A user without matching settings is left as is and nothing is journaled. If the admin lowers the ceiling below a user's setting, the ceiling applies. All three write to the user's journal (`policy_ceiling_set`, `policy_tightened`, `policy_overrides_reset` with the removed values and the effective values before and after). `tools/list` shows a user only the tools their profile allows (the result is `cacheScope: private`), and every `tools/call` is checked again, so a hidden tool called directly gets `permission_denied`.

### Trading: `place_order` (test environment only)

`place_order {symbol, category, side, order_type, qty, price?, reduce_only?, idempotency_key?}` places a market or limit order on spot or USDT perpetuals. `qty` and `price` are JSON **strings** (`"0.001"`, `"68797.6"`); a JSON number gets `invalid_params`, so an amount never passes through a binary float. Before anything is sent:

- the instrument's rules apply: `qty` is rounded **down** to the quantity step (the answer shows `requested_qty`, `qty` and `qty_adjustment`), a limit price off the tick is `instrument_rule_violation` with the two nearest valid prices, below the minimum quantity or order value is refused, and a limit price outside the exchange's current price band (`GET /v5/market/price-limit`) is refused too, because Bybit would silently move a perpetual order to the band edge;
- a **market** order is sent as `Limit IOC` at the worst acceptable price: best ask (buy) or bid (sell) +/- the policy's `slippage_pct` (hard maximum 3%), rounded to the tick on the safe side and kept inside the price band; what cannot execute within that bound is cancelled by the exchange;
- risk-increasing orders must be on the allowlist, within `max_order_notional_usdt` and, on perpetuals, the symbol's current leverage must not exceed `max_leverage`; a refusal names the limit and the allowed value (`limit_exceeded`). The sum checked (`notional_usdt`, also against `confirm_above_notional_usdt`) is the most the order can be worth: qty after rounding x `notional_price`, which is the limit price, the worst price of a market buy, or the best bid for a market sell (it can fill up to the bid, never above, while its own bound is the lowest acceptable price); the exchange's minimum order value is checked on qty x the order price;
- `reduce_only` (perpetuals) can only shrink a position: it needs an opposite open position (`position_not_found` otherwise), is cut to the position size, and is not limited by order size or allowlist, so a position on an instrument removed from the allowlist (or being delisted) stays closable;
- if the policy requires confirming the order (`confirm_actions` contains `place_order`, the default, or the order is at least `confirm_above_notional_usdt`), the call is refused with `confirmation_channel_unavailable` and nothing is sent: human confirmation arrives with the next tickets. To trade without confirmation the admin must say so: `uv run guarded-exchange admin policy set-ceiling --profile trade --allow linear:BTCUSDT --confirm none`.

Each order gets a fresh client order id `gx-<action id>` (Bybit `orderLinkId`, at most 36 characters). The journal records every attempt of a known user: a refusal (`order_refused`, also when refused before the policy decision: read-only profile, another environment, the key check; invalid input is not journaled) or the attempt (`order_attempt`, committed before the request leaves) and the exchange's answer (`order_outcome`: `accepted`, `rejected`, `unknown`, or `not_sent` if no connection could be made), with the client's self-description (`clientInfo`, not verified). The order is read back right after placing: the answer's `price` is the order's price on the exchange and `filled_qty` its execution; if the exchange does not show the order yet (or reports no executed quantity), `filled_qty` is `"unknown"` (never 0) with a hint to check `get_open_orders`; once the exchange has accepted the order, nothing after that (reading it back, writing the journal) turns into an error: the answer is "accepted, filled unknown" and the failure is logged. Mainnet trading is not available in this version.

**Repeats never place a second order.** Pass a fresh `idempotency_key` (1-64 letters, digits, `_ . : -`, e.g. a UUID) with every new order. Repeating the call with the same key and the same arguments returns the first call's answer (`result_source: "replay"`, or the same error marked `details.replayed: "true"`) without touching the exchange; while the first call is still running, a repeat gets `outcome_unknown` with `details.reason: "in_progress"` and sends nothing; the same key with other arguments is `idempotency_conflict`. Keys live 24 h and work across all clients of the user (they are claimed in the database with one atomic `INSERT ... ON CONFLICT DO UPDATE ... WHERE expired`). Decimal spellings do not matter (`"0.10"` = `"0.1"`). Without a key there is a safety net: an identical risk-increasing order within 30 s of the previous one's answer (10 min after an unknown outcome) is a repeat, also when the previous call came with a key from another client; calls with a key never consult this net, so a ladder of identical orders with different keys works; reduce-only orders are never deduplicated without a key (repeating a risk-reducing action must keep working). After `outcome_unknown` without a key the hint says so: a later repeat places a new order, check `get_open_orders` / `get_order_history` first. Every repeat is journaled as `order_repeat`. An accepted order, an unknown outcome and, for an explicit key, a definite refusal (limits, confirmation, the exchange rejecting it) are kept: a new attempt needs a new key. A failure where nothing was decided or created (exchange or key unavailable, rate limit, invalid input, cancelled call) releases the key, so retrying with it works.

**Unknown outcomes are looked up, never resent.** A timeout or broken connection after sending, a 5xx, an exchange-side internal error or "being processed" answer (e.g. spot `170035`), a duplicate client id, a bug while sending, or any `retCode` outside the documented refusal families of `order/create` means the order may exist. The server then looks it up by its client order id (`GET /v5/order/realtime?orderLinkId=`, then `GET /v5/order/history?orderLinkId=`): found means `accepted` with `result_source: "reconnaissance"`; not found anywhere is still `outcome_unknown` (never "failed": the exchange's lists lag behind) with the client order id, `details.reconnaissance` (`not_found` or `failed`) and a hint to check `get_open_orders` / `get_order_history`. Sending has an overall deadline (`GX_ORDER_SEND_DEADLINE_S`, 15 s) on top of the HTTP timeouts, which only bound the silence between chunks of a slowly dripping answer.

### Exchange API keys

Keys are typed into the CLI (no echo), never passed through the AI client or its context (FR-30):

```bash
uv run guarded-exchange keys add                 # prompts for the Bybit API key and secret
uv run guarded-exchange keys show                # id, status, last checked permissions; never the key
uv run guarded-exchange keys replace             # new key; the old one is soft-deleted
uv run guarded-exchange keys delete
```

On `add`/`replace` the server asks Bybit (`GET /v5/user/query-api`, signed with HMAC-SHA256) what the key may do and accepts it only if **every** permission is on an explicit allowlist: `ContractTrade.Order`, `ContractTrade.Position`, `Spot.SpotTrade` (`PERMISSION_ALLOWLIST` in `domain/credentials.py`). Anything else - withdrawals, transfers (`Wallet.*`), options, convert, `Derivatives.DerivativesTrade`, or a permission Bybit adds tomorrow - rejects the key; so does an expired key or one Bybit refuses. A read-only key is accepted and caps the user's profile at `read_only` whatever the policy ceiling says; the allowlist still applies to it, so a read-only key that also lists other permission groups is rejected too - create the key with only the permissions you need. A key not bound to an IP address is accepted with a warning. When creating a Bybit key for this server, tick only Contract "Orders" and "Positions" and Spot "Trade" (or "Read-Only"). If the permissions cannot be checked at all (exchange unreachable, endpoint missing), the key is refused; on the test environment only, the admin may accept it with `keys add --admin-attest` after confirming it has no withdrawal or transfer permission (journaled), and never on mainnet.

Keys are stored encrypted with AES-256-GCM under a sub-key derived from the master key (HKDF), with the credential id, user id, exchange and environment as associated data: a ciphertext copied into another user's row (or relabelled as mainnet) does not decrypt. The key and secret never reach the logs, the journal or command output; the journal records key ids, permission names and verdicts. Permissions are re-checked when the server starts and, before any state-changing action, if the last check is over an hour old; if they have grown beyond the allowlist, trading is blocked with `credentials_permissions_changed` until the key is fixed or replaced. One active key per user and exchange environment; the environment comes from `GX_BYBIT_BASE_URL` and must match the user's.

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
| `GX_BYBIT_BASE_URL` | `https://api-demo.bybit.com` | Bybit REST base URL: only `https://api-demo.bybit.com`, `https://api-testnet.bybit.com` (test environment), `https://api.bybit.com`, `https://api.bytick.com` (mainnet), no path; anything else stops the server (signed requests never go to an unknown host) |
| `GX_TEST_ALLOW_LOOPBACK_BYBIT_URL` | `false` | tests only: also accept a loopback stub (`http://127.0.0.1:PORT`) as the exchange, treated as the test environment |
| `GX_BYBIT_CONNECT_TIMEOUT_S` / `GX_BYBIT_READ_TIMEOUT_S` | `3` / `5` | HTTP timeouts, seconds |
| `GX_BYBIT_GET_MAX_ATTEMPTS` | `3` | attempts for read (GET) requests; writes are never retried automatically |
| `GX_BYBIT_GET_BACKOFF_BASE_S` | `0.2` | exponential backoff base, seconds |
| `GX_ORDER_FILL_READ_DEADLINE_S` | `5` | bound of reading an accepted order back, seconds; after it the fill is `unknown` |
| `GX_ORDER_SEND_DEADLINE_S` | `15` | overall bound of one order request, seconds (up to 60); after it the outcome is unknown and the order is looked up by its client id |
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

Storage tests use real SQLite files in a temporary directory, including a two-process write test. Authentication is tested over the SDK's real Streamable HTTP stack in-process (`httpx2.ASGITransport`, no sockets). Tests never reach external APIs: the Bybit adapter is tested on recorded responses via `httpx2.MockTransport`, request signing is checked against HMAC vectors computed independently with OpenSSL (`tests/fixtures/bybit/signature_vectors.json`), the stdio end-to-end test points the real server at a loopback stub (`GX_TEST_ALLOW_LOOPBACK_BYBIT_URL=true`), and an autouse pytest guard fails any connection or DNS lookup to a non-loopback host.

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
                   ReadPolicy / SetCeiling / TightenPolicy / ResetOverrides / AuthorizeAccess (who may call what),
                   CreateUser / IssueAccessToken / RevokeAccessToken, AuthenticateBearerToken -> Authenticator,
                   Add/Replace/DeleteCredential, RecheckCredential, RequireTradingCredential
                   -> KeyPermissionsReader, CredentialCipher, CredentialRepository,
                   GetBalances / GetPositions / GetOpenOrders -> ReadCredential, AccountReader,
                   GetOrderHistory / GetTrades -> ReadCredential, AccountHistoryReader,
                   SubmitPlaceOrder -> RequireTradingCredential, InstrumentRulesCache, TradingExchange,
                   IdempotencyRepository (claims of repeated calls, answers stored for replays)
  infrastructure/  adapters: bybit/ (httpx2 client, request signing, known hosts, DTO mapper, retCode
                   mapping), db/ (SQLAlchemy Core repositories, unit of work, Alembic migrations),
                   crypto (HKDF keyring, AES-256-GCM), master key file, clock, logging
  interface/       entrypoints: mcp/ (GuardedMCPServer: per-principal tool filter and checks, thin tools,
                   error mapping; http.py: bearer TokenVerifier, Streamable HTTP app), cli/ (typer)
  config.py        pydantic-settings
  bootstrap.py     composition root: the only place that reads config and wires adapters
```

Dependencies point inwards; `import-linter` enforces it in CI.
