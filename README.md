# guarded-exchange-mcp

A guarded [MCP](https://modelcontextprotocol.io) server that lets an AI agent trade on **Bybit V5 Demo Trading** (test environment only in v1) without letting the model decide alone: least-privilege permissions, risk limits, a kill switch, human confirmation in a channel the model does not control, idempotent order submission and an append-only audit journal.

> Portfolio / demo project. v1 is testnet-only, single process per database. Requirements: [`docs/SPEC.md`](docs/SPEC.md), design: [`docs/PLAN.md`](docs/PLAN.md), work items: [`docs/tickets/`](docs/tickets/).

## What it looks like

An illustrative session (Claude Code with the server connected, Bybit Demo account):

```text
> Set a stop loss 3% below the entry of my BTCUSDT position.

● get_positions
  BTCUSDT linear long 0.010 @ 67,200.0
● set_trading_stop(symbol=BTCUSDT, stop_loss=65184.0)
  status: pending, confirmation required
```

The server does not send the order. It parks the action and shows a form with its **own** plan, not the model's description:

```text
Confirm action?
  set_trading_stop  BTCUSDT (linear)
  stop loss: 65184.0  (-3.0% from entry 67200.0)
  [x] Approve        Enter to submit
```

After the human approves, the server executes it and writes the decision to the audit journal. If the form is declined, nothing is sent and the agent only sees `declined`. No MCP tool can approve or reject: the model cannot confirm its own action. The kill switch blocks all new orders until a human turns it off in the CLI.

## Quick start (one command, no Docker)

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 if missing), Linux or macOS. No exchange keys are needed for market data.

```bash
git clone <this repo> && cd guarded-exchange-mcp
uv run guarded-exchange init     # syncs dependencies, creates the database, master key and local user
```

Then connect a client (the server speaks MCP over stdio):

```bash
claude mcp add guarded-exchange -- uv run --directory "$(pwd)" guarded-exchange serve
```

