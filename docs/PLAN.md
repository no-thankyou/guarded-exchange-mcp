# PLAN: guarded-exchange-mcp

Статус: **утверждён пользователем (2026-10-27), прошёл grilling PLAN (2026-10-27)**: решения — раздел 12 (D-1…D-27), уточнения внесены в ADR с пометкой «D-N», тикеты — `docs/tickets/`. Документ отвечает на вопрос «как». Требования — в `SPEC.md` (ссылки FR/NFR/AC), факты и источники — в `docs/research/*.md` (проверены 2026-10-27). Пункты, требующие изменения SPEC, собраны в разделе 10 — без согласия пользователя они не применяются.

## 1. Обзор архитектуры

Один Python-процесс сервера на режим. Внутри — слои по `ENGINEERING.md`: тонкие entrypoints (MCP, CLI, Telegram-бот) вызывают use cases application-слоя; use cases оркестрируют доменные правила (политика, лимиты, классификация риска, конечный автомат подтверждения) и порты (биржа, хранилище, канал подтверждения, часы). Всё, что касается безопасности, решается в domain/application и одинаково для всех entrypoints; MCP-слой не знает ни про лимиты, ни про SQL.

```
 AI-клиент (Claude Code / Desktop / наш агент)        человек
        │ MCP (stdio | Streamable HTTP + bearer)        │ Telegram / CLI
        ▼                                               ▼
 interface/mcp  ──►  application (use cases)  ◄──  interface/telegram, interface/cli
                        │        │       │
                 domain rules   ports (Protocol)
                                  │
     infrastructure: bybit (httpx2) · db (SQLAlchemy Core: SQLite|Postgres) · crypto · telegram · ratelimit
                                  │
                  ConfirmedActionExecutor (фоновая asyncio-задача, outbox по таблице pending)
```

Ключевой инвариант AC-4 — **структурный**: путь «подтвердить действие» (`ResolveConfirmation`) не вызывается ни из одного MCP-инструмента; его вызывают только Telegram-адаптер, CLI и обработчик ответа elicitation (который принимает поля протокола `inputResponses`, а не `arguments` модели — см. ADR-005). Это проверяется тестом на реестре инструментов и e2e-тестом «агент пытается подтвердить сам».

## 2. Стек (версии на 2026-10-27, фиксируются `uv.lock`)

| Компонент | Выбор | Версия | Источник |
|---|---|---|---|
| Язык | Python | 3.12 | ENGINEERING.md |
| Пакеты, lock | uv (`uv.lock`) | 0.12.23 | тикет 001 |
| MCP | `mcp` (Python SDK v2, `from mcp.server import MCPServer`), протокол 2026-07-28 | 2.3.0 (закреплена `==`) | research/mcp-protocol.md |
| HTTP-клиент биржи | `httpx2` (`AsyncClient`, `MockTransport` в тестах) — продолжение `httpx` под опекой Pydantic, тот же API, импорт `httpx2`. Выбран в тикете 001 вместо `httpx`: `mcp` 2.3.0 сам зависит от `httpx2`, а `httpx` застыл на 0.28.1 — один HTTP-стек на процесс | 2.16.0 | research/exchange.md, PyPI 2026-10-27 |
| Биржа | Bybit V5 REST, свой клиент (pybit 5.16.0 — только эталон подписи при отладке) | — | research/exchange.md |
| БД | SQLAlchemy 2.1 Core async (без ORM) + Alembic | 2.1.6 / 1.20.1 | research/storage-audit.md |
| Драйверы | `aiosqlite` (локально), `psycopg` 3 (удалённо) | 0.22.1 / 3.3.6 | research/storage-audit.md |
| Шифрование | `cryptography` (AES-256-GCM, HKDF) | 50.x | research/storage-audit.md |
| Telegram | `aiogram` (Bot API 10.3, long polling) | 3.31.0 | research/confirmation-channel.md |
| LLM (агент-клиент, evals) | `openai` (Responses API), `anthropic` (Messages) | 3.39.0 / 2.3.0 | research/clients-and-agent.md |
| Конфиг, валидация | `pydantic` v2, `pydantic-settings` | 2.14.1 / 2.16.1 | ENGINEERING.md, PyPI 2026-10-27 |
| CLI | `typer` | 0.27.3 | PyPI 2026-10-27 |
| Логи | `structlog` (JSON в stderr; на stdio stdout занят протоколом) | 26.1.0 | NFR-3, research/mcp-protocol.md, PyPI 2026-10-27 |
| Тесты | `pytest`, `pytest-asyncio`, `pytest-cov`, `httpx2.MockTransport` (`respx` не нужен: он рассчитан на `httpx`), `jsonschema` (проверка `outputSchema` в e2e), `testcontainers[postgres]` | 9.1.1 / 1.4.1 / 7.1.0 / — / 4.26.0 / 4.15.0 | research/storage-audit.md, PyPI 2026-10-27 |
| Качество | `ruff`, `mypy --strict`, `import-linter`, `pre-commit`, GitHub Actions (`actions/checkout@v7`, `astral-sh/setup-uv@v10`) | 0.17.3 / 2.4.0 / 2.15 / 4.7.0 | ENGINEERING.md, PyPI и GitHub releases 2026-10-27 |

Версии проверены на PyPI 2026-10-27 и зафиксированы в тикете 001 (`uv.lock`); в `pyproject.toml` — нижние границы, `mcp` закреплён точно (SDK 2.x быстро меняется, раздел 9).

## 3. Структура и слои

```
src/guarded_exchange/
  domain/
    money.py            # Decimal-типы: Price, Qty, Notional, Percent; округление к шагу
    instruments.py      # Instrument, InstrumentRules (tick, qty step, min qty/notional, max leverage), validate (FR-10)
    market.py           # рыночные value objects: Ticker (тикет 001), позже Orderbook, Candle
    actions.py          # TradingAction (PlaceOrder, AmendOrder, CancelOrder, CancelAll, SetTradingStop, ClosePosition, SetLeverage)
    risk.py             # classify(action, position) -> RiskDirection; reduce-only clamp (FR-43)
    policy.py           # AdminCeiling, UserOverrides, EffectivePolicy = strictest(...) (FR-12, FR-18, FR-42, FR-45)
    limits.py           # check_limits(action, policy, market) -> Decision | LimitViolation (FR-13–17)
    confirmation.py     # PendingConfirmation + конечный автомат, action_digest (FR-22, FR-23, FR-47)
    audit.py            # AuditEvent, canonical JSON (FR-36)
    errors.py           # доменные исключения ↔ коды FR-28
  application/
    ports.py            # ExchangeClient, *Repository, UnitOfWork, ConfirmationChannel, CredentialCipher, Clock, IdGenerator, RateLimiter
    read_market.py, read_account.py, read_policy.py, read_audit.py
    submit_action.py    # единый путь любого торгового действия: auth → [второй раунд MRTR → resolve, D-1] → повтор/идемпотентность (D-20) → rate limit → rules → policy/limits → confirm? → execute
    resolve_confirmation.py   # человек одобрил/отклонил (только не-MCP каналы)
    execute_confirmed.py      # перепроверка FR-48 + исполнение + разведка исхода FR-27
    admin.py            # users, tokens, ceilings, mainnet, kill switch, revoke (FR-29, FR-32, FR-44)
    credentials.py      # add/replace/delete ключа + проверка прав (FR-30, FR-49)
    audit_verify.py     # проверка цепочки (FR-50)
  infrastructure/
    bybit/              # client.py (подпись, таймауты, X-Bapi-Limit-*), mapper.py (DTO → domain), errors.py (retCode → domain)
    db/                 # schema.py (Table), repositories.py, uow.py, dialect.py (lock_user, upsert, Decimal/UTC TypeDecorator), migrations/
    crypto.py           # AES-GCM + HKDF keyring
    audit_chain.py      # HMAC-цепочка, checkpoints
    telegram/           # бот: уведомления, callback Approve/Reject, pairing
    ratelimit.py        # token buckets in-memory
    clock.py
  interface/
    mcp/                # server.py (MCPServer, транспорт), tools/*.py (тонкие), schemas.py (pydantic I/O), errors.py (единый маппинг)
    cli/                # admin и user команды (typer)
  executor.py           # ConfirmedActionExecutor (фоновая задача)
  config.py             # pydantic-settings
  bootstrap.py          # composition root
agent/                  # агент-клиент (FR-39): отдельный пакет guarded_agent в том же репо, зависит только от MCP, не от сервера
evals/                  # кейсы (jsonl), runner, отчёт (FR-40)
tests/ unit/ integration/ e2e/ testnet/ (маркер, вне CI)
```

Правила слоёв: domain не импортирует ничего из проекта, кроме себя; application — domain и `ports.py`; infrastructure и interface — application и domain; `bootstrap.py` — всё. Проверка — `import-linter` контракт в CI (дёшево и наглядно на ревью).

## 4. Ключевые решения (ADR)

