# 026: Удалённый режим: Streamable HTTP и bearer-токены
Статус: IN_REVIEW
Зависит от: 006
Требования: FR-29 (проверка на каждом запросе), FR-33, ADR-004

## Цель
Сервер запускается по Streamable HTTP; админ выпускает пользователю токен (в БД — хэш); principal берётся только из токена; статус пользователя проверяется на каждом запросе.

## Scope
- `streamable_http_app()` за ASGI, `TransportSecuritySettings(allowed_hosts)`, `TokenVerifier`, порт `Authenticator`.
- CLI `admin user create|list`, `admin token issue|revoke`; журнал.
- Все use cases уже `user_id`-scoped; «не найдено» = «чужое».

## Non-scope
- Отзыв с аннулированием pending и AC-2 (027), Postgres (028).

## Acceptance criteria
- [x] Integration (реальный HTTP): без токена / с неверным / с отозванным → 401; principal не подменяется заголовками и аргументами.
- [x] Integration: токен хранится только хэшем.

## Заметки
- Из 006 (ревью): проверить на Streamable HTTP, валидирует ли SDK аргументы по `inputSchema` скрытого инструмента (параметры `Mcp-Param`/`_tool_input_schema`, который «вызывается до middleware и находит скрытые инструменты») **до** `GuardedMCPServer.call_tool`, где стоит проверка прав. Если да — запрещённый пользователь получит протокольную ошибку валидации вместо `permission_denied` и узнает схему скрытого инструмента. Закрыть тестом по HTTP и при необходимости отказом раньше (до валидации) или одинаковым ответом.

