# PLAN: guarded-exchange-mcp

Статус: **утверждён пользователем (2026-10-27)**, следующий шаг — grilling PLAN и нарезка тикетов. Документ отвечает на вопрос «как». Требования — в `SPEC.md` (ссылки FR/NFR/AC), факты и источники — в `docs/research/*.md` (проверены 2026-10-27). Пункты, требующие изменения SPEC, собраны в разделе 10 — без согласия пользователя они не применяются.

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
     infrastructure: bybit (httpx) · db (SQLAlchemy Core: SQLite|Postgres) · crypto · telegram · ratelimit
                                  │
                  ConfirmedActionExecutor (фоновая asyncio-задача, outbox по таблице pending)
```

Ключевой инвариант AC-4 — **структурный**: путь «подтвердить действие» (`ResolveConfirmation`) не вызывается ни из одного MCP-инструмента; его вызывают только Telegram-адаптер, CLI и обработчик ответа elicitation (который принимает поля протокола `inputResponses`, а не `arguments` модели — см. ADR-005). Это проверяется тестом на реестре инструментов и e2e-тестом «агент пытается подтвердить сам».

## 2. Стек (версии на 2026-10-27, фиксируются `uv.lock`)

| Компонент | Выбор | Версия | Источник |
|---|---|---|---|
| Язык | Python | 3.12 | ENGINEERING.md |
| Пакеты, lock | uv | актуальная на тикет 001 | — |
| MCP | `mcp` (Python SDK v2, `from mcp.server import MCPServer`), протокол 2026-07-28 | 2.3.0 | research/mcp-protocol.md |
| HTTP-клиент биржи | `httpx` (`AsyncClient`, `MockTransport` в тестах) | актуальная на 001 | research/exchange.md |
| Биржа | Bybit V5 REST, свой клиент (pybit 5.16.0 — только эталон подписи при отладке) | — | research/exchange.md |
| БД | SQLAlchemy 2.1 Core async (без ORM) + Alembic | 2.1.6 / 1.20.1 | research/storage-audit.md |
| Драйверы | `aiosqlite` (локально), `psycopg` 3 (удалённо) | 0.22.1 / 3.3.6 | research/storage-audit.md |
| Шифрование | `cryptography` (AES-256-GCM, HKDF) | 50.x | research/storage-audit.md |
| Telegram | `aiogram` (Bot API 10.3, long polling) | 3.31.0 | research/confirmation-channel.md |
| LLM (агент-клиент, evals) | `openai` (Responses API), `anthropic` (Messages) | 3.39.0 / 2.3.0 | research/clients-and-agent.md |
| Конфиг, валидация | `pydantic` v2, `pydantic-settings` | актуальные на 001 | ENGINEERING.md |
| CLI | `typer` | актуальная на 001 | — (выбор по умолчанию) |
| Логи | `structlog` (JSON в stderr; на stdio stdout занят протоколом) | актуальная на 001 | NFR-3, research/mcp-protocol.md |
| Тесты | `pytest`, `pytest-asyncio`, `respx`/`httpx.MockTransport`, `testcontainers[postgres]` | testcontainers 4.15.0 | research/storage-audit.md |
| Качество | `ruff`, `mypy --strict`, `pre-commit`, GitHub Actions | — | ENGINEERING.md |

Версии без числа проверяются и фиксируются в тикете 001 (не вспоминать — смотреть PyPI).

## 3. Структура и слои

```
src/guarded_exchange/
  domain/
    money.py            # Decimal-типы: Price, Qty, Notional, Percent; округление к шагу
    instruments.py      # Instrument, InstrumentRules (tick, qty step, min qty/notional, max leverage), validate (FR-10)
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
    submit_action.py    # единый путь любого торгового действия: auth → rate limit → idempotency → rules → policy/limits → confirm? → execute
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
- **Последствия:** спайк в тикете 001 руками против demo: доступны ли `query-api` и `instruments-info` на demo; область уникальности `orderLinkId`; семантика 10000/10016/170146 (считаем «неизвестно»). `cancel-all` на linear всегда с явным `orderFilter` (иначе снесёт TP/SL позиции). История закрытых ордеров ограничена (500 / 24 ч для отменённых) — происхождение «через систему» берём из своего журнала, а префикс `orderLinkId` — быстрый фильтр. Слово «testnet» в SPEC = тестовая среда биржи → см. раздел 10, П-1.