### ADR-001. Биржа — Bybit V5; тестовая среда — Demo Trading
- **Контекст:** SPEC 9.1. Нужны спот и линейные перпетуалы USDT, SL/TP на позицию, amend, cancel-all, плечо, правила инструментов, client order id, понятные rate limits, проверка прав ключа (FR-49).
- **Решение:** Bybit V5. Один API для spot/linear (`category`), `POST /v5/position/trading-stop` для SL/TP (FR-6, AC-1), `POST /v5/order/amend`, `GET /v5/user/query-api` (Withdraw/Transfer видны отдельно), per-UID лимиты с `X-Bapi-Limit-Status`, отклонение дубликата `orderLinkId` (110072/170141). Тестовая среда — **Demo Trading** (`api-demo.bybit.com`): правила как на live, публичные данные с mainnet, пополнение `POST /v5/account/demo-apply-money`; классический testnet — запасная среда в конфиге. Позиционный режим v1 — one-way (`positionIdx=0`), hedge mode — понятный отказ.
- **Альтернативы:** Binance — spot и futures разные API, SL/TP вынесены в algo-ордера без amend, лимиты по весу на IP (в multi-tenant общий бюджет, бан 418 до 3 дней). OKX — объём перпетуалов в контрактах (`ctVal`, риск ошибки notional), защита slippage только на споте.
- **Последствия:** спайк 004 (👤) против demo: доступны ли `query-api` и `instruments-info` на demo; область уникальности `orderLinkId`; семантика 10000/10016/170146 (считаем «неизвестно»). `cancel-all` на linear всегда с явным `orderFilter` (иначе снесёт TP/SL позиции). История закрытых ордеров ограничена (500 / 24 ч для отменённых) — происхождение «через систему» берём из своего журнала, а префикс `orderLinkId` — быстрый фильтр. Слово «testnet» в SPEC = тестовая среда биржи → см. раздел 10, П-1.

### ADR-002. Свой async-клиент Bybit на httpx2 за портом `ExchangeClient`
- **Контекст:** FR-27 (точные таймауты, неизвестный исход), FR-35 (заголовки лимитов), NFR-2 (без сети), NFR-5 (вторая биржа).
- **Решение:** `infrastructure/bybit/client.py` — подпись HMAC-SHA256, раздельные таймауты connect/read, без автоматических retry для изменяющих запросов (retry с backoff — только GET). Ответы маппятся в доменные объекты в `mapper.py`, коды ошибок — в доменные исключения в `errors.py`. Порт `ExchangeClient` описан в доменных терминах (instrument, side, qty, price, client_order_id), без слов Bybit.
- **Альтернативы:** pybit (синхронный, requests), ccxt (большая зависимость, скрывает именно то, что надо показать, своя нормализация ошибок).
- **Последствия:** ~300–500 строк своего кода и записанные фикстуры ответов; полный контроль; NFR-5 проверяем тем, что domain/application не импортируют `bybit`.
- **D-26.** Порт выражает доменные понятия (ордер с ограничением цены и немедленным исполнением, reduce-only, client_order_id); `positionIdx`, `orderFilter`, 110043, Set Limit Price Behaviour, формат `gx-`/36 символов — только в адаптере; hedge mode → доменная `UnsupportedPositionMode`. NFR-5 подтверждается тем, что все use cases работают на `FakeExchange`.

### ADR-003. Транспорт: stdio локально, Streamable HTTP удалённо; протокол 2026-07-28
- **Контекст:** SPEC раздел 3, 9.2. Спека 2026-07-28 stateless: нет сессий и `initialize`, capabilities клиента приходят в `_meta` каждого запроса, server→client запросов посреди вызова нет.
- **Решение:** один `MCPServer`, два способа запуска: `run("stdio")` (локальный режим, логи только в stderr) и `streamable_http_app()` за ASGI с `TransportSecuritySettings(allowed_hosts=...)`. Целевая ревизия — 2026-07-28; legacy-клиенты (2025-11-25) поддерживаем в той мере, в какой это делает SDK, а подтверждение через UI на legacy — только если спайк покажет, что это дёшево. `RequestStateSecurity(keys=[из конфига], audience=...)` — иначе рестарт ломает подтверждения в полёте. **Спайк 002:** `keys=[HKDF-подключ request_state]` (≥ 32 байт), `audience="guarded-exchange"` явно (по умолчанию — имя сервера), `ttl` = максимальный TTL подтверждения (по умолчанию 600 с = потолок админа FR-23), `bind_principal` по умолчанию; подробности — «Спайк 002» ниже. При смене master key (`rotate-keys`) подтверждения в полёте отклоняются (`-32602`, fail closed: человек повторяет действие); бесшовная ротация — кольцо `keys=[new, old]` на время TTL.
- **Альтернативы:** HTTP и локально (одна дорожка, но хуже UX подключения и нужен токен даже локально).
- **Последствия:** в локальном режиме Telegram-бот и executor живут в том же stdio-процессе. Claude Code и Desktop запускают каждый свой процесс → несколько процессов на одной SQLite (ADR-007) и **несколько потребителей Telegram long polling** (409 Conflict). Решение: Telegram-поллер и executor работают только у лидера (механизм и fencing — ADR-006, D-3); процесс без лидерства только пишет pending и шлёт уведомления через Bot API (send не конфликтует).

### ADR-004. Аутентификация удалённого режима — opaque bearer-токены за портом; OAuth 2.1 позже
- **Контекст:** SPEC 9.2, FR-29 (мгновенный отзыв), FR-33, NFR-2.
- **Решение:** админ CLI выпускает токен пользователю (в БД — только SHA-256 хэш), `TokenVerifier` ищет хэш и проверяет `users.status=active` **на каждом запросе**; principal — только из `get_access_token()`, никогда из `clientInfo`, заголовков или аргументов. Локальный режим — без токена, principal = локальный пользователь из конфига. Порт `Authenticator` в application позволяет добавить адаптер OAuth 2.1 (JWT + Protected Resource Metadata от реального AS) без изменения use cases.
- **Альтернативы:** полный OAuth 2.1 с Keycloak в docker-compose — spec-conformant, но инфраструктура, CIMD/redirects, сложные e2e; отзыв всё равно требует проверки в БД.
- **Последствия:** отступление от SHOULD спеки (не MUST) — честно в README («static bearer tokens; OAuth-ready via adapter»). Клиенты только с OAuth не подключатся (Claude Code и наш агент — подключатся через `--header`). Auth на stdio и in-memory `Client` SDK не применяется → тесты auth только через реальный HTTP (integration).

### ADR-005. Подтверждение: pending-запись — единый источник истины; каналы — UI клиента (MRTR), Telegram, CLI
- **Контекст:** FR-20–25, FR-42, FR-47, FR-48, AC-1, AC-4; stateless MCP (research/mcp-protocol.md 3.3).
- **Решение:**
  1. Любое действие, требующее подтверждения, сначала создаёт `PendingConfirmation` в БД: случайный 128-битный id, user, credential_id, canonical JSON действия + `action_digest`, **зафиксированная** оценка цены и худшая цена (FR-21/48; текст elicitation обязан быть стабилен между раундами), TTL по политике, канал. Запись журнала — в той же транзакции.
  2. Выбор канала на вызове: если политика доверяет UI клиента (FR-42: testnet — да, mainnet — нет) **и** в этом запросе клиент объявил `elicitation.form` **и** протокол 2026-07-28 → `InputRequiredResult` с формой (FR-47а): повторный `tools/call` несёт `inputResponses` и `requestState`; сервер сверяет digest аргументов с pending (FR-22, защита от подмены), атомарно переводит `PENDING→APPROVED` и исполняет в рамках вызова. Иначе, если у пользователя привязан доверенный внешний канал → сразу structured-результат `{status: pending_confirmation, confirmation_id, expires_at}` и уведомление в Telegram (FR-47б); статус — read-инструмент `get_confirmation_status`. Иначе → отказ без создания записи (FR-25, fail closed), ошибка `confirmation_channel_unavailable` с подсказкой.
  3. Одноразовость — только атомарным условным UPDATE (`WHERE status='pending' AND expires_at > now`): `requestState` single-use не гарантирует (WARNING в спеке).
  4. Внешний канал v1 — **Telegram-бот** (aiogram, long polling, публичный URL не нужен): бот рисует параметры из серверной записи, в `callback_data` только id; личность человека — `from.id`, проставленный Telegram, сверяется с привязкой пользователя. Привязка (pairing): бот выдаёт код → пользователь вводит его в CLI; отвязка — только CLI; всё в журнал.
  5. **CLI `approve/reject <id>`** — второй канал, доверенный только если админ явно включил (по умолчанию выключен для mainnet и удалённого режима), т.к. агент с shell на той же машине может его вызвать.
  6. Исполнение после подтверждения — `ConfirmedActionExecutor` (ADR-006).
  7. **D-1. Второй раунд MRTR** — отдельная ветка до слоя идемпотентности. **Признак второго раунда — только `ctx.request_state is not None`** (наш запечатанный pending id, проверенный SDK), а не наличие `inputResponses`: SDK никак не связывает `inputResponses` с `requestState`, и первый `tools/call` с готовыми `inputResponses` без `requestState` доходит до тела инструмента (спайк 002). Такой запрос → `invalid_params` без создания pending (fail closed). Подтверждением считается только ответ по ключу, который мы задали, типа `ElicitResult` с `action == "accept"` **и** `content.confirm is True`; отсутствие ключа, другой тип, `confirm` ≠ `true`, `decline`/`cancel` — отказ (не исключение). Ветка второго раунда: fingerprint не создаёт и не трогает, write-бакет не тратит; сверяет `pending.user_id` с principal, digest и статус. Повтор с `requestState`, но без `inputResponses` (ретрай «только со state», оборванный поток), пока pending в `PENDING`, возвращает `InputRequiredResult` для **того же** pending, новый не создаётся; повтор первого раунда вообще без `requestState` (второй клиент) — дедупликация по digest открытого pending (D-20 п.2).
  8. **D-2. Один путь исполнения.** Синхронный путь делает один CAS `PENDING→EXECUTING` (канал и решение — в той же транзакции) и вызывает тот же `ExecuteConfirmed`, что и executor; executor берёт только `APPROVED`, выставленные Telegram/CLI. Исполняет только победитель CAS. Второй раунд, заставший pending не в `PENDING` (одобрен другим каналом, исполняется, исполнен), ничего не исполняет и возвращает текущий статус/результат; так же — повтор второго раунда после обрыва потока.
  9. **D-4. Digest и каналы.** `action_digest` = SHA-256 канонического JSON `{tool, args}` (Decimal нормализованы: `"0.10"` = `"0.1"`; float запрещён; дефолтные поля заполнены явно) + environment + credential_id. Pending разрешается только каналом, доверенным политикой **на момент решения**; первый CAS побеждает; канал — в журнал. `cancel` в ответе elicitation → `CANCELLED` (журнал). Клиент, не вернувшийся вторым раундом, — pending живёт до TTL и занимает слот лимита одновременных pending (документировано).
  10. **D-5.** Ключ `RequestStateSecurity` — HKDF-подключ `request_state` от master key (ADR-009, D-14): рестарт процесса между раундами не ломает подтверждение.
  11. **D-6.** Политика `prefer_external_channel` (по умолчанию false) — не использовать UI клиента даже при `trust_client_ui` (против фиктивного `decline`). Агенту доступен `cancel_confirmation(id)`: только `PENDING|APPROVED → CANCELLED` своего действия; это не подтверждение, AC-4-тест проверяет, что он не ведёт к исполнению (решение пользователя, правка SPEC FR-47).
  12. **D-7. Telegram.** `telegram_message_id` хранится в pending (лидер редактирует сообщения, созданные не-лидерами). Уведомление отправляется после коммита pending; ошибка отправки → pending `CANCELLED` и `confirmation_channel_unavailable` (FR-25). Если живого поллера нет (лидерство не удерживается), внешний канал считается недоступным — агент не получает `pending_confirmation`, которое некому одобрить. Включение kill switch сразу отменяет `PENDING/APPROVED` действия, увеличивающие риск, и редактирует сообщения. Approve, нажатый при отсутствии процессов, обработается после старта, когда TTL уже истёк — безопасно, документируется в README.