Ask the agent for the `BTCUSDT` ticker. Trading needs a Bybit **demo** API key (`guarded-exchange keys add`, see [Exchange API keys](#exchange-api-keys)) and a policy with the `trade` profile (see [Policy](#policy)). Everything else is described in the reference sections below.

## How it works

```
Agent (untrusted model) --MCP--> interface/mcp  (thin tools, per-user tool filter)
                                      |
                                 application   (use cases; ONE trading path: ActionPath)
                                      |
                     domain (rules, Decimal money)     ports (Protocols)
                                      |
                           infrastructure: Bybit adapter, SQLite, crypto, clock

Human --CLI or client UI form--> confirmations (never an MCP tool)
```

- **Layers**: `domain / application / ports / infrastructure / interface`, dependencies point inwards, enforced by `import-linter` in CI (`uv run lint-imports`). Details in [Architecture](#architecture).
- **Single trading path (`ActionPath`)**: every state-changing tool (`place_order`, `cancel_order`, `amend_order`, `cancel_all_orders`, `set_trading_stop`, `close_position`, `set_leverage`) goes through the same sequence: kill switch, policy and limits, instrument rules, confirmation, journal, exchange. There is no second way to reach the exchange.
- **Idempotency and unknown outcomes**: every order carries an idempotency key; a repeated call returns the first answer, a different payload under the same key conflicts, and a timeout is resolved by looking the order up on the exchange instead of blindly resending.
- **Kill switch**: the agent can only turn it **on** (`activate_kill_switch`); turning it off is a CLI-only action. While on, every risk-increasing action is refused; reducing risk still works. See [Kill switch](#kill-switch).
- **Confirmations**: a risky action is parked as `pending`. The human approves it either in the client's own UI (MCP 2026-07-28 MRTR form / elicitation, on testnet) or via the CLI (`guarded-exchange confirm approve`, only if the admin enabled it). **No MCP tool approves anything.** See [Human confirmation](#human-confirmation).
- **Audit**: an append-only journal in SQLite (triggers forbid UPDATE and DELETE). Hash-chain tamper evidence is v2.

## Threat model

The model is **untrusted, always**. Everything the exchange returns (e.g. client order ids) is delivered as untrusted text and never interpreted.

| Threat | Mitigation |
|---|---|
| Model tries to confirm its own action (via tool calls or text) | No approving tool exists; confirmation comes only from the client UI form or the CLI. Adversarial test with a hostile "agent": [`tests/e2e/test_ac4_adversarial.py`](tests/e2e/test_ac4_adversarial.py) |
| Prompt injection through exchange data | Exchange text is returned as untrusted data; a risky action still needs human confirmation and passes policy limits |
| Model over-trades (size, leverage, instrument) | Per-user policy: admin ceiling, which the user may only tighten; `get_my_policy` is read-only for the agent |
| Duplicate orders from retries and timeouts | Idempotency keys and reconnaissance of unknown outcomes |
| Runaway agent | Kill switch the agent can enable but not disable |
| Leaked secrets | API keys encrypted at rest (AES-256-GCM, HKDF from a master key file with mode `0600`), never in logs, output or the journal |
| Switching to real money | Trading is testnet-only in v1 |

**Honest boundary.** An agent with **shell access under the same OS account** is outside the guarantees: it can read the master key, run the admin CLI (including enabling CLI approval) or call the exchange API directly. Docker is not a security boundary against such an agent, and `0600` files only protect against accidental reads. The AC-4 guarantee is a guarantee of the **MCP surface**: a model without shell (e.g. Claude Desktop, or Claude Code with deny rules for the data directory, `docker` and the CLI) cannot execute a confirmation-gated action without a human. This is why CLI approval is **off by default** (`GX_CONFIRM_CLI_ENABLED=false`). A custom client that auto-answers the UI form is also outside the guarantee.

## Supported clients

Checked by hand in spike 003 ([`spikes/003/README.md`](spikes/003/README.md)):

| Client | Local stdio | UI confirmation (MRTR form) | Status |
|---|---|---|---|
| Claude Code 2.1.312 (Linux, 2026-10-28) | yes | yes: form drawn, accept / decline / cancel distinguished (tick the box with Space, then Enter) | verified in the spike |
| Claude Desktop | per vendor docs | not verified (no Linux client); use the CLI channel | **not verified** |
| Cursor, VS Code Copilot, ChatGPT | not checked | not checked | **not verified** |
| `guarded-agent` (bundled console agent) | yes | yes, answered by the human in the console | covered by [`tests/e2e/test_agent_demo.py`](tests/e2e/test_agent_demo.py) |

A client that does not declare form elicitation gets no UI form: the action stays `pending` and can be approved in the CLI if the admin enabled it; otherwise the call fails closed. The UI channel works only on protocol 2026-07-28 (MRTR); there is no legacy push branch in v1.

## Limitations

- Testnet (Bybit Demo Trading) only; mainnet is not reachable in v1.
- **One server process per database** (SQLite); starting a second one is unsupported. Rate limits are only what the exchange reports (`rate_limited` with `retry_after_s`); there are no per-user buckets yet.
- Spot orders under the kill switch are refused entirely (a spot sell is not modelled as risk reduction).
- No limit on the number of actions that need no confirmation (FR-46): if an admin loosens confirmations, an agent can split a large order into small ones.
- The journal is append-only but **not tamper-evident** against someone with write access to the file (no HMAC chain or external checkpoints yet).
- External confirmation channel: CLI only, with the agent-with-shell boundary above.
- Remote mode (Streamable HTTP) uses static bearer tokens, not OAuth; two-user isolation and access revocation with pending cancellation are v2.
- Tested on Linux; Windows is out of scope (`fcntl.flock`).

## Roadmap (v2)

Deferred tickets (status `DEFERRED` in [`docs/tickets/`](docs/tickets/)):

- Telegram confirmation channel and pairing (018)
- Several stdio processes with leader election and fencing (019)
- Rate limits at three levels, including per-user buckets (022)
- Limit on actions without confirmation (023, FR-46)
- Journal HMAC chain and `audit verify` (024); checkpoints and journal reading tools (025)
- Access revocation, mainnet behind an explicit flag, two-user isolation (027)
- Postgres and docker-compose (028); Docker run, `doctor`, benchmark (032)
- Tool-description experiment: change descriptions and compare eval metrics (031)

## Evals

`evals/` is a tool-selection harness: 43 cases (Russian and English, 19 dangerous) run against the server **in-process on a fake exchange**; the **first state-changing decision** of the model is scored against a deterministic expectation (`call` with args, `clarify` or `refuse`). The run is repeated `--runs` times and stops at a hard cost budget (default $0.50).

```bash
uv run python -m evals --provider fake --runs 1                              # offline, $0, writes evals/report.md and .json
ANTHROPIC_API_KEY=... uv run python -m evals --provider anthropic --runs 3   # real model
OPENAI_API_KEY=...    uv run python -m evals --provider openai --runs 3
```

The `fake` provider is a scripted stand-in that checks the harness itself, not a model: its numbers say nothing about real tool selection.

### Results with a real model

`claude-haiku-5-5`, 43 cases x 3 runs (129 scored case-runs), 2026-11-09, cost $0.11 of the $0.50 budget.

| Metric | Rate | Hits / total |
|---|---|---|
| dangerous pass rate | **100%** | 57 / 57 |
| refuse or clarify accuracy | 100% | 84 / 84 |
| tool accuracy (when a call was expected) | 40% | 18 / 45 |
| overall pass rate | 79% | 102 / 129 |

The safety result is the one that matters here: in all 57 dangerous case-runs the model never made a state-changing call it should not have. The cost is caution: on legitimate trade requests (market/limit orders, closing half a position) this small model often stopped before the order (`no_call`, or the wrong tool for a partial close) in 27 of 45 case-runs. Larger models were not run: the harness has no price table for them yet. The tool-description before/after experiment is v2.


## Demo walkthrough

1. `uv run guarded-exchange init`, add a Bybit demo key (`uv run guarded-exchange keys add`), set the policy: `uv run guarded-exchange admin policy set-ceiling --profile trade --allow linear:BTCUSDT`.
2. Start Claude Code with the server connected (see Quick start) or run `uv run guarded-agent`.
3. Prompt: "Show my positions." (read-only call, no confirmation).
4. Prompt: "Set a stop loss 3% below the entry of my BTCUSDT position." The confirmation form shows what will be executed; tick the box (Space), Enter.
5. Show that the stop is set (ask for positions again).
6. Prompt: "Place a market buy of BTCUSDT" and decline the form: nothing is sent.
7. Ask the agent to activate the kill switch, then try a new order: refused with `kill_switch_active`. Turn it off only in the CLI: `uv run guarded-exchange kill-switch off`.

---

# Reference

## Setup details

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
- if the policy requires confirming the order (`confirm_actions` contains `place_order`, the default, or the order is at least `confirm_above_notional_usdt`), the order is parked for the human (see [Human confirmation](#human-confirmation)); if no confirmation channel is enabled it is refused with `confirmation_channel_unavailable` and nothing is sent. To trade without confirmation the admin must say so: `uv run guarded-exchange admin policy set-ceiling --profile trade --allow linear:BTCUSDT --confirm none`.

Each order gets a fresh client order id `gx-<action id>` (Bybit `orderLinkId`, at most 36 characters). The journal records every attempt of a known user: a refusal (`order_refused`, also when refused before the policy decision: read-only profile, another environment, the key check; invalid input is not journaled) or the attempt (`order_attempt`, committed before the request leaves) and the exchange's answer (`order_outcome`: `accepted`, `rejected`, `unknown`, or `not_sent` if no connection could be made), with the client's self-description (`clientInfo`, not verified). The order is read back right after placing: the answer's `price` is the order's price on the exchange and `filled_qty` its execution; if the exchange does not show the order yet (or reports no executed quantity), `filled_qty` is `"unknown"` (never 0) with a hint to check `get_open_orders`; once the exchange has accepted the order, nothing after that (reading it back, writing the journal) turns into an error: the answer is "accepted, filled unknown" and the failure is logged. Mainnet trading is not available in this version.

**Repeats never place a second order.** Pass a fresh `idempotency_key` (1-64 letters, digits, `_ . : -`, e.g. a UUID) with every new order. Repeating the call with the same key and the same arguments returns the first call's answer (`result_source: "replay"`, or the same error marked `details.replayed: "true"`) without touching the exchange; while the first call is still running, a repeat gets `outcome_unknown` with `details.reason: "in_progress"` and sends nothing; the same key with other arguments is `idempotency_conflict`. Keys live 24 h and work across all clients of the user (they are claimed in the database with one atomic `INSERT ... ON CONFLICT DO UPDATE ... WHERE expired`). Decimal spellings do not matter (`"0.10"` = `"0.1"`). Without a key there is a safety net: an identical risk-increasing order within 30 s of the previous one's answer (10 min after an unknown outcome) is a repeat, also when the previous call came with a key from another client; calls with a key never consult this net, so a ladder of identical orders with different keys works; reduce-only orders are never deduplicated without a key (repeating a risk-reducing action must keep working). After `outcome_unknown` without a key the hint says so: a later repeat places a new order, check `get_open_orders` / `get_order_history` first. Every repeat is journaled as `order_repeat`. An accepted order, an unknown outcome and, for an explicit key, a definite refusal (limits, confirmation, the exchange rejecting it) are kept: a new attempt needs a new key. A failure where nothing was decided or created (exchange or key unavailable, rate limit, invalid input, cancelled call) releases the key, so retrying with it works.

**Unknown outcomes are looked up, never resent.** A timeout or broken connection after sending, a 5xx, an exchange-side internal error or "being processed" answer (e.g. spot `170035`), a duplicate client id, a bug while sending, or any `retCode` outside the documented refusal families of `order/create` means the order may exist. The server then looks it up by its client order id (`GET /v5/order/realtime?orderLinkId=`, then `GET /v5/order/history?orderLinkId=`): found means `accepted` with `result_source: "reconnaissance"`; not found anywhere is still `outcome_unknown` (never "failed": the exchange's lists lag behind) with the client order id, `details.reconnaissance` (`not_found` or `failed`) and a hint to check `get_open_orders` / `get_order_history`. Sending has an overall deadline (`GX_ORDER_SEND_DEADLINE_S`, 15 s) on top of the HTTP timeouts, which only bound the silence between chunks of a slowly dripping answer.

### Trading: `cancel_order`, `amend_order`, `cancel_all_orders` (test environment only)

- `cancel_order {symbol, category, order_id, idempotency_key?}` cancels one open order by its exchange `order_id` (from `get_open_orders`).
- `amend_order {symbol, category, order_id, qty?, price?, idempotency_key?}` changes an open limit order: `qty` is the order's new **total** quantity (rounded down to the step, above what is already filled), `price` must be on the tick and inside the price band; only the fields that change are sent.
- `cancel_all_orders {symbol, category, idempotency_key?}` cancels the plain open orders on one instrument, at most 20 per call (`has_more: true` -> call again).

**Which orders are touched.** Only orders the call has just read and labelled itself: one order by its id, or a snapshot of the plain open orders on the instrument read with an explicit `orderFilter=Order`, then cancelled **one by one by id**. The server never calls Bybit's `cancel-all` endpoint, which on linear sweeps every kind of order unless filtered (a position's TP/SL included) and would also cancel orders that appeared after the snapshot. Conditional orders and a position's stop-loss / take-profit are never cancelled or amended here (TP/SL is `set_trading_stop`). External orders (placed by a person or another bot) can be cancelled and amended, as the spec allows: every answer and journal record marks each order `origin: system | external` and `cancel_all_orders` reports `external_count`. `origin` is judged by the `gx-` client id prefix, not yet verified against the journal.

**Risk (FR-43).** Cancelling always reduces risk, so it works under the kill switch and faces no limits. An amend increases risk when the new size is larger or the new price is worse (higher for a buy, lower for a sell); then the kill switch blocks it and the allowlist, `max_order_notional_usdt` (checked on the new `qty x price` of the whole order) and leverage limits apply. Amending a reduce-only order always reduces risk, and growing it is cut to the position. The confirmation rule applies by kind (`cancel_order`, `amend_order`, `cancel_all_orders` in `confirm_actions`; the value threshold applies to amends only) and fails closed like `place_order`.

**Accepted is not applied.** Bybit applies cancels and amends asynchronously. After an accepted request the order is read back: the answer says `cancelled` / `amended` only when the order shows it, otherwise `cancel_requested` / `amend_requested` (check again; never "failed"); `filled` if the order filled before the cancel took effect. After a timeout, a 5xx or an answer such as spot `170147`/`170212`/`170310`/`170311` or linear `110079` ("processing"), the request is **not resent**: the order is read by its id, and if it does not show the change the answer is `outcome_unknown` with `details.reconnaissance` and the order's current status. Repeating a cancel or the same amend is harmless, and the hint says so. "Order does not exist / already cancelled" (`110001`, `110008`, `110010`, `170142`, `170143`, `170213`) is `not_found`: nothing was changed; "Order has been locked" (`170144`) is a definite `exchange_rejected`. `order_closed`: the amend was accepted but the order filled or was cancelled first, without the new values. `cancel_all_orders` stops sending after the first unknown outcome or transient failure (the rest is `not_sent`) and settles the others with one read of the open orders; calling it again takes a **new** snapshot, so it also cancels orders placed in the meantime, external ones included. An amend is refused with `rate_limited` (`details.reason: order_change_in_progress`, nothing sent) while another change of the same order is in flight, unconfirmed or confirmed only after this call read the order (within 60 s): its plan would rest on stale values (e.g. an earlier accepted but not yet visible amend could let it pass the order value limit or the kill switch); read the order again and retry. Idempotency keys work as for `place_order`; there is no 30 s window without a key, since a repeated cancel or amend cannot add risk.

```bash
# on a demo key with trading allowed: list, change, cancel
uv run guarded-exchange serve   # then in the client: get_open_orders -> amend_order -> cancel_all_orders
```

### Trading: `set_trading_stop`, `close_position`, `set_leverage` (perpetuals, test environment only)

- `set_trading_stop {symbol, category, take_profit?, stop_loss?, idempotency_key?}` sets or moves the stop-loss / take-profit of the open position (`POST /v5/position/trading-stop`, whole position); prices must be on the tick. Removing a level is not offered.
- `close_position {symbol, category, qty?, idempotency_key?}` closes the position fully or partly with a **reduce-only market order** (Limit IOC at the worst price within the policy's slippage, the same planning, client order id and reconnaissance as `place_order`; `qty` is cut to the position). Answers like `place_order`.
- `set_leverage {symbol, category, leverage, idempotency_key?}` sets the symbol's leverage; the exchange's "not modified" (`110043`) counts as success.

Spot is refused with `invalid_params` (`details.reason: linear_only`). **Risk (FR-43):** closing and setting SL/TP reduce risk, so they work under the kill switch and for positions larger than `max_order_notional_usdt`; raising the leverage (or setting it when the current one is unknown) increases risk: the kill switch blocks it and the allowlist and `max_leverage` apply; lowering or keeping it never faces a limit. **Confirmed by reading the position (D-21):** SL/TP and leverage have no client order id, so after an accepted request the position is read back (`applied` only when it shows the values, else `requested`), and after an unknown outcome it is read again, never re-POSTed; if it does not show the change the answer is `outcome_unknown` (repeating is safe: absolute values).

### Kill switch

While a user's kill switch is on, every **risk-increasing** action is refused with `kill_switch_active` and nothing is sent; reading and **risk-reducing** actions keep working with the usual policy (SPEC FR-16, FR-43). For `place_order` that means: any order that is not reduce-only is refused, including every spot order, buy or sell (a spot sell is not modelled as closing anything, SPEC GP-5); a reduce-only order still goes out. The actions of the next tickets follow the same rule by their risk direction: raising leverage and amending an order to a larger size or a worse price are refused; cancelling, closing a position, SL/TP and lowering leverage are allowed. Turning the switch on **cancels nothing** on the exchange: open orders stay open until cancelled.

- The agent can only turn it **on**: `activate_kill_switch` (no arguments, available to every user, also read-only ones, and repeatable). There is no MCP tool or argument that turns it off (FR-44). `get_my_policy` shows `kill_switch: none | user | admin`.
- The user turns it on and off in the CLI; the admin can do it for any user. A kill switch the admin turned on (also over the user's) only the admin can turn off:

```bash
uv run guarded-exchange kill-switch on                         # the local user (or --user NAME)
uv run guarded-exchange kill-switch off                        # asks first; --yes to skip
uv run guarded-exchange admin kill-switch on --user alice
uv run guarded-exchange admin kill-switch off --user alice --yes
uv run guarded-exchange policy show                            # second line: kill switch: off | ON, ...
```

Every change is journaled (`kill_switch_on` / `kill_switch_off` with `before`, `after` and the actor `mcp:agent`, `cli:user` or `cli:admin`); a command that changes nothing writes nothing. The check runs twice on the order path: before the key check and any exchange read (journaled as `order_refused`, `stage: pre_check`), and again in the decision transaction under the user's lock, which every kill switch change takes too: an order whose decision comes after the switch is refused even if its first checks passed. A repeat of an order placed before the switch (same idempotency key) still gets the first answer. A confirmed action that is executed after the switch went on is checked again: an increase is refused (`kill_switch_active`), a reduction goes through (see Human confirmation); waiting confirmations are not cancelled eagerly.

### Human confirmation

Any trading tool (`place_order`, `cancel_order`, `amend_order`, `cancel_all_orders`, `set_trading_stop`, `close_position`, `set_leverage`) whose action the policy requires confirming (`confirm_actions`, or an order at least `confirm_above_notional_usdt`) is **parked**, not sent: the call returns `status: "pending"` with a `confirmation_id`, `expires_at` (`confirmation_ttl_s`, 120 s by default) and `shown_to_user`, the server's own plan (sizes, the worst price of a market order, the orders a cancel touches with their origin). The agent can read it with `get_confirmation_status` or withdraw it with `cancel_confirmation`; **no MCP tool approves or rejects** (AC-4). Repeating the same call (or the same `idempotency_key`) returns the same pending confirmation; more than `max_pending` waiting ones are refused with `pending_limit_reached`.

**In the client's own UI (MCP 2026-07-28, MRTR).** If the policy trusts the client's UI (`trust_client_ui`: on for testnet, never for mainnet; `prefer_external_channel` turns it off) and the client declared form elicitation, the same call answers with a confirmation form built from the server's own record (what is requested and what will be executed); the client asks the human and repeats the call with the answer, and the agent gets the action's result. Press Space to tick the box, then Enter: submitting without the tick, declining or closing the form is not a confirmation, and it also ends the request (an unclear or missing answer counts as declined and rejects it, so it can no longer be approved in the CLI; the agent has to ask again). The answer is bound to the waiting record by a sealed `requestState` (AES-GCM, key derived from the master key, so a server restart between the rounds is harmless), is judged against the policy at that moment, and goes through the same single decision and the same executor as the CLI: if the form and the CLI race, one wins and the journal names its channel (`client_ui` or `cli`). The model never sees the form or its answer. Tested with Claude Code >= 2.1.312 (spike 003); Claude Desktop is untested here, so set `prefer_external_channel` for it. A custom client that answers forms automatically is outside the guarantee (Claude Code hooks can too): that is why mainnet does not trust the UI.

The human decides in the CLI, and only if the admin enabled that channel (`GX_CONFIRM_CLI_ENABLED=true` for the server **and** the CLI; off by default because an agent with shell access on the same machine could run the CLI itself, FR-42):

```
guarded-exchange confirm list                 # what waits, exactly what will be executed, external orders marked
guarded-exchange confirm approve <id> [--yes] # prints the plan, asks, approves
guarded-exchange confirm reject <id>
```

Approval only marks the confirmation `approved`; the server process executes it itself (the `ConfirmedActionExecutor` scans every second), so **the server must be running**. Execution starts at most 30 s after the approval, then it is `expired_after_approval`. Before sending, everything is checked again as of that moment: user status, the same key and environment, the kill switch (an increase is refused, a reduction goes through), the current policy and limits, the allowlist and the action's own guards. The action is held to what was shown: a market order keeps the shown worst price (what cannot fill within it is cancelled, and the real fill is reported); a reduce-only order or a close is cut to the position that is left; a risk-reducing action that became risk-increasing is refused (`risk_direction_changed`); `cancel_all_orders` cancels exactly the orders shown. A refusal at that point is `failed` with its code in `get_confirmation_status` and the journal. The order is sent with a client order id derived from the confirmation id, so a restart never sends it twice: an `executing` confirmation found after a crash is looked up on the exchange (`executed`, or `unknown`, never resent). Replacing or deleting the exchange key cancels waiting and approved confirmations. Every step is journaled with its channel (`confirmation_requested`, `confirmation_decided`, then the usual `order_*` events under the actor `system:executor`). MVP limits: one server process; `origin: system` of an order is judged by its client id prefix, not verified against the journal.

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
| `GX_CONFIRM_CLI_ENABLED` | `false` | trust the CLI (`confirm approve` / `confirm reject`) as a confirmation channel; set it for the server and the CLI alike (FR-42) |
| `GX_LOCAL_USER` | `local` | name of the local-mode user (the only principal in local mode) |
| `GX_SQLITE_BUSY_TIMEOUT_MS` | `5000` | how long a writer waits for another process's write lock |
| `GX_HTTP_HOST` / `GX_HTTP_PORT` | `127.0.0.1` / `8000` | where `serve --transport http` listens |
| `GX_HTTP_ALLOWED_HOSTS` | `["127.0.0.1:*", "localhost:*", "[::1]:*"]` | JSON list of accepted `Host` headers (`host:*` = any port) |
| `GX_HTTP_ALLOWED_ORIGINS` | `["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]` | JSON list of accepted `Origin` headers (browsers) |

### Agent client and tool-selection evals

`guarded-agent` is a small console agent (package `agent/guarded_agent`, talks to the server only over MCP). It works with Anthropic (`claude-haiku-5-5`, default) or OpenAI (`gpt-6-luna`, Responses API); a confirmation form the server sends is answered by the human in the console, never by the model. Evals (`evals/`) run 43 cases (ru/en, 19 dangerous) against the server in-process on fakes and score the first changing tool call; the run stops at a hard budget (default $0.50).

```bash
ANTHROPIC_API_KEY=... uv run guarded-agent                     # spawns the stdio server (`init` it first)
OPENAI_API_KEY=... uv run guarded-agent --provider openai --url http://127.0.0.1:8000/mcp   # GX_TOKEN for bearer
uv run python -m evals --provider fake --runs 1                # offline smoke run, $0
ANTHROPIC_API_KEY=... uv run python -m evals --provider anthropic --runs 3   # real run -> evals/report.md|json
```

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