### ADR-002. Свой async-клиент Bybit на httpx за портом `ExchangeClient`
- **Контекст:** FR-27 (точные таймауты, неизвестный исход), FR-35 (заголовки лимитов), NFR-2 (без сети), NFR-5 (вторая биржа).
- **Решение:** `infrastructure/bybit/client.py` — подпись HMAC-SHA256, раздельные таймауты connect/read, без автоматических retry для изменяющих запросов (retry с backoff — только GET). Ответы маппятся в доменные объекты в `mapper.py`, коды ошибок — в доменные исключения в `errors.py`. Порт `ExchangeClient` описан в доменных терминах (instrument, side, qty, price, client_order_id), без слов Bybit.
- **Альтернативы:** pybit (синхронный, requests), ccxt (большая зависимость, скрывает именно то, что надо показать, своя нормализация ошибок).
- **Последствия:** ~300–500 строк своего кода и записанные фикстуры ответов; полный контроль; NFR-5 проверяем тем, что domain/application не импортируют `bybit`.

### ADR-003. Транспорт: stdio локально, Streamable HTTP удалённо; протокол 2026-07-28
- **Контекст:** SPEC раздел 3, 9.2. Спека 2026-07-28 stateless: нет сессий и `initialize`, capabilities клиента приходят в `_meta` каждого запроса, server→client запросов посреди вызова нет.
- **Решение:** один `MCPServer`, два способа запуска: `run("stdio")` (локальный режим, логи только в stderr) и `streamable_http_app()` за ASGI с `TransportSecuritySettings(allowed_hosts=...)`. Целевая ревизия — 2026-07-28; legacy-клиенты (2025-11-25) поддерживаем в той мере, в какой это делает SDK, а подтверждение через UI на legacy — только если спайк покажет, что это дёшево. `RequestStateSecurity(keys=[из конфига], audience=...)` — иначе рестарт ломает подтверждения в полёте.
- **Альтернативы:** HTTP и локально (одна дорожка, но хуже UX подключения и нужен токен даже локально).
- **Последствия:** в локальном режиме Telegram-бот и executor живут в том же stdio-процессе. Claude Code и Desktop запускают каждый свой процесс → несколько процессов на одной SQLite (ADR-007) и **несколько потребителей Telegram long polling** (409 Conflict). Решение: Telegram-поллер и executor берут файловую/БД-блокировку «leader»; процесс без лидерства только пишет pending и шлёт уведомления через Bot API (send не конфликтует). Проверить в спайке.

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
- **Альтернативы:** URL-mode elicitation на свою веб-страницу (нужна веб-аутентификация человека — по сути web-login, который SPEC выносит в non-goals; v2 с WebAuthn); ntfy (публичные топики, нет идентичности нажавшего); email; TOTP в чат (человек видит параметры в пересказе модели — нарушает FR-21); Tasks extension MCP (не реализован в Python SDK).
- **Последствия:** Claude Code поддерживает form-elicitation официально, но поддержку MRTR-формы на 2026-07-28 надо проверить руками (спайк 001); если нет — режим legacy для stdio или только внешний канал. Claude Desktop по открытому багу не объявляет elicitation (или присылает фиктивный `decline`) → для него внешний канал; фиктивный `decline` журналируется как «declined by client UI», а не «отклонено человеком» (канал записывается всегда). README: Claude Code `Elicitation` hook может автоответить — поэтому FR-42 не доверяет UI на mainnet. Возможность вернуть `InputRequiredResult` из high-level `@mcp.tool` — проверить в спайке; запасной путь — low-level `Server` для торговых инструментов.