- **Альтернативы:** URL-mode elicitation на свою веб-страницу (нужна веб-аутентификация человека — по сути web-login, который SPEC выносит в non-goals; v2 с WebAuthn); ntfy (публичные топики, нет идентичности нажавшего); email; TOTP в чат (человек видит параметры в пересказе модели — нарушает FR-21); Tasks extension MCP (не реализован в Python SDK).
- **Последствия:** Claude Code поддерживает form-elicitation официально, но поддержку MRTR-формы на 2026-07-28 надо проверить руками (спайк 003); если нет — режим legacy для stdio или только внешний канал. Claude Desktop по открытому багу не объявляет elicitation (или присылает фиктивный `decline`) → для него внешний канал; фиктивный `decline` журналируется как «declined by client UI», а не «отклонено человеком» (канал записывается всегда). README: Claude Code `Elicitation` hook может автоответить — поэтому FR-42 не доверяет UI на mainnet. **Спайк 002 (подтверждено):** `InputRequiredResult` возвращается из тела high-level `@mcp.tool` с аннотацией `-> Annotated[CallToolResult, Out] | InputRequiredResult` (`outputSchema` сохраняется); второй раунд читает `ctx.input_responses` и `ctx.request_state` (plaintext, SDK распечатал и проверил); low-level `Server` не нужен. Выбор канала: `ctx.protocol_version == "2026-07-28"` и `ctx.client_capabilities.elicitation.form is not None`. На legacy-соединении (2025-11-25) `InputRequiredResult` даёт `-32603`, зато работает push `ctx.elicit()` внутри того же вызова — один инструмент может обслуживать обе эры (ветка по `protocol_version`), если спайк 003 покажет, что клиент ходит по legacy. Python `Client` без `elicitation_callback` не объявляет `elicitation`, а на `InputRequiredResult` с формой падает `-32600 Elicitation not supported` — поэтому проверка capability до создания pending обязательна.

### ADR-006. Исполнитель подтверждённых действий — фоновая задача + outbox по таблице pending
- **Контекст:** FR-47б (сервер исполняет сам), FR-48, FR-27, FR-29, рестарты.
- **Решение:** `ConfirmedActionExecutor` — asyncio-задача в процессе-лидере. Будится in-process сигналом после `APPROVED` и раз в N секунд сканирует `APPROVED` (сигнал не теряется). Шаги: CAS `APPROVED→EXECUTING` (один исполнитель) → полная перепроверка на момент исполнения (доступ, ключи = те же `credential_id`, kill switch, политика, лимиты, allowlist, граница цены) → отправка с детерминированным `client_order_id` из pending id → `EXECUTED | FAILED | UNKNOWN`. Вызов биржи и запись журнала экранированы от cancellation (`asyncio.shield`). Рестарт: `PENDING` живут до TTL; `APPROVED` исполняются только если не истёк `expires_at` исходного запроса (иначе `EXPIRED_AFTER_APPROVAL`); `EXECUTING` → разведка по `client_order_id` (FR-27).
- **Альтернативы:** отдельный воркер-процесс с очередью (RabbitMQ/Redis) — лишняя инфраструктура для v1; исполнение только при повторном вызове агента — нарушает FR-47.
- **Последствия:** v1 — один экземпляр сервера в удалённом режиме (ограничение в README); multi-instance — через leader election в БД (D-3).
- **D-3. Лидерство и fencing.** Локально — `fcntl.flock` на файле `leader.lock` рядом с БД (ОС снимает блокировку при смерти процесса); удалённо — строка `leases` с монотонным `epoch`, продление раз в 5 с, TTL 15 с. Не-лидеры пытаются стать лидером раз в 2 с. Каждый захват увеличивает `executor_epoch`; CAS `→EXECUTING` записывает epoch и `exec_started_at`, финальный UPDATE проверяет epoch; восстановление (разведка FR-27) трогает только `EXECUTING` с epoch меньше текущего и не раньше `exec_started_at + 30 с` — иначе новый лидер пометит FAILED ордер, который старый ещё отправляет. Пробуждение: in-process сигнал (Telegram-поллер у лидера) + scan `APPROVED` раз в 1 с (локально; Postgres — `LISTEN/NOTIFY` + scan 5 с). **Дедлайн исполнения `APPROVED` — `approved_at + 30 с`**, а не `expires_at`: подтверждение за секунду до TTL не должно превращаться в отказ, ценовой риск закрывает FR-48. Синхронный путь MRTR исполняется в процессе, получившем второй раунд, независимо от лидерства. Тесты: «лидер убит → другой процесс исполняет APPROVED», «lease истёк при EXECUTING → нет двойного исполнения и ложного FAILED», «CLI approve за 1 с до TTL → исполнено».
- **D-8. Перепроверка FR-48 — что зафиксировано, что пересчитывается.** Зафиксировано в pending: действие (digest), environment, credential_id, оценка и худшая цена, показанный объём, направление риска, для cancel-all — список id ордеров. На момент исполнения пересчитывается: статус пользователя (FR-29); credential тот же и active; environment пользователя = pending; kill switch; эффективная политика (профиль, allowlist, лимит notional, плечо); правила инструмента (FR-10, статус торгов); направление риска (было REDUCES, стало INCREASES — отказ `risk_direction_changed`: человек одобрял снижение риска); для reduce-only/закрытия — объём min(показанный, текущая позиция), что человеку показано как «до N» (решение пользователя, правка SPEC FR-43); для market — граница цены через Limit IOC (ADR-010). FR-46 к подтверждённым не применяется. Cancel-all исполняется как отмена конкретных id из pending (новые внешние ордера человек не видел). Флаг reduce-only всегда уходит на биржу как вторая линия защиты. **TOCTOU:** чтения биржи — до транзакции (D-10); проверки по БД и CAS `→EXECUTING` — одна write-транзакция под `lock_user`; включение kill switch и отзыв доступа тоже берут `lock_user`, поэтому всё, что прошло CAS, считается «уже отправленным» в смысле FR-29. Окно между CAS и HTTP-запросом документируется.
- **D-9. FR-29.** При отзыве доступа и замене/удалении ключа аннулируются `PENDING` и `APPROVED`; `EXECUTING` и `UNKNOWN` доводятся до исхода. Удаление ключа — soft-delete: шифротекст хранится, пока на него ссылаются `EXECUTING/UNKNOWN`, используется только для разведки, затем стирается. Аннулирование pending при замене ключа — в M3 вместе с pending.

