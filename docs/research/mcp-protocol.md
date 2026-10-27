# Research: транспорт, аутентификация, elicitation и pending-подтверждения в MCP 2026-07-28

Дата проверки всех фактов: **2026-10-27**. Исследователь: сабагент researcher (фаза Explore). Источники — только официальные: modelcontextprotocol.io (spec, changelog, schema, extensions), документация и PyPI Python SDK `mcp`, документация Claude Code (только как факт о клиенте).

## 1. Вопрос и затронутые требования

SPEC, раздел 9, пункт 2: транспорт и аутентификация MCP для удалённого (multi-tenant) и локального режима; как elicitation и pending-подтверждения сочетаются со stateless-спецификацией 2026-07-28.

Затронутые требования: FR-11 (видимость инструментов по профилю), FR-20/FR-42 (подтверждение человеком, доверие UI клиента), FR-21/FR-22/FR-23 (что показываем, привязка к параметрам, TTL), FR-25 (fail closed без канала), FR-26/FR-27 (повторы и неизвестный результат), FR-28 (классы ошибок), FR-29 (отзыв доступа), FR-33 (изоляция), FR-36 (какой клиент), FR-47/FR-48 (два исхода, исполнение сервером), NFR-1, NFR-2 (тесты без сети), NFR-3 (логи, request id), раздел 3 (два режима), AC-1/AC-2/AC-4.

## 2. Факты

### 2.1 Версия спецификации и что изменилось