### ADR-006. Исполнитель подтверждённых действий — фоновая задача + outbox по таблице pending
- **Контекст:** FR-47б (сервер исполняет сам), FR-48, FR-27, FR-29, рестарты.
- **Решение:** `ConfirmedActionExecutor` — asyncio-задача в процессе-лидере. Будится in-process сигналом после `APPROVED` и раз в N секунд сканирует `APPROVED` (сигнал не теряется). Шаги: CAS `APPROVED→EXECUTING` (один исполнитель) → полная перепроверка на момент исполнения (доступ, ключи = те же `credential_id`, kill switch, политика, лимиты, allowlist, граница цены) → отправка с детерминированным `client_order_id` из pending id → `EXECUTED | FAILED | UNKNOWN`. Вызов биржи и запись журнала экранированы от cancellation (`asyncio.shield`). Рестарт: `PENDING` живут до TTL; `APPROVED` исполняются только если не истёк `expires_at` исходного запроса (иначе `EXPIRED_AFTER_APPROVAL`); `EXECUTING` → разведка по `client_order_id` (FR-27).
- **Альтернативы:** отдельный воркер-процесс с очередью (RabbitMQ/Redis) — лишняя инфраструктура для v1; исполнение только при повторном вызове агента — нарушает FR-47.
- **Последствия:** v1 — один экземпляр сервера в удалённом режиме (ограничение в README); multi-instance — через leader election в БД, уже заложенный для локального режима.

### ADR-007. Хранилище — SQLAlchemy 2.1 Core, SQLite → Postgres, без Redis
- **Контекст:** SPEC 9.5; оба режима; конкурентные клиенты одного пользователя; «одна команда».
- **Решение:** Core (Table + выражения, без ORM), Alembic. Локально `sqlite+aiosqlite`: WAL, `busy_timeout`, `foreign_keys=ON`, write-транзакции `BEGIN IMMEDIATE`, проверка версии SQLite ≥ 3.35 при старте (RETURNING; fail closed). Удалённо `postgresql+psycopg`. Различия диалектов — в `db/dialect.py`: upsert, `lock_user()` (FOR UPDATE / IMMEDIATE), `TypeDecorator` Decimal-как-TEXT (на SQLite NUMERIC теряет точность) и UTC-время. Идемпотентность, окно 30 с, FR-46, pending и однократное исполнение — unique constraints, upsert и условные UPDATE. Rate limits — in-memory (ADR-011). Изоляция FR-33: каждый метод репозитория принимает `user_id` и включает его в WHERE; «не найдено» и «чужое» — одна ошибка.
- **Альтернативы:** raw SQL (две версии запросов), ORM (лишняя магия для простой модели), Postgres везде (локальному режиму нужен Docker), Redis (ещё один сервис ради того, что БД делает атомарно).
- **Последствия:** Postgres и матрица integration-тестов на двух БД — в milestone удалённого режима. Риск `database is locked` при нескольких stdio-процессах — тест с двумя процессами. Pull образа Postgres в CI — сетевая операция инфраструктуры (раздел 10, П-4).

### ADR-008. Журнал — per-user HMAC-цепочка + checkpoints вне БД + append-only триггеры
- **Контекст:** FR-36, FR-37, FR-38, FR-50.
- **Решение:** запись = `chain_id (user_id | "system"), seq, ts, event_type, actor, payload (canonical JSON, Decimal строками), prev_mac, key_version`, `mac = HMAC-SHA256(audit_key, canonical(record))`. Сериализация внутри цепочки — строка `audit_heads` под `lock_user` + `UNIQUE(chain_id, seq)`. Запись журнала — в той же транзакции, что изменение состояния; для биржи — `attempt` до отправки и `outcome` после. Триггеры `BEFORE UPDATE/DELETE → RAISE` (обе БД), в Postgres — роль приложения только `INSERT, SELECT` на `audit_log`. Периодический checkpoint `{chain_id: (seq, head)}` с HMAC — в файл вне каталога БД (локально) и в Telegram-чат админа (если привязан). CLI `audit verify` — полный проход, отчёт о первом расхождении.
- **Альтернативы:** SHA-256 цепочка без секрета (пересчитывается любым с доступом к БД); глобальная цепочка (горячая точка, пользователь видит чужие хэши); Merkle tree / Tessera (эффективные proofs для внешних аудиторов, которых нет; Python-реализации нет).
- **Последствия:** README — честная таблица: изменение/удаление/вставка — обнаруживаются; усечение хвоста после последнего checkpoint и атакующий с ключом и checkpoints — нет. Потеря master key = невозможность verify → backup в документации.