### ADR-007. Хранилище — SQLAlchemy 2.1 Core, SQLite → Postgres, без Redis
- **Контекст:** SPEC 9.5; оба режима; конкурентные клиенты одного пользователя; «одна команда».
- **Решение:** Core (Table + выражения, без ORM), Alembic. Локально `sqlite+aiosqlite`: WAL, `busy_timeout`, `foreign_keys=ON`, write-транзакции `BEGIN IMMEDIATE`, проверка версии SQLite ≥ 3.35 при старте (RETURNING; fail closed). Удалённо `postgresql+psycopg`. Различия диалектов — в `db/dialect.py`: upsert, `lock_user()` (FOR UPDATE / IMMEDIATE), `TypeDecorator` Decimal-как-TEXT (на SQLite NUMERIC теряет точность) и UTC-время. Идемпотентность, окно 30 с, FR-46, pending и однократное исполнение — unique constraints, upsert и условные UPDATE. Rate limits — in-memory (ADR-011). Изоляция FR-33: каждый метод репозитория принимает `user_id` и включает его в WHERE; «не найдено» и «чужое» — одна ошибка.
- **Альтернативы:** raw SQL (две версии запросов), ORM (лишняя магия для простой модели), Postgres везде (локальному режиму нужен Docker), Redis (ещё один сервис ради того, что БД делает атомарно).
- **Последствия:** Postgres и матрица integration-тестов на двух БД — в milestone удалённого режима. Риск `database is locked` при нескольких stdio-процессах — тест с двумя процессами. Pull образа Postgres в CI — сетевая операция инфраструктуры (раздел 10, П-4).
- **D-10. Транзакции.** Никакого сетевого I/O внутри транзакции БД. Write-путь: чтения биржи → короткая write-транзакция (проверки, слоты, CAS, журнал attempt) → HTTP к бирже вне транзакции → короткая транзакция с исходом. `UnitOfWork(write=True)` → `BEGIN IMMEDIATE` (рецепт SQLAlchemy для pysqlite/aiosqlite: отключить автоматический BEGIN драйвера и выдать свой в событии `begin`), read — deferred. SQLite в WAL с `synchronous=NORMAL`. Время — INTEGER epoch ms (SQLite) / `timestamptz` (Postgres) через `TypeDecorator`; `now` всегда из порта `Clock`, не из БД (иначе не работает `FakeClock` и строковые сравнения ISO).
- **D-11. Миграции.** Только командой `init`/`migrate` под эксклюзивной блокировкой, не при старте сервера (Claude Desktop поднимает процессы параллельно). Сервер при старте проверяет: ревизия = head, триггеры append-only на месте, SQLite ≥ 3.35 — иначе fail closed с подсказкой. Batch-миграции SQLite, пересоздающие `audit_log`, обязаны вернуть триггеры (тест). Postgres — две роли (владелец-мигратор и приложение с `INSERT, SELECT` на журнал), M6. Docker Desktop — данные в named volume (WAL ненадёжен на bind mount с хоста macOS/Windows).
- **D-12. Среда в данных.** `exchange_credentials.environment`, один active на (user, exchange, environment); pending хранит environment; исполнение сверяет равенство (D-8).

### ADR-008. Журнал — per-user HMAC-цепочка + checkpoints вне БД + append-only триггеры
- **Контекст:** FR-36, FR-37, FR-38, FR-50.
- **Решение:** запись = `chain_id (user_id | "system"), seq, ts, event_type, actor, payload (canonical JSON, Decimal строками), prev_mac, key_version`, `mac = HMAC-SHA256(audit_key, canonical(record))`. Сериализация внутри цепочки — строка `audit_heads` под `lock_user` + `UNIQUE(chain_id, seq)`. Запись журнала — в той же транзакции, что изменение состояния; для биржи — `attempt` до отправки и `outcome` после. Триггеры `BEFORE UPDATE/DELETE → RAISE` (обе БД), в Postgres — роль приложения только `INSERT, SELECT` на `audit_log`. Периодический checkpoint `{chain_id: (seq, head)}` с HMAC — в файл вне каталога БД (локально) и в Telegram-чат админа (если привязан). CLI `audit verify` — полный проход, отчёт о первом расхождении.
- **Альтернативы:** SHA-256 цепочка без секрета (пересчитывается любым с доступом к БД); глобальная цепочка (горячая точка, пользователь видит чужие хэши); Merkle tree / Tessera (эффективные proofs для внешних аудиторов, которых нет; Python-реализации нет).
- **Последствия:** README — честная таблица: изменение/удаление/вставка — обнаруживаются; усечение хвоста после последнего checkpoint и атакующий с ключом и checkpoints — нет. Потеря master key = невозможность verify → backup в документации.
- **D-13.** Сериализация цепочки — блокировка строки `audit_heads` (у цепочки `system` нет строки пользователя для `lock_user`). В M1–M4 журнал — минимальная append-only таблица с триггерами без MAC (D-27), цепочка добавляется в M5 миграцией с пересчётом.

### ADR-009. Секреты — AES-256-GCM с AAD, один master key, HKDF-подключи
- **Контекст:** FR-31, FR-50; угроза — подмена шифротекста между пользователями (агент B торгует на аккаунте A).
- **Решение:** ключи биржи и токен Telegram-бота шифруются AES-256-GCM, случайный 96-битный nonce, AAD = `credential_id ‖ user_id ‖ exchange`. Master key — `SecretStr` из окружения; HKDF выводит подключи `credentials` и `audit`. Keyring `{version: key}`; CLI `rotate-keys` перешифровывает credentials, старые audit-ключи хранятся для verify. Локально `init` генерирует master key в файл 0600 вне каталога БД.
- **Альтернативы:** Fernet (нет AAD — копия строки в чужого пользователя расшифруется); envelope encryption (имеет смысл с KMS, в v1 — преждевременно).
- **Последствия:** секреты никогда не логируются (structlog-процессор маскирования + тест).
- **D-14.** HKDF-подключи: `credentials`, `audit`, `request_state` (D-5), `telegram`. AAD токена бота — `"telegram-bot" ‖ bot_id` (AAD биржевых ключей к нему неприменим).

### ADR-010. Market-ордер исполняется как Limit IOC по худшей показанной цене
- **Контекст:** FR-13 (notional по худшей ожидаемой цене), FR-21, FR-48.
- **Решение:** worst = best встречной стороны × (1 ± s), округление к tick в безопасную сторону (buy вниз, sell вверх); notional для FR-13 — по округлённой границе и объёму после округления к qty step. Отправка: `orderType=Limit, timeInForce=IOC, price=worst`. Неисполненный остаток отменяется биржей; агент получает фактически исполненный объём. Для подтверждения цена фиксируется в pending (ADR-005).
- **Альтернативы:** нативный `slippageTolerance` Bybit — считается от опорной цены биржи (для деривативов — mark price) в момент матчинга, а не от показанной человеку → нарушает FR-48; есть не на всех биржах (NFR-5).
- **Последствия:** при внешнем канале и TTL 2 мин узкий допуск mainnet часто даст «исполнено 0» — безопасно; сообщение агенту объясняет, что делать. Проверить на demo «Set Limit Price Behaviour» (биржа может молча сдвинуть limit-цену в коридор) — выставить reject, если поддерживается.
- **D-15.** Ответ `order/create` асинхронный: фактически исполненный объём читается из ордера/исполнений после отправки; если ещё не виден — агент получает `filled: unknown` с подсказкой, как проверить, а не 0.

### ADR-011. Rate limits — in-memory token buckets в три уровня
- **Контекст:** FR-34, FR-35, FR-46.
- **Решение:** (1) на пользователя — бакеты read и write (создание pending тратит write; отмены write-бакет не тратят — FIA 3.4); (2) на биржевой UID — 50% per-UID лимита Bybit по категории; (3) глобальный на egress IP — 50% IP-лимита (Bybit 600/5 с → 60/с). Остаток обновляется по `X-Bapi-Limit-Status`; на `10006`/HTTP 403 — circuit breaker до `X-Bapi-Limit-Reset-Timestamp` (по умолчанию 10 с; 403 — 10 мин). Ответ агенту — `rate_limited` с `retry_after_s` (секунды). FR-46 — не бакет, а sliding window log по таблице исполненных действий с резервированием слота под `lock_user` (иначе два клиента на границе пройдут оба).
- **Альтернативы:** Redis-лимитер (нужен только при нескольких экземплярах — v1 один).
- **Последствия (исправлено D-16):** все бакеты in-memory, т.е. per-process: при N stdio-процессах бюджет N× по всем уровням, включая UID/IP. Бана нет, потому что суммарный трафик ограничен пользовательскими бакетами каждого процесса (read 2/с); оговорка в README. Circuit breaker на `10006` — по (UID, группа эндпоинтов), на 403 — глобальный. `activate_kill_switch` и `cancel_confirmation` от rate limit освобождены. Executor при отказе лимитера ждёт в пределах дедлайна исполнения (D-3), затем `FAILED(rate_limited)`. Числа биржи — в адаптере, не в domain (NFR-5).
- **D-17. Слоты.** FR-46: строка-слот вставляется в той же write-транзакции, где решение политики (под `lock_user`), и не возвращается при отказе биржи. Лимит одновременных pending (10/5) — count+insert в одной write-транзакции.

