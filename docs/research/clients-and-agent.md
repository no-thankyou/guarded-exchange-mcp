# Research: MCP-клиенты, агент-клиент и стоимость evals

Дата проверки всех фактов: **2026-10-27** (если не указано иное). Исследователь: сабагент `researcher`, фаза Explore.

## 1. Вопрос и затронутые требования SPEC

Вопросы:
1. Какие MCP-возможности на сегодня поддерживают Claude Code (CLI), Claude Desktop, а для справки Cursor, VS Code (Copilot), ChatGPT/OpenAI: elicitation (form/URL mode), remote Streamable HTTP, OAuth / bearer-токены в конфиге, tool annotations, structured output, `list_changed`. Какой клиент и канал подтверждения выбрать для AC-1 (локальный режим, stdio).
2. Как на Python сделать консольный MCP-клиент с обработкой elicitation и переключаемым провайдером LLM (OpenAI Responses / Anthropic Messages); актуальные версии `mcp`, `openai`, `anthropic`; свой цикл или MCP connector провайдера.
3. Какие дешёвые модели с tool calling позволяют прогнать ≥ 40 кейсов × несколько раз дешевле $0.50; оценка стоимости.

Затронутые требования: FR-20 (подтверждение получает человек), FR-25 (fail closed без доверенного канала), FR-42 (доверие UI-подтверждению клиента — настройка политики), FR-47 (синхронный исход vs «ожидает»), FR-39 (агент-клиент), FR-40 (evals, < $0.50), NFR-2 (CI без сети), NFR-6 (честная таблица клиентов в README), AC-1, AC-4, AC-5, AC-6.

## 2. Факты с источниками

### 2.1 Claude Code (CLI)