### ADR-009. Секреты — AES-256-GCM с AAD, один master key, HKDF-подключи
- **Контекст:** FR-31, FR-50; угроза — подмена шифротекста между пользователями (агент B торгует на аккаунте A).
- **Решение:** ключи биржи и токен Telegram-бота шифруются AES-256-GCM, случайный 96-битный nonce, AAD = `credential_id ‖ user_id ‖ exchange`. Master key — `SecretStr` из окружения; HKDF выводит подключи `credentials` и `audit`. Keyring `{version: key}`; CLI `rotate-keys` перешифровывает credentials, старые audit-ключи хранятся для verify. Локально `init` генерирует master key в файл 0600 вне каталога БД.
- **Альтернативы:** Fernet (нет AAD — копия строки в чужого пользователя расшифруется); envelope encryption (имеет смысл с KMS, в v1 — преждевременно).
- **Последствия:** секреты никогда не логируются (structlog-процессор маскирования + тест).

### ADR-010. Market-ордер исполняется как Limit IOC по худшей показанной цене
- **Контекст:** FR-13 (notional по худшей ожидаемой цене), FR-21, FR-48.
- **Решение:** worst = best встречной стороны × (1 ± s), округление к tick в безопасную сторону (buy вниз, sell вверх); notional для FR-13 — по округлённой границе и объёму после округления к qty step. Отправка: `orderType=Limit, timeInForce=IOC, price=worst`. Неисполненный остаток отменяется биржей; агент получает фактически исполненный объём. Для подтверждения цена фиксируется в pending (ADR-005).
- **Альтернативы:** нативный `slippageTolerance` Bybit — считается от опорной цены биржи (для деривативов — mark price) в момент матчинга, а не от показанной человеку → нарушает FR-48; есть не на всех биржах (NFR-5).
- **Последствия:** при внешнем канале и TTL 2 мин узкий допуск mainnet часто даст «исполнено 0» — безопасно; сообщение агенту объясняет, что делать. Проверить на demo «Set Limit Price Behaviour» (биржа может молча сдвинуть limit-цену в коридор) — выставить reject, если поддерживается.

### ADR-011. Rate limits — in-memory token buckets в три уровня
- **Контекст:** FR-34, FR-35, FR-46.
- **Решение:** (1) на пользователя — бакеты read и write (создание pending тратит write; отмены write-бакет не тратят — FIA 3.4); (2) на биржевой UID — 50% per-UID лимита Bybit по категории; (3) глобальный на egress IP — 50% IP-лимита (Bybit 600/5 с → 60/с). Остаток обновляется по `X-Bapi-Limit-Status`; на `10006`/HTTP 403 — circuit breaker до `X-Bapi-Limit-Reset-Timestamp` (по умолчанию 10 с; 403 — 10 мин). Ответ агенту — `rate_limited` с `retry_after`. FR-46 — не бакет, а sliding window log по таблице исполненных действий с резервированием слота под `lock_user` (иначе два клиента на границе пройдут оба).
- **Альтернативы:** Redis-лимитер (нужен только при нескольких экземплярах — v1 один).
- **Последствия:** в локальном режиме с несколькими stdio-процессами пользовательский бакет делится на процессы (приемлемо: биржевой UID-уровень всё равно защищает; отметить). Числа биржи — в адаптере, не в domain (NFR-5).

### ADR-012. Видимость инструментов: граница — проверка в `tools/call`; фильтрация `tools/list` — по результату спайка
- **Контекст:** FR-11, FR-44; в `MCPServer` нет документированного хука per-user `tools/list`, middleware SDK помечен provisional.
- **Решение:** права проверяются в use case на каждом вызове — это и есть граница безопасности. Сверху — фильтрованный `tools/list` по профилю (`cacheScope: "private"`), если спайк 001 покажет, что это делается чисто (override list handler низкоуровневого сервера при регистрации инструментов через `MCPServer`); иначе v1 отдаёт статический список, а read-only пользователь получает явный отказ `permission_denied` (FR-11 это допускает).
- **Альтернативы:** весь сервер на low-level `Server` (больше ручного кода схем); provisional middleware (может сломаться в 2.x minor).
- **Последствия:** evals измеряют, мешают ли видимые, но запрещённые инструменты (кейсы read-only профиля).