### ADR-012. Видимость инструментов: граница — проверка в `tools/call`; фильтрованный `tools/list` через override `MCPServer.list_tools()` (спайк 002)
- **Контекст:** FR-11, FR-44; в `MCPServer` нет документированного хука per-user `tools/list`, middleware SDK помечен provisional.
- **Решение:** права проверяются в use case на каждом вызове — это и есть граница безопасности. Сверху — фильтрованный `tools/list` по профилю. **Спайк 002:** `interface/mcp/server.py` объявляет подкласс `GuardedMCPServer(MCPServer)` и переопределяет публичный `async list_tools()`: SDK вызывает его из `_handle_list_tools` без контекста, поэтому principal берётся из contextvar — `get_access_token()` на HTTP (работает внутри `list_tools`, проверено на Streamable HTTP + `TokenVerifier`), локальный пользователь из конфига на stdio. Профиль пользователя читает use case (application), interface только отфильтровывает имена. Результат по умолчанию уже `ttlMs=0, cacheScope="private"` (дефолт `CacheableResult` в `mcp_types`), явный `cache_hints` не нужен. Скрытый инструмент по-прежнему вызывается напрямую через `tools/call` — отказ `permission_denied` даёт use case (тест AC-2/FR-11 обязан это покрыть).
- **Альтернативы:** весь сервер на low-level `Server` (больше ручного кода схем); provisional middleware (работает — спайк фильтрует dict-результат `tools/list`, — но SDK прямо обещает менять сигнатуру в 2.x minor); замена обработчика через приватный `_lowlevel_server.add_request_handler`; `Extension` не может перехватить `tools/list` (`MethodBinding` запрещает spec-методы).
- **Последствия:** evals измеряют, мешают ли видимые, но запрещённые инструменты (кейсы read-only профиля). Override опирается на публичный метод, но на неявный контракт «`_handle_list_tools` вызывает `self.list_tools()`» — при обновлении `mcp` его ловит e2e-тест фильтрации (версия закреплена `==`). Смена профиля без переподключения клиента видна на следующем `tools/list` (`ttlMs=0`); `notifications/tools/list_changed` — только подписчикам `subscriptions/listen`.

### ADR-013. Единая модель ошибок для агента
- **Контекст:** FR-28, FR-41, FR-3.
- **Решение:** доменные исключения → в `interface/mcp/errors.py` в tool execution error (`isError: true`) со structured content `{code, message, next_step, retry_after_s?, details?, untrusted_text?}` (имя поля `retry_after_s` — секунды, float; зафиксировано в тикете 001); коды: `invalid_params`, `instrument_rule_violation`, `permission_denied`, `limit_exceeded`, `kill_switch_active`, `confirmation_required|pending|rejected|expired|channel_unavailable`, `idempotency_conflict`, `exchange_rejected`, `rate_limited`, `exchange_unavailable`, `exchange_credentials_invalid`, `outcome_unknown`, `not_found`, а также (D-18) `pending_limit_reached`, `position_not_found`, `risk_direction_changed`, `unsupported_position_mode`, `credentials_permissions_changed`. Protocol errors (`MCPError`) — только для ошибок протокола: их видит клиент, а не модель. Ответы — pydantic-модели (автоматический `outputSchema` + `structuredContent`) и краткий текст; свободный текст с биржи/из журнала — в поле `untrusted_text` с пометкой (FR-41).
- **Последствия:** маппинг покрыт таблично-параметризованным тестом; evals используют `code` для детерминированной оценки.
- **Спайк 002: ошибки валидации аргументов SDK → `invalid_params` в одном месте.** `GuardedMCPServer` переопределяет публичный `call_tool(name, arguments, context)`: `ToolError`, у которого `__cause__` — pydantic `ValidationError` и который не `UnexpectedToolError`, превращается в tool error `{code: invalid_params, message: "<поле>: <msg>; …", details: {field, reason}}` без эха значений (свои валидаторы аргументов не вставляют значение в текст ошибки; для `value_error` в ответ идут только `loc` и `type`, без `msg`); остальное (неизвестный инструмент, сбой в теле → `UnexpectedToolError`) остаётся SDK. Поэтому аргументы инструментов объявляются настоящими типами (`Literal[...]`, `Field(pattern=..., gt=0)`, `Decimal`) — `inputSchema` и проверка формы на границе MCP из одной аннотации, `WithJsonSchema` больше не нужен. Это **дополнительный** слой: нормализация и проверка `symbol`/`category` остаются в доменных value objects и use case, потому что у use case есть и другие входы (CLI, Telegram, executor), а `action_digest` (D-4) считается от нормализованных значений; доменные правила (шаг цены, min notional) — тоже в домене. `Extension.intercept_tool_call` не подходит: он видит уже готовый текстовый `CallToolResult` (исключение превращено в текст внутри `_handle_call_tool`), а ещё рекламирует себя клиентам как протокольное расширение. Нюансы pydantic в SDK: лишние аргументы молча игнорируются (модель не может «подсунуть» `confirmed: true` — поле просто не доходит до кода); JSON-число `0.1` в `Decimal`-поле становится `Decimal('0.1')` (через `str`), а схема `Decimal` по умолчанию — `anyOf: [number, string]`; чтобы модель не слала float (D-4 запрещает float в digest), денежные аргументы объявлять строкой с pattern по образцу `DecimalStr` выхода — решение тикета 011.

### ADR-014. Идемпотентность — в БД, ключ агента или отпечаток в окне 30 с
- **Контекст:** FR-26, FR-27, FR-47; спека MCP явно перекладывает идемпотентность на приложение (оборванный поток → клиент переотправляет).
- **Решение:** `idempotency_records(user_id, key, request_hash, status, result, expires_at)`. Явный ключ: `INSERT ... ON CONFLICT DO NOTHING RETURNING` → вставили — исполняем; нет — другой `request_hash` → `idempotency_conflict`, тот же → результат первого (или «в процессе» / тот же pending id). Без ключа: `fingerprint = hash(tool, canonical args)`, upsert `ON CONFLICT DO UPDATE ... WHERE expired RETURNING` (одна атомарная команда). `client_order_id` на бирже = `gx-` + детерминированный `action_id` — один источник для pending, записи идемпотентности и ордера (D-19). Автоматической повторной отправки нет (ADR-002, FR-27); отклонение дубликата `orderLinkId` биржей — вторая линия защиты, пока спайк 004 не подтвердит область уникальности. Неизвестный исход → `GET /v5/order/realtime?orderLinkId=` → `/v5/order/history`; «не найден нигде» = `UNKNOWN` (eventual consistency), не `FAILED`.
- **Последствия:** работает между клиентами одного пользователя (общая БД); тесты на гонку двух клиентов.
- **D-20. Идемпотентность и pending.** (1) Проверка повтора — до rate limit: повтор не тратит токен и не получает `rate_limited` вместо результата. (2) Вызов без ключа, чей digest совпадает с открытым `PENDING`, возвращает этот pending, пока тот не в терминальном статусе (иначе через 40 с создастся второй pending и человек одобрит оба); окно 30 с — для уже исполненных. (3) Fingerprint-окно — только для действий, увеличивающих риск: cancel, cancel-all, close, SL/TP, уменьшение плеча им не дедуплицируются (иначе `cancel_all → place → cancel_all` вернёт устаревший результат). (4) Записи с явным ключом живут 24 ч. (5) Запись обновляется финальным исходом executor; после `REJECTED/EXPIRED` тот же ключ возвращает тот же исход — «новая попытка — новый ключ» в описании инструментов.
- **D-21. FR-27 для действий без client order id.** Плечо — `position/list` → leverage; SL/TP — поля позиции; cancel/amend — статус ордера по `orderId` (amend и cancel-all у Bybit асинхронны: «принято» ≠ «применено»); cancel-all — открытые ордера из списка id. Совпало с целевым состоянием → `EXECUTED`, иначе `UNKNOWN` с подсказкой.

### ADR-015. Модель угрозы локального режима — агент с shell
- **Контекст:** research/confirmation-channel.md 2.5: Claude Code с Bash по умолчанию может читать почти всю машину, т.е. БД, master key, и вызвать CLI `approve`.
- **Решение:** рекомендованный локальный запуск — сервер в Docker (данные и master key в volume, недоступном агенту; клиент подключается к `docker run -i` по stdio); запуск без Docker поддерживается, но README и `doctor`-команда предупреждают. CLI-канал подтверждения в локальном режиме доверен только по явному включению. В README — раздел threat model: что гарантирует сервер, что — нет (агент с правами ОС на хост — вне гарантий AC-4), рекомендация deny-правил sandbox Claude Code.
- **Альтернативы:** отдельный пользователь ОС (сложнее «одна команда», платформенно-зависимо).
- **D-22. Честная граница.** Агент с shell в той же учётной записи ОС — вне гарантий: Docker не граница безопасности (группа `docker` → `docker exec`/mount volume → master key → торговля напрямую через API биржи), админ-CLI (включить CLI-approve, ослабить политику) доступен ему так же, как человеку; файл 0600 и checkpoint — та же учётная запись. Docker и 0600 — защита от случайного чтения. Гарантия AC-4 — гарантия поверхности MCP: модель без shell (Claude Desktop; Claude Code с deny-правилами на каталог данных, `docker` и CLI) не может исполнить действие без человека. **Mainnet в локальном режиме выключен**, пока не задан явный флаг `allow_local_mainnet` (предупреждение в `doctor`, запись в журнал); для mainnet рекомендуется удалённый режим или отдельный пользователь ОС (решение пользователя). Подтверждение ослабляющих команд CLI через Telegram — отклонено (сложность против выигрыша; агент с shell всё равно читает master key).