Решения и отклонения (2026-10-28, исполнитель):
- **AC-тесты** — `tests/integration/test_http_auth.py`, весь ASGI-стек SDK in-process (`httpx2.ASGITransport`), реальная SQLite, fake-биржа. 401: без заголовка, пустой/`Bearer`/`Basic`, мусор, well-formed неизвестный, выданный + лишний символ, отозванный токен, токен отозванного пользователя — × `tools/list`/`tools/call`, тело инструмента не выполняется. Отзыв токена у открытого клиента → следующий запрос 401 (статус ловится event hook httpx: клиент SDK превращает 401 в `MCPError -32603`), другой токен работает. Пользователь, отозванный после подключения, → 401. Подмена principal: заголовки `X-User-Id/X-Forwarded-User/X-Remote-User/X-MCP-Principal`, `clientInfo` с чужим именем, аргументы `user_id/user/principal` → видна только своя политика (alice `trade`, bob `read_only`, в обе стороны). Хэш: через настоящие функции `bootstrap` (`create_user`, `issue_token`) + HTTP-вызов с `GX_LOG_LEVEL=DEBUG`; в `access_tokens` — `(id, sha256)`, в `iterdump()`, байтах БД и `-wal`, логах секрета нет; журнал ссылается на `token_id`. Плюс: Host вне allowlist → 421; сбой проверки токена → 401 и лог только `error_type`; проводка `serve("http")` в composition root (uvicorn подменён драйвером, lifespan приложения запускается как у uvicorn).
- **Заметка из ревью 006 (Mcp-Param) — проверено по исходникам `mcp` 2.3.0, закрыто.** До dispatch SDK сверяет только заголовки `Mcp-Param-*` со свойствами, помеченными `x-mcp-header`; схему берёт `MCPServer._tool_input_schema`, которая видит скрытые инструменты. Общей валидации аргументов по `inputSchema` до `call_tool` нет (pydantic — после gate). Утечка возможна только при `x-mcp-header`, поэтому: тест реестра запрещает `x-mcp-header` в наших инструментах; канареечный тест фиксирует поведение SDK (скрытый инструмент с `x-mcp-header` → `MCPError` с `Mcp-Param-Symbol` вместо `permission_denied`); HTTP-тест: read-only пользователь, плохие аргументы + посторонние `Mcp-Param-*` → `permission_denied`, имя поля не раскрыто, у trade-пользователя — `invalid_params`. Отказ «до dispatch» через override приватного `_tool_input_schema` не делал (приватный API, сейчас не нужен) — записано в PLAN ADR-012.
- **Слои.** Порт `Authenticator` (application/ports.py) + `AuthenticateBearerToken` (application/auth.py); `TokenGenerator` — порт, `SecretTokenGenerator` в infrastructure; домен `access_tokens.py` (формат `gxt_`+43 base64url, проверка до хэширования, SHA-256, `hmac.compare_digest` повторно после поиска по уникальному хэшу). Interface `interface/mcp/http.py`: `BearerTokenVerifier`, `token_principal`, `auth_settings`, `create_http_server`, `http_app`, `serve_http`. `create_server`/`GuardedMCPServer` принимают `token_verifier`/`auth`; `app_server` — ровно один источник principal (`principal` для stdio или `authenticator` для HTTP).
- **Отказ один и тот же** (`None` → 401 `invalid_token`) для всех причин — клиент не узнаёт, существует ли токен/пользователь (FR-33). Trade-off: оператор видит в логе только `bearer_rejected` без причины; причину можно добавить в лог позже (без идентификаторов токена).
- **Секрет не живёт в контексте запроса:** `AccessToken.token="redacted"` (SDK поле `.token` не использует); `client_id` константный, пользователь — в `subject` (на нём же привязка `requestState` и legacy-сессий SDK). Ключ логов `token*` маскируется `mask_secrets`, поэтому `token_id` в логи не пишется вовсе.
- **`AuthSettings(issuer_url=<свой URL>, resource_server_url=None)`:** PRM не публикуется (AS нет), `WWW-Authenticate` без `resource_metadata`. `issuer_url` обязателен в модели SDK, но без провайдера не используется.
- **Хранение:** миграция **0005** `access_tokens` (FK users, unique `token_hash`, CHECK длины 64, `revoked_at` NULL); 0001–0004 не тронуты. `UserRepository.list`, `AccessTokenRepository(add/get/get_by_hash/revoke)`. Отзыв — `revoked_at`, строки не удаляются (журнал ссылается на id).
- **Журнал:** `user_created` (system, actor `cli:admin`; общий билдер события с `init`), `access_token_issued/revoked` в цепочке пользователя, payload — только `token_id`. Повторный revoke — no-op без записи («уже отозван»). Отозванному пользователю токен не выдаётся (`PermissionDenied`).
- **CLI:** `serve --transport stdio|http`; `admin user create NAME`, `admin user list`, `admin token issue --user NAME` (секрет печатается один раз в stdout с id и командой отзыва), `admin token revoke TOKEN_ID`. Ошибки — как у policy-команд (exit 1, одна строка в stderr).
- **Конфиг:** `GX_HTTP_HOST` (127.0.0.1), `GX_HTTP_PORT` (8000), `GX_HTTP_ALLOWED_HOSTS`, `GX_HTTP_ALLOWED_ORIGINS` (JSON-списки, по умолчанию loopback); DNS-rebinding защита включена всегда (SDK включает её сам только для loopback-хоста). Сервер — plain HTTP; README: наружу только за TLS-прокси.
- **Зависимости:** `uvicorn>=0.54.0`, `starlette>=1.10.0` объявлены явно (транзитивные от `mcp`, но импортируются напрямую); lock обновлён офлайн, версии те же.
- **Не сделано (не в scope):** `RequestStateSecurity` с ключом из master key (D-5) — в 020 вместе с MRTR; stateless/json_response-режимы оставлены по умолчанию SDK (legacy-сессии SDK привязывает к principal сам); отзыв пользователя с аннулированием pending — 027.
- Тестовая гигиена: тесты, перенаправляющие root logger, восстанавливают его фикстурой `restore_logging` (иначе последующие тесты писали в закрытый capture-поток).

Предложения (вне scope):
- `admin token list [--user]` (id, пользователь, создан, отозван) — сейчас id токена виден только при выдаче и в журнале.
- Срок жизни токена (`expires_at`; SDK уже отклоняет `AccessToken.expires_at` в прошлом) и `last_used_at` для аудита неиспользуемых токенов.
- Причина отказа аутентификации в логе (`unknown|revoked|user_inactive|malformed`) без идентификаторов — для диагностики оператором.
- Rate limit на 401 по IP (перебор токенов бессмыслен при 256 битах, но шум в логах и нагрузка на БД) — в 022.