### ADR-013. Единая модель ошибок для агента
- **Контекст:** FR-28, FR-41, FR-3.
- **Решение:** доменные исключения → в `interface/mcp/errors.py` в tool execution error (`isError: true`) со structured content `{code, message, next_step, retry_after?, details}`; коды: `invalid_params`, `instrument_rule_violation`, `permission_denied`, `limit_exceeded`, `kill_switch_active`, `confirmation_required|pending|rejected|expired|channel_unavailable`, `idempotency_conflict`, `exchange_rejected`, `rate_limited`, `exchange_unavailable`, `exchange_credentials_invalid`, `outcome_unknown`, `not_found`. Protocol errors (`MCPError`) — только для ошибок протокола: их видит клиент, а не модель. Ответы — pydantic-модели (автоматический `outputSchema` + `structuredContent`) и краткий текст; свободный текст с биржи/из журнала — в поле `untrusted_text` с пометкой (FR-41).
- **Последствия:** маппинг покрыт таблично-параметризованным тестом; evals используют `code` для детерминированной оценки.

### ADR-014. Идемпотентность — в БД, ключ агента или отпечаток в окне 30 с
- **Контекст:** FR-26, FR-27, FR-47; спека MCP явно перекладывает идемпотентность на приложение (оборванный поток → клиент переотправляет).
- **Решение:** `idempotency_records(user_id, key, request_hash, status, result, expires_at)`. Явный ключ: `INSERT ... ON CONFLICT DO NOTHING RETURNING` → вставили — исполняем; нет — другой `request_hash` → `idempotency_conflict`, тот же → результат первого (или «в процессе» / тот же pending id). Без ключа: `fingerprint = hash(tool, canonical args)`, upsert `ON CONFLICT DO UPDATE ... WHERE expired RETURNING` (одна атомарная команда). `client_order_id` на бирже = `gx-` + детерминированный id из записи → повторная отправка безопасна (дубликат отклоняется биржей), неизвестный исход → `GET /v5/order/realtime?orderLinkId=` → `/v5/order/history`.
- **Последствия:** работает между клиентами одного пользователя (общая БД); тесты на гонку двух клиентов.

### ADR-015. Модель угрозы локального режима — агент с shell
- **Контекст:** research/confirmation-channel.md 2.5: Claude Code с Bash по умолчанию может читать почти всю машину, т.е. БД, master key, и вызвать CLI `approve`.
- **Решение:** рекомендованный локальный запуск — сервер в Docker (данные и master key в volume, недоступном агенту; клиент подключается к `docker run -i` по stdio); запуск без Docker поддерживается, но README и `doctor`-команда предупреждают. CLI-канал подтверждения в локальном режиме доверен только по явному включению. В README — раздел threat model: что гарантирует сервер, что — нет (агент с правами ОС на хост — вне гарантий AC-4), рекомендация deny-правил sandbox Claude Code.
- **Альтернативы:** отдельный пользователь ОС (сложнее «одна команда», платформенно-зависимо).

### ADR-016. Агент-клиент и evals — свой цикл на `mcp.Client` + порт `LLMProvider`
- **Контекст:** FR-39, FR-40, AC-5, AC-6.
- **Решение:** пакет `guarded_agent`: `mcp.Client` (stdio | HTTP | in-process `Client(MCPServer)`), `elicitation_callback` рисует консольную форму, конвертация `input_schema` в tools провайдера, адаптеры `OpenAIProvider` (Responses API, `gpt-6-luna`, reasoning effort none/low) и `AnthropicProvider` (Messages, `claude-haiku-5-5`, effort low). Evals: ≥ 40 кейсов (ru/en, ≥ 25% опасных) в jsonl с детерминированным эталоном (`call{tool, args⊂}` / `clarify` / `refuse`); сервер в evals — in-process на fake-бирже, оценивается **первое изменяющее решение**; 3–5 прогонов; prompt caching описаний; счётчик стоимости по `usage` и жёсткий стоп по бюджету $0.50; отчёт markdown с метриками и сравнением двух вариантов описаний (git-ref или версия `descriptions/*.yaml`).
- **Альтернативы:** MCP connectors провайдеров (нужен публичный HTTP, elicitation не обрабатывают, CI без сети невозможен); LangChain `MCPAdapter` (beta, прячет то, что надо показать).
- **Последствия:** evals запускаются вручную/по метке (требуют ключи и сеть), CI гоняет только unit-тесты раннера на записанных ответах.