### ADR-016. Агент-клиент и evals — свой цикл на `mcp.Client` + порт `LLMProvider`
- **Контекст:** FR-39, FR-40, AC-5, AC-6.
- **Решение:** пакет `guarded_agent`: `mcp.Client` (stdio | HTTP | in-process `Client(MCPServer)`), `elicitation_callback` рисует консольную форму, конвертация `input_schema` в tools провайдера, адаптеры `OpenAIProvider` (Responses API, `gpt-6-luna`, reasoning effort none/low) и `AnthropicProvider` (Messages, `claude-haiku-5-5`, effort low). Evals: ≥ 40 кейсов (ru/en, ≥ 25% опасных) в jsonl с детерминированным эталоном (`call{tool, args⊂}` / `clarify` / `refuse`); сервер в evals — in-process на fake-бирже, оценивается **первое изменяющее решение**; 3–5 прогонов; prompt caching описаний; счётчик стоимости по `usage` и жёсткий стоп по бюджету $0.50; отчёт markdown с метриками и сравнением двух вариантов описаний (git-ref или версия `descriptions/*.yaml`).
- **Альтернативы:** MCP connectors провайдеров (нужен публичный HTTP, elicitation не обрабатывают, CI без сети невозможен); LangChain `MCPAdapter` (beta, прячет то, что надо показать).
- **Последствия:** evals запускаются вручную/по метке (требуют ключи и сеть), CI гоняет только unit-тесты раннера на записанных ответах.

### Спайк 002 (2026-10-27): возможности MCP Python SDK `mcp` 2.3.0
Проверено на закреплённой `mcp==2.3.0` / `mcp_types==2.3.0` (PyPI 2026-10-02), протокол 2026-07-28 (и legacy 2025-11-25 для сравнения), по исходникам установленного пакета и https://py.sdk.modelcontextprotocol.io/handlers/multi-round-trip/ (2026-10-27). Эксперименты — `spikes/002/` (запуск: `uv run pytest spikes/002 -v`, всё in-process под тем же сетевым guard, что `tests/`; HTTP — через `httpx2.ASGITransport`). Вывод: архитектура interface/mcp не меняется (high-level `MCPServer`, low-level `Server` не нужен); в `interface/mcp/server.py` появляется подкласс `GuardedMCPServer` с двумя override публичных методов.

| Вопрос | Ответ | Как / где |
|---|---|---|
| MRTR из high-level `@mcp.tool` | **Да.** | Аннотация `-> Annotated[CallToolResult, Out] \| InputRequiredResult`: SDK вырезает `InputRequiredResult` из union при выводе `outputSchema` и пропускает его в `convert_result` (`mcp/server/mcpserver/utilities/func_metadata.py`). Раунд 1: `ctx.request_state is None` (если при этом пришли `inputResponses` — `invalid_params`, fail closed) → вернуть `InputRequiredResult(input_requests={"confirm": ElicitRequest(params=ElicitRequestFormParams(message=..., requested_schema=...))}, request_state="<наш plaintext>")`. Раунд 2: `ctx.request_state` — наш plaintext (pending id); без `inputResponses` — снова спросить про тот же pending; подтверждение — `ctx.input_responses.get("confirm")` является `ElicitResult` с `action == "accept"` и `content.confirm is True`, всё остальное — отказ. SDK сам не связывает `inputResponses` с `requestState` (проверено). Ограничение: в одном инструменте нельзя сочетать `Resolve(...)`-параметры и `InputRequiredResult` из тела (`InvalidSignature`). `spikes/002/test_mrtr_high_level.py` |
| `requestState`: защита и ключи | SDK сам запечатывает/проверяет (middleware `RequestStateBoundary`). | AES-256-GCM от HKDF каждого ключа (`keys[0]` шифрует, все расшифровывают — ротация `[new, old]`), ключ ≥ 32 байт; claims: метод, имя инструмента, SHA-256 **сырых** `arguments` (sort_keys), `iat/exp`, `aud`, солёный хэш principal. Любой отказ — один `-32602 "Invalid or expired requestState"`, причина только в логе сервера. Подмена аргументов, порча токена, чужой principal, истёкший TTL, другой `aud` — отказ до вызова инструмента. Повтор того же токена принимается снова — single-use остаётся на CAS pending (ADR-005 п.3). `mcp/server/request_state.py`, `spikes/002/test_request_state_security.py` |
| Поведение после рестарта | Без `request_state_security` ключ эфемерный (`os.urandom` при старте) — рестарт ломает второй раунд. С `keys=[...]` из конфига — переживает (проверено двумя экземплярами сервера). | D-5: ключ — HKDF-подключ `request_state` от master key. |
| `audience` | По умолчанию — имя сервера (`MCPServer("guarded-exchange")`); сервер без имени с явной политикой не стартует. | Задаём `audience="guarded-exchange"` явно, чтобы переименование сервера не ломало токены в полёте. |
| Привязка к principal | По умолчанию `bind_principal=authenticated_principal`: `(client_id, iss, subject)` из `get_access_token()`; без токена (stdio, in-process) — `None`, состояние не привязано. | `TokenVerifier` (ADR-004) кладёт `user_id` в `AccessToken.subject` — тогда второй раунд с токеном другого пользователя отклоняется (проверено по HTTP). Локально пользователь один; сверка `pending.user_id` с principal (D-1) остаётся в любом случае. |
| Фильтрация `tools/list` без provisional API | **Да.** | Override `async def list_tools(self) -> list[Tool]` в подклассе `MCPServer`; principal — из contextvar (`get_access_token()` на HTTP — проверено внутри `list_tools`; локальный пользователь на stdio). Дефолт результата `ttlMs=0, cacheScope="private"`. Прямой `tools/call` скрытого инструмента доходит до инструмента → отказ обязан дать use case. `spikes/002/test_tools_list_and_client_info.py` |
| `clientInfo` и capabilities в контексте вызова | **Да.** | 2026-07-28: каждый запрос строит `Connection.from_envelope(_meta)`; `ctx.session.client_params.client_info` (`Implementation(name, version, ...)`) есть, если в `_meta` пришли и `clientInfo`, и `clientCapabilities` (иначе `client_params is None`, а `ctx.client_capabilities` всё равно заполнен); сырой `_meta` — `ctx.request_context.meta`. Legacy: то же из `initialize`. Для FR-36 — самоописание, не верифицируется; для выбора канала ADR-005 — `ctx.client_capabilities.elicitation.form` и `ctx.protocol_version`. `mcp/server/connection.py` |
| In-process `Client` и MRTR (e2e M3) | Прозрачно. | `Client(server, elicitation_callback=cb)` объявляет `elicitation: {form, url}` и сам повторяет вызов с ответами (`mcp/client/_input_required.py`, до `input_required_max_rounds=10`; результат только с `request_state` без вопросов — повтор с backoff 50→250 мс). Без колбэка capability не объявляется, а форма даёт `MCPError -32600 "Elicitation not supported"`. Ручной цикл (проверка подмены/повтора): `client.session.call_tool(..., allow_input_required=True, input_responses=..., request_state=...)`. `mode="legacy"` — для проверки push-elicitation. Principal для in-process тестов: contextvar, выставленный до `async with Client(...)`, наследуется задачей сервера. |
| Push `ctx.elicit()` на 2026-07-28 | Нет. | `NoBackChannelError` → протокольная ошибка `-32600`, не tool result. На legacy работает внутри вызова. |
| Единый маппинг ошибок валидации аргументов в `invalid_params` (вопрос ревью 001) | **Да**, override `MCPServer.call_tool`. | См. ADR-013 «Спайк 002». `spikes/002/test_invalid_params.py` |

Минимальный пример (форма из спайка, без доменной логики):
```python
class GuardedMCPServer(MCPServer[Any]):
    async def list_tools(self) -> list[Tool]:            # ADR-012
        tools = await super().list_tools()
        return [t for t in tools if t.name in self._visible_tools(current_principal())]

    async def call_tool(self, name, arguments, context=None):  # ADR-013
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if isinstance(exc, UnexpectedToolError) or not isinstance(exc.__cause__, ValidationError):
                raise
            return invalid_params_result(exc.__cause__)

@mcp.tool(name="place_order")
async def place_order(..., ctx: Context) -> Annotated[CallToolResult, OrderOut] | InputRequiredResult:
    if ctx.request_state is None:             # round 1 (D-1): the only round-2 marker is our sealed state
        if ctx.input_responses is not None:   # answers we never asked for -> fail closed
            return invalid_params_result_for("inputResponses without requestState")
        pending_id = ...                      # create pending (or dedupe by digest, D-20)
        return ask(pending_id)                # InputRequiredResult(input_requests={"confirm": ElicitRequest(...)},
                                              #                     request_state=pending_id)
    pending_id = ctx.request_state            # unsealed and verified by the SDK
    if ctx.input_responses is None:
        return ask(pending_id)                # state-only retry: same pending, never a new one
    answer = ctx.input_responses.get("confirm")
    confirmed = (isinstance(answer, ElicitResult) and answer.action == "accept"
                 and (answer.content or {}).get("confirm") is True)   # anything else = refusal
    ...
```