- Последняя версия — **2.1.311 от 2026-10-26** ([changelog](https://code.claude.com/docs/en/changelog), 2026-10-27).
- **Elicitation поддерживается, form и URL mode.** Цитата: «Claude Code displays an interactive dialog and passes your response back to the server. No configuration is required on your side». Form mode — диалог с полями сервера; URL mode — Claude Code спрашивает, открыть ли ссылку в браузере; длина URL ограничена (~8000 символов без escape, ~4000 при сплошных `%`-escape) ([docs: MCP → Respond to MCP elicitation requests](https://code.claude.com/docs/en/mcp), 2026-10-27).
- Объявляемые capabilities: `elicitation: {form: {}, url: {}}` — на соединениях с протоколом **2026-07-28** и на соединениях со старыми ревизиями (2025-11-25 и раньше) к stdio/sse/http/ws серверам из пользовательского конфига, **начиная с v2.1.287**. На прочих соединениях старых ревизий (например, claude.ai connectors) — голое `elicitation: {}`, то есть только form mode (там же).
- Elicitation впервые появилась в **v2.1.76 (13–14 марта 2026)**: «MCP servers can now request structured input mid-task via an interactive dialog (form fields or browser URL)»; в том же релизе — hooks `Elicitation` и `ElicitationResult` (по зеркалам changelog: [havoptic](https://www.havoptic.com/r/claude-code-2.1.76), [claude-world](https://claude-world.com/articles/claude-code-2176-release/); вторичные источники расходятся в дате на 1 день; в официальном changelog строку 2.1.76 я не дочитал — страница > 1 МБ).
- **Важно для FR-42:** `Elicitation` hook позволяет **автоматически отвечать** на elicitation без показа диалога: «To auto-respond to elicitation requests without showing a dialog, use the Elicitation hook» ([docs](https://code.claude.com/docs/en/mcp)). То есть даже в официальном клиенте ответ на form-elicitation может дать не человек, а конфиг пользователя. Это прямое подтверждение границы доверия из SPEC раздел 2.
- v2.1.310: «Changed the error for an MCP tool call that times out in a non-interactive session while the server's elicitation is unanswered: it now tells Claude that no answer came and not to repeat the call» ([changelog](https://code.claude.com/docs/en/changelog)).
- Remote: `claude mcp add --transport http …`; «HTTP servers are the recommended option»; в JSON `type: "streamable-http"` — алиас `http`; SSE deprecated ([docs](https://code.claude.com/docs/en/mcp)).
- Auth: OAuth 2.0 (вход через `/mcp` или `claude mcp login <name>`; при отсутствии DCR — `--client-id/--client-secret/--callback-port`; Claude Code использует свой CIMD и loopback redirect); статические заголовки `--header "Authorization: Bearer …"`; динамические — `headersHelper` (shell-команда, печатает JSON заголовков, таймаут 10 с, перезапуск на 401/403; в проектном `.mcp.json` выполняется только после trust dialog) ([docs](https://code.claude.com/docs/en/mcp); [Claude auth для коннекторов](https://claude.com/docs/connectors/building/authentication.md)).
- Tool annotations: `readOnlyHint` — вызовы выполняются параллельно с другими read-only; `idempotentHint`/`readOnlyHint` — только такие вызовы Claude Code повторяет после обрыва HTTP-соединения (v2.1.307: «only read-only or idempotent tools are now retried»). Есть вендорское `_meta["anthropic/requiresUserInteraction"]: true` — permission prompt на каждый вызов даже в `bypassPermissions`, без «don't ask again»; в `dontAsk` вызов запрещается ([docs](https://code.claude.com/docs/en/mcp), [changelog](https://code.claude.com/docs/en/changelog)).
- `list_changed`: в интерактивной сессии Claude Code перечитывает tools/prompts/resources; в `-p` и Agent SDK — только tools ([docs](https://code.claude.com/docs/en/mcp)).
- Structured output (`outputSchema`/`structuredContent`): в документации Claude Code явного описания **не нашёл** — не подтверждено.
- Лимиты: вывод инструмента > 10k токенов — warning, по умолчанию лимит 25k (`MAX_MCP_OUTPUT_TOKENS`); `MCP_TOOL_TIMEOUT` / per-server `timeout`; idle timeout вызова без ответа/progress — 5 мин для HTTP, **30 мин для stdio** ([docs](https://code.claude.com/docs/en/mcp)). TTL подтверждения 2 мин (FR-23) в эти таймауты укладывается.
- Протокол 2026-07-28: v2.1.299 — cloud/Remote Control коннекторы по умолчанию договариваются на 2026-07-28; v2.1.302 — исправление для серверов 2026-07-28 без `resultType`/`ttlMs`/`cacheScope` ([changelog](https://code.claude.com/docs/en/changelog)).

### 2.2 Claude Desktop (и claude.ai / mobile / Cowork)

- Официальная документация для авторов серверов: Claude поддерживает tools, prompts, resources; **не поддерживает** «Resource subscriptions, Sampling, Advanced or draft capabilities». Elicitation в списке поддерживаемого **не упомянута** ни в плюс, ни в минус ([Build an MCP server for Claude](https://claude.com/docs/connectors/building), 2026-10-27).
- Транспорт remote: Streamable HTTP; legacy HTTP+SSE поддерживается, но deprecated. Remote-серверы добавляются как custom connector в UI (Settings → Connectors), а не через `claude_desktop_config.json` (там же; [support.claude.com](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)). Важно: claude.ai/Desktop подключается к remote-серверу **с инфраструктуры Anthropic** (egress `160.79.104.0/21`), т.е. сервер должен быть доступен из интернета ([auth](https://claude.com/docs/connectors/building/authentication.md)).
- Auth для remote: OAuth (DCR, CIMD, Anthropic-held creds) по умолчанию; **статический bearer/API-key заголовок (`static_headers`) — beta, «for a limited set of organizations»**; `client_credentials` (M2M) не поддерживается ([auth](https://claude.com/docs/connectors/building/authentication.md)).
- Tool annotations: «All MCP tools must declare both … readOnlyHint … destructiveHint» ([MCP orientation](https://claude.com/docs/connectors/building/mcp.md)); «These determine auto-permissions in Claude. Read-only tools can run without per-call confirmation, and destructive tools always prompt» ([review criteria](https://claude.com/docs/connectors/building/review-criteria.md)). Это permission prompt клиента — сервер его не видит и не может проверить.
- Лимиты hosted surfaces: результат ~150 000 символов, **таймаут вызова инструмента 240 с** ([building](https://claude.com/docs/connectors/building)).
- Локальные серверы — через `claude_desktop_config.json` или пакет MCPB ([MCPB](https://claude.com/docs/connectors/building/mcpb.md); [Anthropic engineering](https://www.anthropic.com/engineering/desktop-extensions)).
- **Elicitation в Desktop — по имеющимся данным не работает.** Официального подтверждения ни «да», ни «нет» нет. Свидетельства:
  - Issue [anthropics/claude-code#96043](https://github.com/anthropics/claude-code/issues/96043) (открыт 2026-09-22, статус **open**, метки `bug`, `area:desktop`, `area:mcp`, `has repro`, последние комментарии 2026-10-13/23 «всё ещё воспроизводится»; ответа Anthropic нет): в Desktop 2.2553.1/2.2553.13 локальные серверы видят клиента `local-agent-mode-<server>` с capabilities `roots` + `extensions["io.modelcontextprotocol/ui"]`, **без `elicitation`**; при этом, по выводу автора из кода (не с провода), встроенный Agent SDK отвечает на `elicitation/create` **`{"action":"decline"}` без показа диалога**. Тот же сервер в standalone CLI получает рабочую форму.
  - Feature request [#41110](https://github.com/anthropics/claude-code/issues/41110) (март 2026) — «elicitation only available in the CLI»; закрыт автором, официального ответа нет.
- MCP Apps (`io.modelcontextprotocol/ui`) Desktop объявляет (по issue #96043). Отдельная официальная страница MCP Apps: [getting started](https://claude.com/docs/connectors/building/mcp-apps/getting-started.md) — не исследовал подробно.
- Политика каталога: коннекторы, которые «Transfer money, cryptocurrency, or other financial assets», в Directory не принимаются ([review criteria](https://claude.com/docs/connectors/building/review-criteria.md)). Для нас не блокер (публикации в Directory нет, переводы — non-goal FR-9), но фраза для README полезна.

### 2.3 Cursor

- Транспорты stdio, SSE, Streamable HTTP; OAuth; `headers` в `mcp.json` (`"Authorization": "Bearer ${env:MY_SERVICE_TOKEN}"`); протокольные возможности: Tools, Prompts, Resources, Roots, **Elicitation**, Apps ([cursor.com/docs/context/mcp](https://cursor.com/docs/context/mcp), 2026-10-27).
- Не подтверждено: разделение form/URL mode, tool annotations, structured output, `list_changed`, sampling.

### 2.4 VS Code (GitHub Copilot)

- Транспорты: stdio, Streamable HTTP (`http`), SSE (legacy). Возможности: Tools, Prompts, Resources, **Elicitation**, **Sampling**, OAuth, server instructions, Roots, MCP Apps. Annotations: `title`, `readOnlyHint` ([MCP developer guide](https://code.visualstudio.com/api/extension-guides/ai/mcp), страница датирована 10/21/2026). По вторичному пересказу той же страницы: VS Code спрашивает подтверждение перед вызовом инструмента без `readOnlyHint` (в моей выборке цитаты нет — проверить).
- Пользовательская страница ([mcp-servers](https://code.visualstudio.com/docs/copilot/chat/mcp-servers)): remote «HTTP (HTTP or Server-Sent Events)», local stdio, MCP Apps (Experimental). Headers/inputs — в [configuration reference](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration) (не дочитан).
- Не подтверждено: form vs URL mode, structured output, `list_changed`.

### 2.5 ChatGPT и OpenAI API

- ChatGPT подключает только **remote** MCP (Streamable HTTP, обычно `/mcp`); для приватных/локальных серверов — **Secure MCP Tunnel** (stdio или HTTP) ([connect-chatgpt](https://developers.openai.com/plugins/deploy/connect-chatgpt)). Auth: OAuth 2.1, CIMD (public client `none`, `private_key_jwt`), DCR «remains supported» ([api/docs/mcp](https://developers.openai.com/api/docs/mcp/)). Статические bearer-заголовки в ChatGPT — **не подтверждено**.
- Annotations: `readOnlyHint`, `destructiveHint`, `openWorldHint`; «Annotations help ChatGPT and Codex choose appropriate confirmation and safety behavior» и «do not replace authorization, validation, or confirmation in your server»; «ChatGPT currently requires manual confirmation in any conversation before write actions can be taken» ([plugins/build/mcp-server](https://developers.openai.com/plugins/build/mcp-server); [api/docs/mcp](https://developers.openai.com/api/docs/mcp/)).
- Structured output: рекомендуют `outputSchema` и `structuredContent` + дубль JSON-строкой в `content` для совместимости (там же).
- Elicitation: гайд для авторов серверов пишет «Use MCP elicitation when the server needs structured information…» ([mcp-server](https://developers.openai.com/plugins/build/mcp-server)) — это намёк на поддержку, но **явного подтверждения, что ChatGPT показывает elicitation (и какие mode), не нашёл**. `list_changed` — не описан.
- **Responses API remote MCP tool** (`type: "mcp"`): поля `server_label`, `server_url`, `tunnel_id`, `authorization` (OAuth token, не хранится — передавать в каждом запросе), `require_approval` (`always`/`never`/по списку), `allowed_tools`, `defer_loading`; транспорт Streamable HTTP и HTTP/SSE; approval через `mcp_approval_request`/`mcp_approval_response` — это одобрение **вызова инструмента** разработчиком приложения, не elicitation сервера; elicitation в гайде не упомянута ([tools-connectors-mcp](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)). Вызовы к MCP-серверу делает инфраструктура OpenAI.

### 2.6 Страница клиентов modelcontextprotocol.io

- `https://modelcontextprotocol.io/clients` на 2026-10-27 **редиректит на вводную страницу** — матрицы возможностей клиентов там больше нет. Есть только [Extension Support Matrix](https://modelcontextprotocol.io/extensions/client-matrix.md) (MCP Apps, OAuth Client Credentials, Enterprise Auth, Skills, Tasks; «maintained by the community»); при конвертации в markdown ячейки пустые (вероятно, иконки), поэтому данные по ней **не извлёк**. Для таблицы в README источник — документация самих клиентов.

### 2.7 Python SDK `mcp` v2 — клиент и elicitation

- Версии на PyPI: **`mcp` 2.3.0 (2026-10-02)**, requires Python ≥ 3.10; ветка 2.x с 2.0.0 (2026-07-28); последняя 1.x — 1.30.0 (2026-09-07) ([pypi.org/project/mcp](https://pypi.org/project/mcp/), [JSON API](https://pypi.org/pypi/mcp/json)).
- Клиент — класс `Client` (`from mcp import Client`), позиционный аргумент определяет транспорт: URL → Streamable HTTP; `StdioServerParameters` → subprocess; объект транспорта; **экземпляр `MCPServer`/`Server` → in-process, «no subprocess and no port»**, рекомендовано для тестов (`Client(mcp)`). `list_tools()` → `name`, `title`, `description`, `input_schema`; `call_tool()` → `content`, `structured_content`, `is_error` (исключение в инструменте не бросается у клиента — проверять `is_error`). По умолчанию `Client` пробует новый протокол и откатывается на классический handshake; согласованная версия — `client.protocol_version` ([py.sdk…/client](https://py.sdk.modelcontextprotocol.io/client/)).
- Elicitation callback ([py.sdk…/client/callbacks](https://py.sdk.modelcontextprotocol.io/client/callbacks/)):
  - импорты `from mcp import Client`, `from mcp.client import ClientRequestContext`, `from mcp.types import ElicitRequestParams, ElicitResult`; сигнатура `async def handle(context: ClientRequestContext, params: ElicitRequestParams) -> ElicitResult`; передаётся `Client(..., elicitation_callback=handle)`. (В выдаче поиска встречался и вариант импорта `from mcp_types import …` на странице `/v2/` — расхождение, проверить по установленному пакету.)
  - Результат: `ElicitResult(action="accept", content={...})` / `"decline"` / `"cancel"`; `ErrorData` — отказ и падение всего вызова. Один callback на оба mode, ветвление по `params.mode` (`"form"` → `requested_schema`, `"url"` → `url`).
  - Регистрация callback сама объявляет capability `elicitation`; без callback SDK отказывает: `MCPError: Elicitation not supported`.
  - **Критично для архитектуры сервера:** в протоколе **2026-07-28** у сервера нет back-channel для server→client запросов: `ctx.elicit` «fails before your callback runs». Вместо этого инструмент возвращает **`InputRequiredResult` с `ElicitRequest`**, `Client` вызывает тот же `elicitation_callback` и **автоматически повторяет вызов** (multi-round-trip). Для классического server→client elicitation документация советует `mode="legacy"`. Sampling и roots в 2026-07-28 — deprecated RPCs.
- Следствие: подтверждение в UI (FR-47а) на 2026-07-28 реализуется как «вызов → `InputRequiredResult` → ответ человека → повтор вызова с ответом». Повтор приходит **новым запросом** — сервер обязан привязать ответ к конкретному pending-действию и его параметрам (FR-22) и не исполнить дважды (FR-26). Поддерживает ли Claude Code именно MRTR-elicitation через `InputRequiredResult` на 2026-07-28 (а не только объявляет capability) — **явно не подтверждено**; это вопрос к отчёту по транспорту/stateless.

### 2.8 SDK провайдеров и MCP connectors

- **`openai` 3.39.0 (2026-10-26)**, Python ≥ 3.10; **`anthropic` 2.3.0 (2026-10-23)**, Python ≥ 3.10 ([pypi openai](https://pypi.org/project/openai/), [pypi anthropic](https://pypi.org/project/anthropic/)). Обе — новые мажорные ветки относительно туториалов 2025 года; API клиентов сверять с текущими доками при реализации.
- **Anthropic MCP connector** (beta header `mcp-client-2025-11-20`, предыдущий `mcp-client-2025-04-04` deprecated): «only tool calls are currently supported»; «The server must be publicly exposed through HTTP… Local STDIO servers cannot be connected directly»; auth — `authorization_token` (OAuth bearer); не ZDR-eligible ([MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector)).
- **OpenAI remote MCP tool** — см. 2.5: вызовы с серверов OpenAI, нужен публичный URL или Secure MCP Tunnel, elicitation не документирована, approval — про вызов инструмента. Плата только за токены ([tools-connectors-mcp](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)).
- `gpt-6-luna`: Responses, Chat Completions, Batch; function calling, structured outputs, prompt caching, remote MCP (только Responses). Reasoning effort: none/low/medium/high/xhigh/max, **по умолчанию medium**. «Chat Completions supports function calling only with `reasoning_effort` set to `none`» — для function calling рекомендуют Responses API ([model page](https://developers.openai.com/api/docs/models/gpt-6-luna)). Совпадает с заметкой в `CLAUDE.md`.
- `claude-haiku-5-5`: tool use, adaptive thinking, **default effort `medium`**, контекст 1M, output 128K, retirement не раньше 2027-10-07 ([models overview](https://platform.claude.com/docs/en/about-claude/models/overview)). Tool use system prompt: **286 токенов** (auto/none), 406 (any/tool) ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)). Новый токенайзер (с Claude 4.7) даёт ~30% больше токенов на тот же текст (там же).

### 2.9 Цены (USD за 1M токенов, Standard; Batch/Flex — 50%)

| Модель | Input | Cached input (read) | Output | Источник |
|---|---|---|---|---|
| `claude-haiku-5-5` (≤ 100k prompt) | 0.10 | 0.01 (write 5m: 0.125) | 0.50 | [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| `claude-sonnet-5-5` | 2.00 | 0.10 | 10.00 | там же |
| `claude-opus-5-5` | 4.00 | 0.20 | 20.00 | там же |
| `gpt-6-luna` | 0.10 | 0.01 (write 0.125) | 0.50 | [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [model](https://developers.openai.com/api/docs/models/gpt-6-luna) |
| `gpt-5-nano` | 0.05 | 0.005 | 0.40 | OpenAI pricing |
| `gpt-4.1-nano` | 0.10 | 0.025 | 0.40 | OpenAI pricing |
| `gpt-4.1-mini` | 0.40 | 0.10 | 1.60 | OpenAI pricing |
| `gpt-6.1-sol` | 2.00 | 0.10 | 10.00 | OpenAI pricing |

Caching: Anthropic — минимум **512 токенов** для Haiku 5.5, tools кэшируются (`cache_control` на последнем tool; смена любого описания инструмента инвалидирует весь кэш) ([prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)). OpenAI — для GPT-5.6+ минимум 1024 видимых токена, tool definitions входят в кэшируемый префикс ([OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)).

## 3. Таблица для README (черновик, NFR-6)

Легенда: Да — подтверждено официальной документацией; Нет — подтверждено отсутствие; ? — не подтверждено; Да* / Нет* — см. примечание.

| Возможность | Claude Code (CLI) | Claude Desktop | Cursor | VS Code Copilot | ChatGPT |
|---|---|---|---|---|---|
| Локальный stdio | Да | Да | Да | Да | Нет (только через Secure MCP Tunnel) |
| Remote Streamable HTTP | Да | Да (custom connector, с серверов Anthropic) | Да | Да | Да |
| OAuth | Да (DCR/CIMD/pre-registered) | Да (DCR/CIMD/…) | Да | Да | Да (CIMD/DCR) |
| Статический bearer в конфиге | Да (`--header`, `headersHelper`) | Beta, ограниченно (`static_headers`) | Да (`headers`) | ? (в config reference) | ? |
| Elicitation form | Да (с v2.1.76) | Нет* | Да (mode не уточнён) | Да (mode не уточнён) | ? |
| Elicitation URL | Да | Нет* | ? | ? | ? |
| Tool annotations влияют на UX | Да (`readOnlyHint` параллельность/повтор, `idempotentHint`) | Да (read-only без подтверждения, destructive — всегда prompt) | ? | Да* (`readOnlyHint`) | Да (подтверждение write-действий) |
| Structured output (`outputSchema`/`structuredContent`) | ? | ? | ? | ? | Да (рекомендован) |
| `list_changed` | Да (tools; в интерактиве также prompts/resources) | ? | ? | ? | ? |

Примечания: Desktop/elicitation — официально не задокументировано; по open bug [#96043](https://github.com/anthropics/claude-code/issues/96043) локальные серверы не получают capability, а запрос может быть молча отклонён. VS Code/annotations — со слов developer guide, цитата не перепроверена.

## 4. Варианты с trade-offs

### 4.1 Канал подтверждения для AC-1

- **A. Claude Code CLI + stdio + form-mode elicitation.** Плюсы: официально задокументировано, оба mode, capability объявляется (с 2.1.287 и на старых ревизиях протокола), stdio idle timeout 30 мин > TTL 2 мин. Минусы: пользователь может включить `Elicitation` hook с автоответом — сервер этого не видит (ровно сценарий FR-42; на testnet по политике доверяем, на mainnet — нет). Демо в терминале менее «зрелищно», чем Desktop.
- **B. Claude Desktop + внешний канал.** Плюсы: популярный клиент, честно показывает fail-over FR-25/FR-20. Минусы: elicitation нет/сломана; риск «declare нет, но на запрос приходит `decline`» (по issue — либо capability не объявлена, и тогда сервер сразу идёт во внешний канал, либо приходит фиктивный `decline`, который сервер примет за отказ человека — fail-safe, но плохой UX и неверная запись в журнале «отклонено пользователем»).
- **C. URL-mode elicitation как мост к внешнему каналу.** Сервер отдаёт URL своей страницы подтверждения; клиент лишь предлагает открыть ссылку, решение принимается на странице сервера. Плюсы: подтверждение уходит из-под контроля и модели, и программы клиента (ближе к «внешнему каналу», полезно для mainnet при FR-42 «не доверять UI»); соответствует назначению URL mode в спецификации (out-of-band, sensitive). Минусы: нужна локальная web-страница/сервер подтверждений и защита ссылки (одноразовый токен, привязка к действию); поддержка — только Claude Code (у остальных не подтверждено). Пересекается с вопросом «внешний канал» (SPEC 9) — решать вместе.

### 4.2 Архитектура агент-клиента (FR-39)

- **A. Свой цикл: `mcp.Client` + adapter провайдера.** `Client(StdioServerParameters | URL | MCPServer)`, `elicitation_callback` рисует консольную форму из `requested_schema` (или печатает URL), `list_tools()` → конвертация `input_schema` в формат tools провайдера (OpenAI Responses function tools / Anthropic Messages `tools`), цикл «модель → tool call → `call_tool` → результат → модель». Плюсы: работает со stdio и локальным сервером, полный контроль над elicitation (AC-5), один и тот же цикл используется в evals, в тестах — in-process `Client(mcp)` без сети (NFR-2), провайдер — порт `LLMProvider` с двумя адаптерами (ENGINEERING.md). Минусы: больше кода (~200–400 строк), нужно самим нормализовать schemas (Anthropic API не принимает `anyOf/oneOf/allOf` на корне схемы — см. [Claude Code docs](https://code.claude.com/docs/en/mcp), раздел про root-level combinators), считать токены.
- **B. MCP connector провайдера (Anthropic `mcp_servers` / OpenAI `type: "mcp"`).** Плюсы: минимум кода. Минусы, блокирующие: сервер должен быть публично доступен по HTTP (Anthropic: stdio нельзя; OpenAI: публичный URL или tunnel) — противоречит локальному режиму и CI без сети; Anthropic connector поддерживает только tool calls, OpenAI не документирует elicitation — **elicitation некому показать**, значит AC-5 не выполнить; evals зависят от сетевого вызова провайдера к нашему серверу — недетерминированно и не offline; Anthropic connector не ZDR-eligible.
- **C. Готовые фреймворки (LangChain `MCPAdapter`, Agent SDK).** Не исследовал глубоко; по `CLAUDE.md` `langchain.mcp.MCPAdapter` — beta. Добавляют зависимость и прячут именно то, что надо показать на интервью (elicitation, цикл tool use). Не рекомендую для v1.

### 4.3 Модель и стоимость evals (FR-40)

Допущения для оценки: описания ~20 инструментов ≈ 4k токенов (у Claude +30% токенайзер → ~5k), system ≈ 500, запрос ≈ 100, overhead tool use 286 → **~5.5–6k input на вызов**; вывод — tool call ~150 токенов + reasoning; в среднем **1.3 вызова модели на кейс** (часть кейсов многошаговые: «покажи позиции → поставь стоп»); кейсы оцениваются по первому изменяющему решению на моках инструментов.

| Сценарий | Вызовов | Input | Output | Haiku 5.5 / gpt-6-luna, без кэша | с кэшем tools+system |
|---|---|---|---|---|---|
| 40 кейсов × 3 прогона, effort low (~350 out) | 156 | ~0.9M | ~55k | ≈ $0.09 + $0.03 = **$0.12** | ≈ **$0.04** |
| 40 × 5, effort low | 260 | ~1.5M | ~90k | ≈ $0.15 + $0.05 = **$0.20** | ≈ **$0.07** |
| 40 × 5, effort medium по умолчанию (~2k out) | 260 | ~1.5M | ~520k | ≈ $0.15 + $0.26 = **$0.41** | ≈ $0.32 |
| До/после изменения описаний (2 варианта) × 40 × 5, low | 520 | ~3M | ~180k | ≈ **$0.39** | ≈ **$0.13** |
| То же на Sonnet 5.5 / gpt-6.1-sol | 520 | ~3M | ~180k | ≈ $7.8 | > $1 |

Вывод: Haiku 5.5 и gpt-6-luna по цене одинаковы ($0.10/$0.50) и укладываются в $0.50 с запасом **только при низком effort/reasoning и с prompt caching**; главный драйвер стоимости — не input, а reasoning-токены (по умолчанию effort = medium у обеих). Sonnet/Opus/gpt-6.1-sol — только для разового «эталонного» сравнения, не для регулярного прогона. Batch API (−50%) применим к одношаговым кейсам, но неудобен для многошаговых циклов. `gpt-5-nano` ($0.05/$0.40) — ещё дешевле, но качество tool calling на русском не проверял.

## 5. Рекомендация и что остаётся неизвестным

Рекомендация:
- **AC-1: Claude Code CLI, локальный stdio, form-mode elicitation** как основной канал на testnet. Claude Desktop в README — «elicitation не поддерживается (на 2026-10-27), используйте внешний канал»; проверять capability `elicitation` в handshake/`_meta` и при её отсутствии сразу уходить в pending + внешний канал (FR-25, FR-47б). Сценарий Desktop + внешний канал — хороший второй демо-кейс fail-over.
- **В README прямо написать**, что Claude Code `Elicitation` hook может автоматически отвечать на формы, а permission prompts клиентов (`destructiveHint`, `requiresUserInteraction`) — это UX клиента, а не гарантия сервера; поэтому FR-42 по умолчанию не доверяет UI на mainnet. Это сильный аргумент на интервью.
- Аннотации ставить всем инструментам: `title`, `readOnlyHint: true` для чтения, `destructiveHint: true` для торговых, `idempotentHint` — только где это действительно так (иначе Claude Code может повторить вызов после обрыва); для торговых инструментов — рассмотреть `_meta["anthropic/requiresUserInteraction"]` как дополнительный, но не основной барьер. Отдавать и `structuredContent` с `outputSchema`, и текст в `content`.
- **Агент-клиент: свой цикл** на `mcp` 2.3.x `Client` + порт `LLMProvider` с адаптерами `openai` 3.39.x (Responses API, function tools) и `anthropic` 2.3.x (Messages tool use); elicitation — консольный callback (form → поля по схеме, url → печать ссылки). Не использовать MCP connectors провайдеров: требуют публичного HTTP и не дают обработать elicitation. Тот же цикл и in-process `Client(MCPServer)` — основа evals и e2e-тестов без сети.
- **Evals: по умолчанию `claude-haiku-5-5` с effort `low` и `gpt-6-luna` с `reasoning.effort` `none`/`low` (Responses API)**, prompt caching описаний инструментов, счётчик фактической стоимости по `usage` в отчёте и жёсткий стоп при превышении бюджета. 3–5 прогонов на кейс. Сравнение «до/после описаний» — две ветки на одной модели.

Риски:
- Stateless 2026-07-28: elicitation идёт через `InputRequiredResult` и повторный вызов; привязка ответа к pending-действию и защита от двойного исполнения — на сервере (FR-22, FR-26, FR-47). Если Claude Code не реализует MRTR-elicitation на 2026-07-28, может понадобиться `legacy`-режим для stdio.
- Desktop может присылать фиктивный `decline` — сервер запишет «отклонено пользователем» без участия человека (безопасно, но журнал неточен).
- Быстро меняющиеся клиенты: таблица в README устаревает за недели — указать дату проверки и версии клиентов.
- Reasoning-токены непредсказуемы — бюджет $0.50 держится только при явном низком effort.

Не подтверждено / открыто:
- Поддержка Claude Code именно MRTR-формы elicitation (`InputRequiredResult`) на протоколе 2026-07-28 — проверить руками в первом тикете (stdio, обе ревизии протокола).
- Structured output и `list_changed` в Claude Desktop, Cursor, VS Code; form/URL mode в Cursor и VS Code; elicitation в ChatGPT; статические bearer-заголовки в VS Code/ChatGPT.
- Точная дата релиза Claude Code 2.1.76 (13 или 14 марта 2026) — по вторичным источникам.
- Расхождение импорта типов в документации SDK (`mcp.types` vs `mcp_types`) — сверить с установленным пакетом.
- Реальный размер описаний 20 инструментов в токенах и доля reasoning у Haiku 5.5 / gpt-6-luna — измерить на первом прогоне (Anthropic token counting / `usage`).