## 5. Модели данных

### 5.1 Домен (dataclasses, frozen)
- `Instrument(symbol, category: spot|linear, base, quote="USDT")`, `InstrumentRules(tick_size, qty_step, min_qty, min_notional, max_leverage)`.
- `TradingAction` — union: `PlaceOrder(instrument, side, qty, order_type: market|limit, price?, reduce_only)`, `AmendOrder(order_ref, qty?, price?)`, `CancelOrder(order_ref)`, `CancelAll(instrument)`, `SetTradingStop(instrument, stop_loss?, take_profit?)`, `ClosePosition(instrument, qty?)`, `SetLeverage(instrument, leverage)`.
- `RiskDirection = INCREASES | REDUCES` (FR-43), `classify(action, position, current_order?)`.
- `EffectivePolicy(profile: read_only|trade, environment, allowlist, max_order_notional, max_leverage, slippage_pct, confirm_rule, confirmation_ttl, trust_client_ui, unconfirmed_actions_window, kill_switch: none|user|admin)`; `strictest(ceiling, overrides)`; `validate_override` отклоняет ослабление (FR-12).
- `PendingConfirmation(id, user_id, credential_id, action, action_digest, price_snapshot, worst_price, status, created_at, expires_at, channel, decided_by?)` + функции переходов, возвращающие новый объект или `InvalidTransition`.
- `AuditEvent(event_type, actor, payload)`.

### 5.2 Таблицы
| Таблица | Ключевые поля | Ограничения |
|---|---|---|
| `users` | id, name, status active/revoked, environment, kill_switch, created_at | — |
| `access_tokens` | id, user_id, token_hash, created_at, revoked_at | unique(token_hash) |
| `policies` | user_id, ceiling JSON, overrides JSON, updated_at | pk(user_id) |
| `exchange_credentials` | id, user_id, exchange, ciphertext, nonce, key_version, permissions JSON, status, created_at | один active на user+exchange |
| `telegram_bindings` | user_id, telegram_user_id, chat_id, bound_at | unique(user_id), unique(telegram_user_id) |
| `pending_confirmations` | id, user_id, credential_id, action JSON, action_digest, price_snapshot, worst_price, status, channel, created_at, expires_at, decided_at, decided_by, exchange_result JSON | index(user_id, status), index(status, expires_at) |
| `idempotency_records` | user_id, key_kind explicit/fingerprint, key, request_hash, status, result JSON, client_order_id, created_at, expires_at | unique(user_id, key_kind, key) |
| `executed_actions` | id, user_id, action_type, risk_direction, confirmed bool, client_order_id, executed_at | index(user_id, confirmed, executed_at) — для FR-46 |
| `audit_log` | chain_id, seq, ts, event_type, actor, payload JSON, prev_mac, mac, key_version | pk(chain_id, seq); триггеры append-only |
| `audit_heads` | chain_id, last_seq, last_mac | pk(chain_id) |
| `leases` | name, holder, expires_at | pk(name) — лидер Telegram-поллера/executor |

Деньги — TEXT через `TypeDecorator` (Decimal), время — UTC ISO / timestamptz.

### 5.3 Инструменты MCP (черновой список; финальные имена — в тикетах)
Чтение: `get_ticker`, `get_orderbook`, `get_candles`, `get_instrument_rules`, `get_balances`, `get_positions`, `get_open_orders`, `get_order_history`, `get_trades`, `get_my_policy`, `get_audit_log`, `get_confirmation_status`. Торговля: `place_order`, `amend_order`, `cancel_order`, `cancel_all_orders`, `set_trading_stop`, `close_position`, `set_leverage`. Служебное: `activate_kill_switch`. Итого 20 (≈ 4–5k токенов описаний — замерить для evals).

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
- **Bybit V5** (база `api-demo.bybit.com` / `api.bybit.com`): `GET /v5/market/instruments-info|tickers|orderbook|kline`, `GET /v5/account/wallet-balance`, `GET /v5/position/list`, `GET /v5/order/realtime|history`, `GET /v5/execution/list`, `POST /v5/order/create|amend|cancel|cancel-all`, `POST /v5/position/trading-stop|set-leverage`, `GET /v5/user/query-api`, `POST /v5/account/demo-apply-money` (CLI-утилита для демо). Права ключа FR-49: отклонять при `Withdraw`, `AccountTransfer`, `SubMemberTransfer`; `readOnly=1` → профиль чтения; пустой IP whitelist — предупреждение. Особые коды: 110043 (то же плечо) = успех; 110072/170141 (дубль orderLinkId) = «уже создан», запросить состояние; 10000/10016/170146 = исход неизвестен.
- **Telegram Bot API** через aiogram: `sendMessage` с inline-клавиатурой, `editMessageText` при решении/истечении, long polling `getUpdates` только у лидера.
- **LLM-провайдеры** — только агент-клиент и evals, не сервер.