Для тикета 003: сервер спайка — `build_dual_era_server()` из `spikes/002/test_mrtr_high_level.py` (MRTR на 2026-07-28, `ctx.elicit` на legacy); логировать в stderr `protocol_version`, `client_info`, `client_capabilities.elicitation`, `action` ответа. Для тикета 020: digest в `requestState` SDK считает по сырым `arguments`, поэтому клиент обязан повторить аргументы байт-в-байт (Python `Client` так и делает); наш `action_digest` (D-4) — отдельная, нормализованная проверка поверх.

## 5. Модели данных

### 5.1 Домен (dataclasses, frozen)
- `Instrument(symbol, category: spot|linear, base, quote="USDT")`, `InstrumentRules(tick_size, qty_step, min_qty, min_notional, max_leverage)`.
- `TradingAction` — union: `PlaceOrder(instrument, side, qty, order_type: market|limit, price?, reduce_only)`, `AmendOrder(order_ref, qty?, price?)`, `CancelOrder(order_ref)`, `CancelAll(instrument)`, `SetTradingStop(instrument, stop_loss?, take_profit?)`, `ClosePosition(instrument, qty?)`, `SetLeverage(instrument, leverage)`.
- `RiskDirection = INCREASES | REDUCES` (FR-43), `classify(action, position, current_order?)`.
- `EffectivePolicy(profile: read_only|trade, environment, allowlist, max_order_notional, max_leverage, slippage_pct, confirm_rule, confirmation_ttl, trust_client_ui, prefer_external_channel, unconfirmed_actions_window, max_pending, kill_switch: none|user|admin)`; `strictest(ceiling, overrides)` по полям (D-23): профиль — min (read_only < trade); allowlist — пересечение; `trust_client_ui` — AND; `prefer_external_channel` — OR; TTL, slippage, лимиты, окна, `max_pending` — min; правила подтверждения — объединение множеств подтверждаемых типов действий и min порога X. `validate_override` отклоняет ослабление (FR-12).
- `PendingConfirmation(id, action_id, user_id, credential_id, environment, action, action_digest, risk_direction, shown_qty, price_snapshot, worst_price, status, created_at, expires_at, approved_at?, channel, decided_by?)` + функции переходов, возвращающие новый объект или `InvalidTransition`. Переходы: `PENDING → APPROVED → EXECUTING → EXECUTED|FAILED|UNKNOWN`; `PENDING → EXECUTING` (синхронный MRTR, D-2); `PENDING → REJECTED|EXPIRED|CANCELLED`; `APPROVED → EXPIRED_AFTER_APPROVAL|CANCELLED`; `UNKNOWN → EXECUTED|FAILED` (разведка).
- `AuditEvent(event_type, actor, payload)`.

### 5.2 Таблицы
| Таблица | Ключевые поля | Ограничения |
|---|---|---|
| `users` | id, name, status active/revoked, environment, kill_switch, created_at | — |
| `access_tokens` | id, user_id, token_hash, created_at, revoked_at | unique(token_hash) |
| `policies` | user_id, ceiling JSON, overrides JSON, updated_at | pk(user_id) |
| `exchange_credentials` | id, user_id, exchange, environment, ciphertext, nonce, key_version, permissions JSON, permissions_checked_at, admin_attested bool, status active/deleted, created_at | один active на user+exchange+environment (D-12) |
| `telegram_bindings` | user_id, telegram_user_id, chat_id, bound_at | unique(user_id), unique(telegram_user_id) |
| `pending_confirmations` | id, action_id, user_id, credential_id, environment, action JSON, action_digest, risk_direction, shown_qty, price_snapshot, worst_price, status, channel, created_at, expires_at, approved_at, decided_at, decided_by, executor_epoch, exec_started_at, telegram_message_id, exchange_result JSON | unique(action_id), index(user_id, status), index(status, expires_at) |
| `idempotency_records` | user_id, key_kind explicit/fingerprint, key, request_hash, status, result JSON, client_order_id, created_at, expires_at | unique(user_id, key_kind, key) |
| `executed_actions` | id, user_id, action_type, risk_direction, confirmed bool, client_order_id, executed_at | index(user_id, confirmed, executed_at) — для FR-46 |
| `audit_log` | chain_id, seq, ts, event_type, actor, payload JSON, prev_mac, mac, key_version | pk(chain_id, seq); триггеры append-only |
| `audit_heads` | chain_id, last_seq, last_mac | pk(chain_id) |
| `leases` | name, holder, epoch, expires_at | pk(name) — лидер в удалённом режиме; локально — flock + счётчик epoch (D-3) |

Деньги — TEXT через `TypeDecorator` (Decimal), время — INTEGER epoch ms (SQLite) / timestamptz (D-10).

### 5.3 Инструменты MCP (черновой список; финальные имена — в тикетах)
Чтение: `get_ticker`, `get_orderbook`, `get_candles`, `get_instrument_rules`, `get_balances`, `get_positions`, `get_open_orders`, `get_order_history`, `get_trades`, `get_my_policy`, `get_audit_log`, `get_confirmation_status`. Торговля: `place_order`, `amend_order`, `cancel_order`, `cancel_all_orders`, `set_trading_stop`, `close_position`, `set_leverage`. Служебное: `activate_kill_switch`, `cancel_confirmation` (D-6). Итого 21 (≈ 4–5k токенов описаний — замерить для evals).

## 6. Значения по умолчанию (FR-18, FR-34, FR-46, FR-48)

| Параметр | testnet (demo) | mainnet | Примечание |
|---|---|---|---|
| Макс. notional ордера | 1000 USDT | 50 USDT | SPEC FR-18 |
| Макс. плечо | ×10 | ×3 | SPEC FR-18 |
| Подтверждение | все торговые | все торговые | SPEC FR-19 |
| Доверие UI клиента | да | нет | SPEC FR-42 |
| TTL подтверждения | 2 мин (макс. админа ≤ 10) | 2 мин | SPEC FR-23 |
| Допуск slippage | 2% | 0.5% | жёсткий максимум 3% |
| Действий без подтверждения | 20 / 10 мин | 5 / 10 мин | работает, если админ ослабил подтверждения |
| Read на пользователя | bucket 20, +2/с | то же | |
| Write на пользователя (вкл. создание pending) | bucket 5, +1/2 с | bucket 3, +1/5 с | отмены не тратят |
| Одновременных pending | 10 | 5 | дополнение к SPEC |
| Окно идемпотентности без ключа | 30 с | 30 с | SPEC FR-26 |
| UID / IP бакеты биржи | 50% лимита Bybit | 50% | |

Обоснования и источники — research/defaults.md.

## 7. Интеграции
- **Bybit V5** (база `api-demo.bybit.com` / `api.bybit.com`): `GET /v5/market/instruments-info|tickers|orderbook|kline`, `GET /v5/account/wallet-balance`, `GET /v5/position/list`, `GET /v5/order/realtime|history`, `GET /v5/execution/list`, `POST /v5/order/create|amend|cancel|cancel-all`, `POST /v5/position/trading-stop|set-leverage`, `GET /v5/user/query-api`, `POST /v5/account/demo-apply-money` (CLI-утилита для демо). Права ключа FR-49 (D-24) — **allowlist**, а не denylist (NFR-1): `ContractTrade{Order,Position}`, `Spot{SpotTrade}`, `readOnly`; любое другое право, включая неизвестное, → отказ; `readOnly=1` → профиль чтения; проверяется `expiredAt`; пустой IP whitelist — предупреждение. Перепроверка при старте и перед изменяющим действием, если последняя старше 1 ч; расширение прав → изменяющие действия блокируются (`credentials_permissions_changed`). Если `query-api` нет на demo: classic testnet, если там есть; иначе для тестовой среды ключ принимается только после явного подтверждения админом в CLI (`admin_attested`, запись в журнал); mainnet — строго fail closed (решение пользователя, правка SPEC FR-49). Особые коды: 110043 (то же плечо) = успех; 110072/170141 (дубль orderLinkId) = «уже создан», запросить состояние; 10000/10016/170146 = исход неизвестен.
- **Telegram Bot API** через aiogram: `sendMessage` с inline-клавиатурой, `editMessageText` при решении/истечении, long polling `getUpdates` только у лидера.
- **LLM-провайдеры** — только агент-клиент и evals, не сервер.