- Текущая версия спецификации — **2026-07-28**. Версия передаётся в каждом запросе в `_meta["io.modelcontextprotocol/protocolVersion"]`, на Streamable HTTP дублируется заголовком `MCP-Protocol-Version`; несовпадение версии — `UnsupportedProtocolVersionError`. Есть обязательный RPC `server/discover` (версии, capabilities, identity сервера). Источник: https://modelcontextprotocol.io/specification/versioning (проверено 2026-10-27).
- Changelog 2026-07-28 относительно 2025-11-25 (https://modelcontextprotocol.io/specification/2026-07-28/changelog, проверено 2026-10-27), главное для нас:
  - Удалены protocol-level сессии и заголовок `Mcp-Session-Id`; `tools/list` и другие list-эндпоинты «no longer vary per-connection»; состояние между вызовами — через явные server-minted handles в аргументах инструментов (SEP-2567).
  - Удалён handshake `initialize`/`notifications/initialized`. Каждый запрос несёт версию и **capabilities клиента** в `_meta` (`io.modelcontextprotocol/clientCapabilities`); клиент SHOULD передавать `io.modelcontextprotocol/clientInfo` в каждом запросе (SEP-2575).
  - HTTP GET-эндпоинт и `resources/subscribe` заменены на `subscriptions/listen` — долгоживущий POST-ответ (SSE) для change-notifications, в т.ч. `toolsListChanged`.
  - Удалены `ping`, `logging/setLevel`; Roots, Sampling, Logging — Deprecated (SEP-2577). Рекомендация: в stdio логировать в `stderr` или OpenTelemetry.
  - **Server-initiated requests (`elicitation/create`, `sampling/createMessage`, `roots/list`) заменены паттерном Multi Round-Trip Requests (MRTR)**: сервер возвращает `InputRequiredResult` (`resultType: "input_required"`) с `inputRequests`, клиент повторяет исходный запрос с `inputResponses` (SEP-2322). Все результаты несут обязательное поле `resultType`.
  - Удалена resumability SSE (`Last-Event-ID`): оборванный поток теряет in-flight запрос, клиент MUST переотправить его как новый запрос с новым id.
  - Experimental tasks вынесены из ядра в официальное расширение `io.modelcontextprotocol/tasks` (polling `tasks/get`, `tasks/update`, без `tasks/list`) (SEP-2663).
  - Удалены `notifications/elicitation/complete` и `elicitationId` URL-mode (введённые в 2025-11-25): исход out-of-band взаимодействия клиент узнаёт повтором исходного запроса; корреляция — через `requestState`.
  - Обязательные заголовки `Mcp-Method`, `Mcp-Name` на POST; `ttlMs` и `cacheScope` обязательны в результатах `tools/list` и др.; `inputSchema`/`outputSchema` — любой JSON Schema 2020-12; Dynamic Client Registration — Deprecated в пользу Client ID Metadata Documents (CIMD); клиент MUST валидировать `iss` (RFC 9207).
- Для сравнения, 2025-11-25 относительно 2025-06-18 добавил URL-mode elicitation, experimental tasks, CIMD, OIDC Discovery, incremental scope consent, иконки (https://modelcontextprotocol.io/specification/2025-11-25/changelog, проверено 2026-10-27). То есть в 2025-06-18 был только form-mode elicitation как server→client запрос внутри сессии.

### 2.2 Транспорты

- Стандартные транспорты: **stdio** и **Streamable HTTP**; custom транспорты допускаются. Сообщения сервер→клиент — только responses и notifications: «servers do not initiate JSON-RPC requests». https://modelcontextprotocol.io/specification/2026-07-28/basic/transports (2026-10-27).
- stdio: newline-delimited JSON-RPC; сервер MUST NOT писать в `stdout` ничего, кроме MCP-сообщений, логи — в `stderr`; отмена — `notifications/cancelled`; при падении процесса in-flight запросы теряются, клиент может повторить. https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio (2026-10-27).
- Streamable HTTP: один endpoint, каждый запрос — отдельный POST, ответ — JSON или SSE-поток, привязанный к этому запросу; сервер MUST валидировать `Origin` (DNS rebinding), локально SHOULD слушать только 127.0.0.1, SHOULD иметь аутентификацию; закрытие SSE-потока ответа = отмена запроса, сервер SHOULD прекратить работу; старые клиенты с `Mcp-Session-Id` — сервер игнорирует заголовок; HTTP+SSE (2024-11-05) — Deprecated. https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http (2026-10-27).
- «Stateless» на практике: нет сессии и handshake; любой replica обслуживает любой запрос; сервер не может «позвонить» клиенту посреди вызова; всё, что нужно для продолжения, — либо в `requestState` (через клиента), либо в хранилище сервера по явному handle.

### 2.3 Authorization

- Authorization **OPTIONAL**. HTTP-транспорты, если поддерживают, SHOULD соответствовать спецификации; **stdio SHOULD NOT** следовать ей и берёт credentials из окружения. https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization (2026-10-27).
- При поддержке: MCP-сервер — OAuth 2.1 resource server; MUST реализовать Protected Resource Metadata (RFC 9728); клиент MUST использовать RFC 8707 `resource`; сервер MUST валидировать audience токена, MUST NOT принимать/передавать чужие токены (token passthrough запрещён); токен — только в `Authorization: Bearer`, не в query; 401 с `WWW-Authenticate: Bearer resource_metadata=..., scope=...`; 403 `insufficient_scope` для step-up. Регистрация клиента: CIMD (SHOULD), pre-registration, DCR (deprecated). Там же.
- Официальные auth-расширения (Stable, ревизия 2026-07-28): Enterprise-Managed Authorization, OAuth Client Credentials. https://github.com/modelcontextprotocol/ext-auth (2026-10-27).
- **Простые bearer API keys:** спецификация их прямо не запрещает и не описывает; MUST-требования относятся к случаю «implementations support authorization» по OAuth. Статический bearer-токен, выданный не OAuth AS, — отступление от SHOULD «conform to this specification», а не нарушение MUST транспорта. Это моя интерпретация текста, не прямая цитата.
- Python SDK официально показывает `StaticTokenVerifier` (lookup-таблица) как допустимый verifier «для demo/internal testing»; production — JWT или introspection. https://py.sdk.modelcontextprotocol.io/run/authorization/index.md (2026-10-27).
- Клиенты: Claude Code поддерживает `--transport http` с `--header "Authorization: Bearer ..."`, `headersHelper` (если helper вернул `Authorization`, Claude Code не переходит на OAuth), полноценный OAuth (`claude mcp login`), pre-registered `--client-id`. https://code.claude.com/docs/en/mcp (2026-10-27). Claude Desktop (custom connectors) — **не проверено** в рамках этого отчёта (вопрос SPEC 9.3).
- Spec elicitation: для удалённых серверов идентичность пользователя MUST выводиться из credentials MCP authorization (например `sub`), а не из того, что сообщил клиент. https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation (2026-10-27).

### 2.4 Elicitation в 2026-07-28

Источник: https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation и https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr (2026-10-27).

- Два режима: **form** (структурированные данные через клиента, плоская схема из примитивов: string/number/integer/boolean/enum) и **url** (out-of-band: пользователь открывает URL, данные клиенту не видны).
- Capability: клиент MUST объявить `elicitation` в `_meta.io.modelcontextprotocol/clientCapabilities` **каждого запроса**, например `{"elicitation": {"form": {}, "url": {}}}`; пустой `{}` = только form. Сервер MUST NOT слать `inputRequests` типов, которые клиент не объявил, и MUST NOT слать неподдерживаемые режимы. Если запрос без нужной capability обслужить нельзя — `MissingRequiredClientCapabilityError` (`-32021`, HTTP 400, `data.requiredCapabilities`). https://modelcontextprotocol.io/specification/2026-07-28/schema (2026-10-27).
- Server→client запрос посреди tool call в буквальном смысле **невозможен**. Вместо этого `tools/call` возвращает `InputRequiredResult`; исходный запрос завершён; клиент показывает UI, затем шлёт **новый** `tools/call` (новый JSON-RPC id, те же arguments) с `inputResponses` и эхом `requestState`. `InputRequiredResult` допустим только на `tools/call`, `prompts/get`, `resources/read`.
- `requestState` — attacker-controlled input: если влияет на авторизацию/бизнес-логику, сервер MUST защищать целостность (HMAC/AEAD) и SHOULD включать principal, короткий TTL, идентификатор исходного запроса (метод + digest параметров). Явное предупреждение: это **не гарантирует single-use** — одноразовость MUST обеспечиваться на стороне сервера.
- Сервер MUST NOT предполагать, что клиент ответит или повторит запрос; может повторно возвращать `InputRequiredResult`.
- Ответ: `accept` / `decline` / `cancel`. Для URL-mode `accept` означает лишь согласие открыть ссылку, не завершение; сервер на повторе по `requestState`/своему хранилищу решает, завершено ли взаимодействие, и может вернуть ещё один `InputRequiredResult`. Клиент MUST показать полный URL и получить согласие, MUST открыть URL так, чтобы клиент/LLM не видели содержимое; сервер MUST проверить, что ссылку открыл тот же пользователь (защита от фишинга), MUST NOT давать pre-authenticated URL.
- Безопасность: сервер MUST привязывать elicitation к client и user identity.
- Tools spec: «there SHOULD always be a human in the loop with the ability to deny tool invocations»; tool annotations для клиента — untrusted. https://modelcontextprotocol.io/specification/2026-07-28/server/tools (2026-10-27).

### 2.5 Tasks / long-running

- Tasks — расширение `io.modelcontextprotocol/tasks`: только для `tools/call`; сервер сам решает вернуть `resultType: "task"` (`CreateTaskResult` с `taskId`, `status`, `ttlMs`, `pollIntervalMs`), но только если клиент объявил extension в запросе; статусы `working`, `input_required`, `completed`, `failed`, `cancelled`; polling `tasks/get`; input через `tasks/update`; `tasks/cancel` кооперативный; task id с достаточной энтропией, auth-проверка на каждом task-запросе. https://tasks.extensions.modelcontextprotocol.io/specification/draft/tasks (2026-10-27). Статус документа (draft vs final) на странице однозначно не указан.
- **Python SDK v2 не реализует tasks extension**: «The SDK does not implement that extension yet»; experimental Tasks API v1 удалён. https://py.sdk.modelcontextprotocol.io/whats-new/ (2026-10-27).
- Claude Code упоминает ответы на input request задачи и отмену задач в non-interactive режиме — то есть клиент tasks знает; детали не проверялись. https://code.claude.com/docs/en/mcp (2026-10-27).
- Спецификация tools (non-normative «Stateful Tools»): состояние между вызовами — handle, возвращаемый одним инструментом и принимаемый другими; handle — имя, не capability, авторизацию проверять на каждом вызове; срок жизни описывать в description; истёкший handle — tool execution error. https://modelcontextprotocol.io/specification/2026-07-28/server/tools (2026-10-27).

### 2.6 Tools: annotations, список по пользователю, list_changed

Источник: https://modelcontextprotocol.io/specification/2026-07-28/server/tools и schema (2026-10-27).

- `tools/list` «MUST NOT vary per-connection», но «**MAY vary by the authorization presented on the request** — for example, returning only the tools the caller's granted scopes permit». Т.е. per-user список законен.
- `ListToolsResult` обязательно содержит `ttlMs` и `cacheScope`; `"private"` — кэш только в пределах того же authorization context (другой токен — другой кэш). Per-user список ⇒ `cacheScope: "private"`.
- Изменение списка: `notifications/tools/list_changed` идёт только клиентам, открывшим `subscriptions/listen` с `toolsListChanged: true`.
- `ToolAnnotations`: `title`, `readOnlyHint` (default false), `destructiveHint` (default true, смысл только при readOnly=false), `idempotentHint` (default false), `openWorldHint` (default true). Все — hints; клиенты не должны принимать решения по annotations от недоверенных серверов.
- Ошибки: protocol errors (JSON-RPC) vs tool execution errors (`isError: true`, видны модели для самокоррекции). Input validation — как tool execution error.
- Claude Code: при `list_changed` в интерактиве перечитывает tools/prompts/resources без переподключения. https://code.claude.com/docs/en/mcp (2026-10-27).

### 2.7 Python SDK `mcp`

- PyPI: последняя версия **2.3.0 (2026-10-02)**; 2.0.0 — 2026-07-28; параллельно поддерживается ветка 1.x (1.30.0 — 2026-09-07). PyPI указывает `Requires-Python >=3.10`, README репозитория — «Python 3.11+» (**расхождение**; проекту на 3.12 не мешает). https://pypi.org/project/mcp/, https://github.com/modelcontextprotocol/python-sdk (2026-10-27).
- Справка из памяти проекта **подтверждена**: `from mcp.server import MCPServer`; FastMCP переименован, старый путь удалён (не deprecated); `ctx.fastmcp` → `ctx.mcp_server`, `get_context()` удалён, `McpError` → `MCPError`; типы в пакете `mcp_types` (alias `mcp.types`); поля в Python — snake_case. SDK v2 обслуживает 2025-11-25 и 2026-07-28 с одного endpoint без флага. https://py.sdk.modelcontextprotocol.io/whats-new/ (2026-10-27).
- Сигнатуры (API reference https://py.sdk.modelcontextprotocol.io/api/mcp/server/mcpserver/server/, 2026-10-27):
  - `MCPServer(name=None, ..., token_verifier: TokenVerifier | None = None, *, auth: AuthSettings | None = None, request_state_security: RequestStateSecurity | None = None, cache_hints=..., subscriptions=..., middleware: Sequence[ServerMiddleware] | None = None, lifespan=..., ...)`
  - `@mcp.tool(name=None, title=None, description=None, annotations: ToolAnnotations | None = None, icons=None, meta=None, structured_output: bool | None = None)`
  - `mcp.run("stdio")` / `mcp.run("streamable-http", host=..., port=..., json_response=..., stateless_http=..., transport_security=...)`; `mcp.streamable_http_app(...) -> Starlette`.
- Context (https://py.sdk.modelcontextprotocol.io/api/mcp/server/mcpserver/context/, 2026-10-27): `ctx.protocol_version`, `ctx.client_capabilities` (`None`, если клиент ничего не объявил), `ctx.input_responses`, `ctx.request_state`, `ctx.request_id`, `ctx.headers` (client-supplied, не identity), `ctx.report_progress(...)`, `ctx.notify_tools_changed()`, `ctx.elicit(message, schema)`, `ctx.elicit_url(...)`. Отдельного метода «клиент поддерживает elicitation?» нет — проверять `ctx.client_capabilities.elicitation` вручную. `client_info` в Context **не найден**.
- Elicitation в SDK (https://py.sdk.modelcontextprotocol.io/handlers/elicitation/index.md, 2026-10-27):
  - Рекомендуемый путь, работающий на обеих эрах: параметр `Annotated[ElicitationResult[Model], Resolve(fn)]`, где `fn` возвращает значение или `Elicit("вопрос", Model)`; результат — `AcceptedElicitation | DeclinedElicitation | CancelledElicitation`.
  - `ctx.elicit()` / `ctx.elicit_url()` работают только на legacy-соединениях (≤ 2025-11-25); на 2026-07-28 бросают `NoBackChannelError`.
  - Если клиент не поддерживает elicitation — protocol error «Elicitation not supported», **исключение, а не decline**.
- MRTR вручную (https://py.sdk.modelcontextprotocol.io/handlers/multi-round-trip/index.md, 2026-10-27): handler может вернуть `InputRequiredResult(input_requests={...}, request_state=...)`; SDK сам запечатывает и проверяет `request_state`; `RequestStateSecurity(keys=[...], ttl=600 по умолчанию, bind_principal=..., audience=..., codec=...)`; ключ по умолчанию — per-process (рестарт/другой worker ⇒ `-32602 Invalid or expired requestState`). Важные caveats: на legacy-соединении `InputRequiredResult` даёт `-32603` — проверять `ctx.protocol_version`; **ответы привязаны к тексту вопроса** — если message строится из живых значений (цена, время), он меняется между раундами и сервер переспрашивает до лимита раундов (`input_required_max_rounds` = 10); инструмент с `Resolve(...)` не может одновременно вернуть `InputRequiredResult` из тела.
- Auth (https://py.sdk.modelcontextprotocol.io/run/authorization/index.md, 2026-10-27): `TokenVerifier.verify_token(token) -> AccessToken | None`; `AuthSettings(issuer_url, resource_server_url, required_scopes, validate_token_resource=True)`; SDK сам отдаёт PRM на `/.well-known/oauth-protected-resource/mcp` и 401 с `WWW-Authenticate`; идентичность внутри tool — `from mcp.server.auth.middleware.auth_context import get_access_token` → `AccessToken(token, client_id, scopes, resource, ...)`; per-tool проверка scopes — ответственность сервера; на stdio и в in-memory `Client(mcp)` **auth не применяется** (тестировать auth через реальный HTTP).
- Видимость инструментов per-user в `MCPServer`: **документированного хука нет**. Варианты — низкоуровневый `Server(on_list_tools=..., on_call_tool=...)` (handler `async (ctx, params) -> result`), либо `middleware` (помечен provisional: «may change in a 2.x minor release»; рекомендуют для observe/refuse, не как фундамент). https://py.sdk.modelcontextprotocol.io/advanced/middleware/index.md (2026-10-27). `MCPError` из middleware видит клиент, не модель; для модели — `is_error=True`.
- Structured output (https://py.sdk.modelcontextprotocol.io/servers/structured-output/index.md, 2026-10-27): возврат Pydantic-модели/TypedDict ⇒ автоматический `outputSchema` и `structured_content`, возврат валидируется; скаляры/списки оборачиваются в `{"result": ...}`; класс без type hints молча теряет schema.
- Annotations в SDK: `ToolAnnotations(read_only_hint=True, destructive_hint=..., idempotent_hint=..., open_world_hint=...)` (snake_case). https://py.sdk.modelcontextprotocol.io/servers/tools/index.md (2026-10-27).
- Тесты (https://py.sdk.modelcontextprotocol.io/get-started/testing/index.md, 2026-10-27): `async with Client(mcp, raise_exceptions=True) as c: await c.call_tool(...)` — in-memory, без транспорта; по умолчанию 2026-07-28; `mode="legacy"` — для push-elicitation. Клиент с MRTR: `Client(..., elicitation_callback=handler)` прозрачно повторяет вызов; ручной цикл — `client.session.call_tool(..., allow_input_required=True, input_responses=..., request_state=...)`.
- Клиент по HTTP с bearer: заголовки ставятся на `httpx2.AsyncClient(headers={"Authorization": "Bearer ..."})`, передаётся в `streamable_http_client(url, http_client=...)`; дефолтный timeout httpx2 — 5 с, SDK-дефолт read — 300 с. https://py.sdk.modelcontextprotocol.io/client/transports/index.md (2026-10-27).
- Deploy (https://py.sdk.modelcontextprotocol.io/run/deploy/index.md, 2026-10-27): без `transport_security` `streamable_http_app()` принимает только localhost Host (иначе 421); `TransportSecuritySettings(allowed_hosts=[...])`; для нескольких workers — общий ключ `RequestStateSecurity(keys=[key])` и одинаковое имя сервера/`audience`; для `list_changed` между репликами — свой `SubscriptionBus` (встроенный только in-process).

### 2.8 Факт о клиенте, важный для FR-42

- Claude Code объявляет `elicitation: {form: {}, url: {}}` на 2026-07-28 и на локально сконфигурированных серверах (v2.1.287+); для claude.ai connectors на старых ревизиях — `elicitation: {}` (только form). **Claude Code имеет `Elicitation` hook для автоответа без диалога.** https://code.claude.com/docs/en/mcp (2026-10-27). Это подтверждает допущение SPEC (раздел 2): UI-подтверждение даже «официального» клиента может быть автоматизировано пользователем/конфигом — сервер это не различит.

## 3. Варианты

### 3.1 Транспорт по режимам

| Вариант | Плюсы | Минусы |
|---|---|---|
| A. Локальный — stdio; удалённый — Streamable HTTP | Стандарт для Claude Desktop/Code локально; по spec на stdio auth не нужен; один и тот же `MCPServer`, разный `run()` | Внешний канал подтверждения (веб-страница/ссылка) в локальном режиме требует отдельного HTTP-listener рядом со stdio-процессом; логи строго в stderr |
| B. Оба режима — Streamable HTTP (локально на 127.0.0.1) | Одна кодовая дорожка, auth одинаково; веб-страница подтверждения в том же процессе | Пользователь должен держать сервер запущенным; нужен токен даже локально; хуже UX подключения Claude Desktop |
| C. stdio + Streamable HTTP, плюс локальный HTTP-эндпоинт только для страницы подтверждения | Сохраняет плюсы A, решает канал для URL-mode/ссылки | Два входа в один процесс, нужно аккуратно с lifecycle |

### 3.2 Аутентификация удалённого режима

| Вариант | Плюсы | Минусы |
|---|---|---|
| A. Opaque bearer-токены, выданные админом через CLI (хранятся хэшем), `TokenVerifier` → principal | Просто, без внешних зависимостей, тестируемо (NFR-2), мгновенный отзыв (FR-29: проверка в БД на каждом запросе); работает в Claude Code (`--header`/`headersHelper`) и в нашем агент-клиенте | Отступление от SHOULD (не OAuth AS); OAuth-only клиенты не подключатся; PRM у SDK требует `issuer_url` — если указывать фиктивный AS, OAuth-клиенты будут ломиться в него |
| B. Полный OAuth 2.1: внешний AS (Keycloak/иной), JWT-verifier, PRM, scopes | Spec-conformant, «настоящий» ответ на интервью, совместимость с OAuth-клиентами | Инфраструктура (AS в docker-compose), CIMD/redirects, сложнее e2e-тесты; отзыв доступа требует introspection или короткоживущих токенов + проверки в БД |
| C. A сейчас за портом `Authenticator` + B как отдельный адаптер позже | Слои по ENGINEERING.md, демонстрирует расширяемость, v1 быстро | Нужно не протечь «токен = пользователь» в domain; README должен честно описывать отступление |

Во всех вариантах: principal берётся только из `get_access_token()` (результат verifier), никогда из `clientInfo`/заголовков/аргументов (FR-33); на каждом вызове проверка «доступ не отозван» в хранилище (FR-29).

### 3.3 Подтверждение (FR-20/42/47) поверх stateless

| Вариант | Как | Плюсы | Минусы |
|---|---|---|---|
| A. Form-mode elicitation через MRTR (UI клиента) | Первый `tools/call`: валидация, политика, создание pending-записи в БД (id, digest параметров, principal, TTL, зафиксированная оценка цены) → `InputRequiredResult` с form-elicitation (текст FR-21) и `requestState` = {pending_id, principal, digest, exp} под AEAD. Повтор с `accept` → атомарный переход pending→confirmed→executing (single-use в БД), перепроверка FR-48, исполнение, результат — исход (а) FR-47 | Нативно для 2026-07-28, stateless-совместимо (любая реплика), модель не видит и не контролирует `inputResponses` (это поля протокола, не arguments) | Доверие = доверие программе клиента (hook автоответа в Claude Code; самописный клиент) ⇒ только где политика доверяет (testnet по умолчанию, FR-42); если клиент не повторил запрос — pending истекает по TTL, агент не получает ответа на этот вызов, только «результат неизвестен» на стороне клиента |
| B. URL-mode elicitation на нашу веб-страницу подтверждения | `InputRequiredResult` с `mode: "url"` на страницу `/confirm/<pending_id>` с аутентификацией пользователя в браузере; на повторе сервер смотрит статус pending; если ещё не подтверждено — снова `InputRequiredResult` или pending-ответ | Подтверждение вне клиента и вне LLM (клиент MUST открывать URL изолированно) — ближе к «внешнему каналу», пригодно для mainnet | Нужна веб-аутентификация пользователя (spec: phishing, MUST проверить того же пользователя), т.е. web-login, который SPEC выносит за non-goals (нет веб-интерфейса для пользователей) — нужна минимальная страница; повтор после `accept` может прийти раньше, чем человек нажал — нужна логика «подождать N секунд или вернуть pending» |
| C. Немедленный pending-результат с handle + внешний канал (мессенджер/ссылка) + read-инструмент статуса | Обычный `CallToolResult` (`resultType: complete`, structured: `{status: "pending_confirmation", confirmation_id, expires_at}`), уведомление во внешний канал, после подтверждения сервер исполняет сам (FR-47б) | Ровно паттерн spec «Stateful Tools» (handle + авторизация на каждом вызове); работает с любым клиентом, даже без elicitation (FR-25); подходит для mainnet | Требует фонового исполнителя и хранилища (сервер становится stateful по данным, но не по протоколу — это допустимо); агент должен опрашивать статус |
| D. Tasks extension (`resultType: "task"`) | `tools/call` возвращает task, человек подтверждает, `tasks/get` возвращает результат | Семантически ближе всего к FR-47б, клиент сам поллит | **Python SDK v2 не реализует extension** — пришлось бы писать вручную на low-level; клиентская поддержка не проверена; extension-статус неоднозначен. Для v1 — не брать |

Важные следствия для всех вариантов:
- `requestState` не даёт single-use (spec WARNING) ⇒ одноразовость подтверждения (FR-22) — только атомарным переходом состояния pending-записи в БД.
- Текст elicitation должен быть стабилен между раундами (SDK caveat): оценку market-цены и «худшую цену» (FR-21/48) фиксировать в pending-записи при первом вызове, а не пересчитывать.
- Повтор MRTR — это новый `tools/call` с теми же arguments; сервер MUST сверять digest arguments с тем, что в `requestState`/pending (FR-22, защита от подмены).
- FR-25 fail closed: проверять `ctx.client_capabilities.elicitation` (и режим form/url) **на каждом запросе** (capabilities теперь per-request) + политику доверия; если UI-канал недоступен и внешнего нет — tool execution error «требуется подтверждение, канал недоступен» (`isError: true`, FR-28), а не `MissingRequiredClientCapabilityError` (последний видит клиент, а не модель — хуже для evals и подсказки следующего шага).
- Legacy-клиенты (≤ 2025-11-25): `InputRequiredResult` на legacy даёт `-32603`; `Resolve(...)` работает на обеих эрах, но с ограничениями (нельзя вернуть `InputRequiredResult` из тела, стабильный текст). Ветвиться по `ctx.protocol_version`, либо поддерживать только 2026-07-28 и честно написать в README.

### 3.4 FR-11: видимость инструментов

| Вариант | Плюсы | Минусы |
|---|---|---|
| A. Статический список, отказ в `tools/call` | Просто, на `MCPServer` | Read-only агент видит торговые инструменты — шум в контексте, хуже evals; FR-11 допускает «явно отказывают», так что формально ок |
| B. Per-user `tools/list` по профилю (`cacheScope: "private"`, малый `ttlMs`) + обязательный отказ в `tools/call` | Spec прямо разрешает варьировать по authorization; least-privilege видно в UI; defense in depth | В `MCPServer` нет хука — low-level `Server` или provisional middleware; при смене профиля нужен `list_changed` через `subscriptions/listen` (в мульти-реплике — свой `SubscriptionBus`); клиенты кэшируют по `ttlMs` |
| C. B только для удалённого режима, A для локального | Меньше работы локально | Два поведения — сложнее тесты и README |

## 4. Рекомендация

1. **Транспорт:** вариант 3.1-A/C — локально stdio (`mcp.run("stdio")`, логи только в stderr), удалённо Streamable HTTP через `streamable_http_app()` с `TransportSecuritySettings(allowed_hosts=...)`, за ASGI. Целевая версия протокола — 2026-07-28; legacy-клиенты (2025-11-25) поддерживать в той мере, в какой SDK делает это сам, а подтверждение через UI на legacy — только если окажется дёшево (решить после проверки, на какой ревизии реально работает Claude Desktop, вопрос SPEC 9.3).
2. **Аутентификация:** вариант 3.2-C — порт аутентификации в application-слое; v1-адаптер — opaque bearer-токены, выдаваемые админ-CLI (хэш в БД, проверка «не отозван» на каждом запросе), через `TokenVerifier`; principal только из `get_access_token()`. OAuth 2.1 (JWT + PRM от реального AS) — отдельный адаптер/стретч-цель; в README честно: «static bearer tokens; OAuth-ready via adapter». Локальный режим: без токена, principal = единственный локальный пользователь из конфига (spec: stdio берёт credentials из окружения).
3. **Подтверждения:** комбинировать 3.3-A и 3.3-C, B — как реализация «внешнего канала по ссылке», если выберут ссылку (SPEC 9.4). Pending-запись в БД — единый источник истины для всех каналов (single-use, TTL, digest параметров, principal, зафиксированная цена). Решение на первом вызове: если политика доверяет UI клиента И клиент объявил `elicitation.form` в этом запросе И протокол 2026-07-28 → `InputRequiredResult` (исход FR-47а); иначе если настроен внешний канал → немедленный structured-результат `pending_confirmation` с id (FR-47б) + read-инструмент статуса; иначе → fail closed (FR-25). Tasks extension не использовать в v1 (нет в Python SDK).
4. **FR-11:** вариант 3.4-B — фильтрованный per-user `tools/list` (`cacheScope: "private"`) + обязательная проверка в `tools/call` (истинная граница — она). Это склоняет к low-level `Server` для `tools/list` или к собственной тонкой обёртке; решение — в PLAN (ADR), т.к. middleware provisional.
5. **Надёжность:** spec явно перекладывает идемпотентность на приложение (оборванный поток ⇒ клиент MUST переотправить новый запрос) — это прямое обоснование FR-26. Отмена (закрытие SSE / `notifications/cancelled`) не должна прерывать уже отправленный на биржу запрос — исполнение биржевого вызова и запись журнала экранировать от cancellation (FR-27, FR-29).
6. **Мелочи для PLAN:** `RequestStateSecurity(keys=[...], audience=...)` из конфига (иначе рестарт/второй worker ломает подтверждения в полёте); `clientInfo` из `_meta` — в журнал как «самоописание» (FR-36), в SDK Context доступа к нему не найдено — проверить в коде; trace context в `_meta` (OpenTelemetry) — для request id (NFR-3); annotations: read-инструменты `read_only_hint=True`, торговые `destructive_hint=True, idempotent_hint=False, open_world_hint=True`, отмена — `idempotent_hint=True` (но это только hints, никакой безопасности на них не строить); структурированные ответы — Pydantic-модели (автоматический `outputSchema`, FR-3, FR-28).

Пример сигнатур (иллюстрация API SDK v2, не код проекта):

```python
from typing import Annotated
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, RequestStateSecurity
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import ToolAnnotations, InputRequiredResult, ElicitRequest, ElicitRequestFormParams

mcp = MCPServer("guarded-exchange", token_verifier=..., auth=AuthSettings(...),
                request_state_security=RequestStateSecurity(keys=[...]))

@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                      idempotent_hint=False, open_world_hint=True))
async def place_order(..., ctx: Context) -> OrderOutcome | InputRequiredResult:
    principal = get_access_token()          # None на stdio -> локальный пользователь
    caps = ctx.client_capabilities          # per-request; caps.elicitation is None -> нет UI
    ctx.protocol_version, ctx.input_responses, ctx.request_state
```

Возможность объявить возвращаемый тип `X | InputRequiredResult` у `@mcp.tool` (а не только у low-level `Server` и `@mcp.prompt`) в документации прямо показана только для prompt и low-level tool — **проверить в первом тикете**.

### Что остаётся неизвестным

- Какую ревизию протокола и какие elicitation-режимы реально объявляет **Claude Desktop** на сегодня и поддерживает ли он MRTR и статические bearer-заголовки для удалённых коннекторов (SPEC 9.3, другой отчёт).
- Можно ли в `@mcp.tool` (high-level) вернуть `InputRequiredResult` из тела инструмента; как получить `clientInfo` в Context (в API reference не найден).
- Как `ctx.client_capabilities` ведёт себя на legacy-соединении (вероятно, из `initialize`) — проверить тестом.
- Поведение `tools/list`, фильтрованного по пользователю, при кэшировании конкретными клиентами (уважают ли `cacheScope`/`ttlMs`, реагирует ли Claude Desktop на `list_changed`).
- Статус tasks extension (draft/final) и сроки поддержки в Python SDK — на v1 не влияет.
- Обработка в клиентах повторного `InputRequiredResult` после URL-mode `accept` (если выбирать вариант 3.3-B): сколько ждут, есть ли ручной retry в UI Claude Code.