## 8. Стратегия тестов
- **Unit (domain, application):** порты подменены fake-реализациями (`FakeExchange` с настраиваемыми ответами/таймаутами, `InMemoryRepositories`, `FakeConfirmationChannel`, `FakeClock`). Здесь — основная масса AC-3: классификация риска, политика и потолки, лимиты с округлением, автомат подтверждения, перепроверка FR-48, kill switch, FR-46, идемпотентность на уровне use case, маппинг ошибок. Каждому edge case из SPEC раздела 6 — именованный тест (`test_edge_<...>`), таблица соответствия FR → тест в `docs/traceability.md` (генерируется маркерами `@pytest.mark.req("FR-13")`).
- **Integration (infrastructure):** Bybit-клиент — `httpx.MockTransport`/`respx` на записанных фикстурах (подпись, маппинг, коды, заголовки лимитов, таймаут → unknown); БД — реальная SQLite во временном файле, позже матрица SQLite + Postgres (testcontainers); гонки: два клиента, один ключ идемпотентности; две конкурентные записи в цепочку; два процесса на одной SQLite; tamper-тест FR-50 (прямой UPDATE/DELETE/INSERT в обход триггеров → `verify` ловит); auth через реальный HTTP.
- **E2E:** in-process `Client(MCPServer)` + fake-биржа: сценарий AC-1 (позиции → стоп −3% → elicitation accept → журнал), pending-путь с `FakeConfirmationChannel`, AC-2 (два пользователя по HTTP, изоляция, read-only), AC-4 («агент» вызывает все инструменты с аргументами-«подтверждениями» и текстом → ничего не исполнено; проверка реестра).
- **Testnet (ручной, `-m testnet`, вне CI):** против Bybit demo: правила, ордер/отмена, trading-stop, unknown-разведка, query-api.
- **Производительность NFR-4:** benchmark-тест накладных расходов на fake-бирже, p95 ≤ 50 мс, в CI как отдельный job (не флейкающий порог — с запасом).
- **CI:** ruff, ruff format --check, mypy --strict, import-linter, pytest с coverage (порог на domain/application ≥ 90%), без сети к биржам и LLM.

## 9. Риски
| Риск | Влияние | Митигация |
|---|---|---|
| Claude Code не поддерживает MRTR-elicitation на 2026-07-28 | AC-1 через UI клиента | спайк 001; запасные пути — legacy-режим на stdio или внешний канал (AC-1 это допускает) |
| `InputRequiredResult` нельзя вернуть из `@mcp.tool` | архитектура interface/mcp | спайк 001; low-level `Server` для торговых инструментов |
| Demo Bybit не поддерживает `query-api` / часть эндпоинтов | FR-49, тесты на demo | спайк 001; П-3 в SPEC; testnet как запасная среда |
| Несколько stdio-процессов: SQLite locks, конфликт Telegram polling, раздельные rate buckets | надёжность локального режима | WAL + IMMEDIATE + busy_timeout, lease лидера, тест двух процессов |
| Агент с shell читает БД/ключи (локально) | гарантии AC-4 | ADR-015: Docker-запуск, threat model в README |
| Фиктивный `decline` от Claude Desktop | неверная запись журнала | канал в журнале; не используем UI Desktop, пока capability не объявлена |
| Изменения биржевых лимитов и API | FR-35 | читать заголовки, числа в адаптере с датой проверки |
| Reasoning-токены в evals | бюджет $0.50 | effort low/none, кэш, жёсткий стоп |
| Python SDK MCP 2.x быстро меняется (middleware provisional) | поломки при обновлении | закрепить версию в lock, не строить на provisional API |
| Объём проекта | сроки | milestones с работающей системой после каждого; удалённый режим и Postgres — после ядра |