## 8. Стратегия тестов
- **Unit (domain, application):** порты подменены fake-реализациями (`FakeExchange` с настраиваемыми ответами/таймаутами, `InMemoryRepositories`, `FakeConfirmationChannel`, `FakeClock`). Здесь — основная масса AC-3: классификация риска, политика и потолки, лимиты с округлением, автомат подтверждения, перепроверка FR-48, kill switch, FR-46, идемпотентность на уровне use case, маппинг ошибок. Каждому edge case из SPEC раздела 6 — именованный тест (`test_edge_<...>`), таблица соответствия FR → тест в `docs/traceability.md` (генерируется маркерами `@pytest.mark.req("FR-13")`).
- **Integration (infrastructure):** Bybit-клиент — `httpx2.MockTransport` на записанных фикстурах (подпись, маппинг, коды, заголовки лимитов, таймаут → unknown); БД — реальная SQLite во временном файле, позже матрица SQLite + Postgres (testcontainers); гонки: два клиента, один ключ идемпотентности; две конкурентные записи в цепочку; два процесса на одной SQLite; tamper-тест FR-50 (прямой UPDATE/DELETE/INSERT в обход триггеров → `verify` ловит); auth через реальный HTTP.
- **E2E:** in-process `Client(MCPServer)` + fake-биржа: сценарий AC-1 (позиции → стоп −3% → elicitation accept → журнал), pending-путь с `FakeConfirmationChannel`, AC-2 (два пользователя по HTTP, изоляция, read-only), AC-4 («агент» вызывает все инструменты с аргументами-«подтверждениями» и текстом → ничего не исполнено; проверка реестра).
- **Testnet (ручной, `-m testnet`, вне CI):** против Bybit demo: правила, ордер/отмена, trading-stop, unknown-разведка, query-api.
- **Производительность NFR-4 (D-25):** меряем серверный span `tools/call` минус время в порте `ExchangeClient` и ожидание человека; нагрузка — смесь read и write (place_order без подтверждения на fake-бирже, SQLite WAL `synchronous=NORMAL`), warmup, 1000 вызовов; результат с описанием железа — в README. CI-job — только сторож регрессии с мягким порогом (CI-раннер — не «локальная машина»).
- **CI:** ruff, ruff format --check, mypy --strict, import-linter, pytest с coverage (порог на domain/application ≥ 90%), без сети к биржам и LLM.

## 9. Риски
| Риск | Влияние | Митигация |
|---|---|---|
| Claude Code не поддерживает MRTR-elicitation на 2026-07-28 | AC-1 через UI клиента | спайк 003; запасные пути — legacy-режим на stdio или внешний канал (AC-1 это допускает) |
| ~~`InputRequiredResult` нельзя вернуть из `@mcp.tool`~~ | закрыт спайком 002: можно | — |
| Demo Bybit не поддерживает `query-api` / часть эндпоинтов | FR-49, тесты на demo | спайк 004; П-3 в SPEC; testnet как запасная среда |
| Несколько stdio-процессов: SQLite locks, конфликт Telegram polling, раздельные rate buckets | надёжность локального режима | WAL + IMMEDIATE + busy_timeout, lease лидера, тест двух процессов |
| Агент с shell читает БД/ключи (локально) | гарантии AC-4 | ADR-015: Docker-запуск, threat model в README |
| Фиктивный `decline` от Claude Desktop | неверная запись журнала | канал в журнале; не используем UI Desktop, пока capability не объявлена |
| Изменения биржевых лимитов и API | FR-35 | читать заголовки, числа в адаптере с датой проверки |
| Reasoning-токены в evals | бюджет $0.50 | effort low/none, кэш, жёсткий стоп |
| Python SDK MCP 2.x быстро меняется (middleware provisional) | поломки при обновлении | закрепить версию `==`, не строить на provisional API; overrides `list_tools`/`call_tool` (спайк 002) покрыты e2e-тестами; перед обновлением `mcp` прогнать `uv run pytest spikes/002` как канарейку |
| Объём проекта | сроки | milestones с работающей системой после каждого; удалённый режим и Postgres — после ядра |

## 10. Изменения SPEC (приняты пользователем 2026-10-27, внесены в SPEC — решения E-1…E-6)
- **П-1.** «testnet» в SPEC = «тестовая среда биржи»; для Bybit это Demo Trading (классический testnet — запасная). Затрагивает FR-18, FR-32, FR-42, FR-45, AC-1.
- **П-2.** FR-46: считать только действия, **увеличивающие риск**, выполненные без подтверждения (снижающие риск не ограничивать — иначе в панике нельзя быстро отменять ордера). Рекомендация research/defaults.md.
- **П-3.** FR-49 на demo, если `query-api` недоступен: fail closed (ключ отклоняется) — рекомендация; альтернатива — принять с профилем «только чтение» до ручного подтверждения админом.
- **П-4.** NFR-2 уточнить: «CI без обращений к внешним API (биржа, LLM, Telegram)»; загрузка Docker-образов и пакетов допустима.
- **П-5.** Добавить в FR-42/FR-20: канал CLI доверен только по явному включению админом; в журнале фиксируется канал решения, в том числе «declined by client UI».
- **П-6.** Максимум одновременных pending на пользователя (10 / 5) — новое требование к FR-19 (защита от усталости от подтверждений).

## 11. Milestones и тикеты (после grilling PLAN, D-27)
Порядок изменён по итогам grilling: минимальный append-only журнал — с первого среза с БД, `action_id` + идемпотентность + FR-27 — в первом торговом срезе, локальный CLI политики и kill switch — до HITL. Тикеты — `docs/tickets/NNN-*.md`; «👤» — нужен пользователь (ключи, ручная проверка клиента).
- **M0. Скелет + спайки:** 001 скелет и `get_ticker` насквозь; 002 спайк MCP SDK (InputRequiredResult из `@mcp.tool`, фильтрация `tools/list`, `clientInfo`, `RequestStateSecurity`); 003 👤 спайк MRTR-elicitation в Claude Code / Desktop; 004 👤 спайк Bybit demo (`query-api`, `orderLinkId`, коды, IOC, Set Limit Price Behaviour).
- **M1. Хранилище, политика, ключи, чтение:** 005 БД + `init` + локальный пользователь + минимальный журнал; 006 политика (потолок, overrides, CLI, `get_my_policy`); 007 credentials и FR-49; 008 чтение аккаунта; 009 чтение рынка; 010 история ордеров и сделок.
- **M2. Торговля без HITL (только testnet; подтверждение «всегда → fail closed», «без подтверждения» — только явной настройкой админа):** 011 `place_order` насквозь; 012 идемпотентность и неизвестный исход; 013 cancel/cancel-all/amend; 014 SL/TP, закрытие, плечо; 015 kill switch.
- **M3. Подтверждение человеком:** 016 pending + CLI-канал + executor; 017 полная перепроверка FR-48 и аннулирование; 018 Telegram; 019 несколько процессов и лидерство; 020 UI-канал MRTR (AC-1); 021 AC-4 adversarial e2e.
- **M4. Rate limits:** 022 бакеты трёх уровней + FR-35; 023 FR-46.
- **M5. Журнал:** 024 HMAC-цепочка + `audit verify`; 025 checkpoints + `get_audit_log`.
- **M6. Удалённый multi-tenant:** 026 Streamable HTTP + bearer-токены; 027 отзыв, mainnet, изоляция (AC-2); 028 Postgres + docker-compose.
- **M7. Агент и evals:** 029 `guarded_agent` (AC-5); 030 evals: кейсы и раннер; 031 эксперимент с описаниями (AC-6).
- **M8. Релиз:** 032 Docker-запуск, `doctor`, NFR-4 benchmark; 033 👤 README, threat model, GIF (AC-7).

Последовательно M0→M3; параллельно можно: 002 ‖ 004 ‖ 005 ‖ 009 после 001; 022 ‖ 024 ‖ 026 после M3; 029–031 после 020.

## 12. Журнал решений grilling PLAN (2026-10-27)
Рецензент (reviewer, режим grilling): 2 blocker, 15 major, 14 minor. Бесспорные правки внесены оркестратором в ADR с пометкой D-N; спорные решены пользователем.

**Blockers:** D-1 второй раунд MRTR — отдельная ветка до идемпотентности (иначе fingerprint 30 с возвращал бы снова `InputRequiredResult`, AC-1 ломался); D-2 один путь исполнения через CAS `PENDING→EXECUTING` / `APPROVED→EXECUTING` и общий `ExecuteConfirmed` (иначе двойное исполнение leverage/trading-stop без client id).

**Бесспорные правки (оркестратор):** D-3 лидерство flock/lease + epoch fencing, scan 1 с, дедлайн `approved_at + 30 с` (ADR-006); D-4 состав digest, канал на момент решения, `cancel` → CANCELLED (ADR-005); D-5 подключ `request_state`; D-7 детали Telegram-канала; D-8 таблица перепроверки FR-48 и TOCTOU под `lock_user`; D-9 FR-29 по статусам и soft-delete ключей; D-10 без сетевого I/O в транзакциях, `UnitOfWork(write)`, epoch ms; D-11 миграции только командой, проверки при старте; D-12 environment в credentials и pending; D-13 блокировка `audit_heads`; D-14 подключи HKDF и AAD токена бота; D-15 фактически исполненный объём IOC; D-16 честная оговорка про per-process бакеты, breaker, исключения из лимитов; D-17 резервирование слотов FR-46 и pending; D-18 новые коды ошибок; D-19 единый `action_id`; D-20 идемпотентность и pending; D-21 FR-27 для действий без client id; D-23 `strictest()` по полям; D-24 allowlist прав ключа; D-25 метод NFR-4; D-26 NFR-5 в порте; D-27 новый порядок milestones (раздел 11), спайки — отдельные тикеты.

**Решения пользователя:** clamp reduce-only «до N» (D-8, SPEC GP-2); объём вниз, цена — ошибка (SPEC GP-1); FR-49 на demo: classic testnet → подтверждение админа, mainnet fail closed (D-24, SPEC GP-3); mainnet локально только с флагом, подтверждение ослабления через Telegram отклонено (D-22); спот под kill switch — строго (README); `cancel_confirmation` для агента (D-6, SPEC GP-4); `prefer_external_channel` (D-6); перепроверка прав ключа при старте + раз в час (D-24).