## 10. Изменения SPEC (приняты пользователем 2026-10-27, внесены в SPEC — решения E-1…E-6)
- **П-1.** «testnet» в SPEC = «тестовая среда биржи»; для Bybit это Demo Trading (классический testnet — запасная). Затрагивает FR-18, FR-32, FR-42, FR-45, AC-1.
- **П-2.** FR-46: считать только действия, **увеличивающие риск**, выполненные без подтверждения (снижающие риск не ограничивать — иначе в панике нельзя быстро отменять ордера). Рекомендация research/defaults.md.
- **П-3.** FR-49 на demo, если `query-api` недоступен: fail closed (ключ отклоняется) — рекомендация; альтернатива — принять с профилем «только чтение» до ручного подтверждения админом.
- **П-4.** NFR-2 уточнить: «CI без обращений к внешним API (биржа, LLM, Telegram)»; загрузка Docker-образов и пакетов допустима.
- **П-5.** Добавить в FR-42/FR-20: канал CLI доверен только по явному включению админом; в журнале фиксируется канал решения, в том числе «declined by client UI».
- **П-6.** Максимум одновременных pending на пользователя (10 / 5) — новое требование к FR-19 (защита от усталости от подтверждений).

## 11. Milestones (черновик; нарезка в тикеты — после grilling PLAN)
- **M0. Скелет + спайки.** Структура по разделу 3, uv, ruff, mypy, import-linter, pytest, CI, pre-commit; один read-only инструмент (`get_ticker`) насквозь: MCP stdio → use case → Bybit-клиент → ответ, с тестами на MockTransport и in-process MCP-клиентом. Спайки (результат — заметки в PLAN): MRTR-elicitation в Claude Code; `InputRequiredResult` из `@mcp.tool`; фильтрация `tools/list`; demo Bybit (`query-api`, `orderLinkId`).
- **M1. Чтение рынка и аккаунта.** Все read-инструменты, credentials (CLI add/replace/delete, шифрование, проверка прав FR-49), локальный пользователь, SQLite + Alembic, LLM-friendly ответы и пометка недоверенного текста (FR-1–3, FR-30, FR-31, FR-41, FR-49).
- **M2. Политика и торговля без HITL.** Политика с потолком, классификация риска, правила инструментов, лимиты, kill switch, все торговые действия, Limit IOC для market, модель ошибок (FR-4–8, FR-10–18, FR-43–45). Подтверждение здесь — заглушка «всегда требуется → fail closed», чтобы не было небезопасного промежуточного состояния.
- **M3. Подтверждение человеком.** Pending, UI-канал (MRTR), Telegram + pairing, CLI-канал, executor, перепроверка FR-48, AC-1, AC-4 (FR-19–25, FR-42, FR-47, FR-48).
- **M4. Надёжность.** Идемпотентность (ключ + окно), unknown outcome и разведка, rate limits трёх уровней, FR-46 (FR-26–28, FR-34, FR-35, FR-46).
- **M5. Журнал.** HMAC-цепочка, append-only, checkpoints, `audit verify`, чтение журнала через MCP и CLI (FR-36–38, FR-50).
- **M6. Удалённый multi-tenant.** Streamable HTTP, bearer-токены, админ CLI (пользователи, токены, отзыв FR-29, mainnet FR-32), изоляция, Postgres + матрица тестов, docker-compose, AC-2 (FR-29, FR-32, FR-33, NFR-6).
- **M7. Агент-клиент и evals.** `guarded_agent` с двумя провайдерами и консольным подтверждением (AC-5); 40+ кейсов, раннер, отчёт, эксперимент с описаниями (AC-6).
- **M8. Релиз.** README на английском (архитектура, threat model, таблица клиентов с датой, tamper-evidence, evals), GIF демо, NFR-4 benchmark, одна команда запуска (AC-7).

Порядок M1→M5 последовательный (общие файлы application); внутри M1, M4, M7 возможна параллельная работа по независимым тикетам.
