# 001: Скелет проекта и get_ticker насквозь
Статус: DONE
Зависит от: —
Требования: NFR-2, NFR-3, NFR-5, NFR-7, FR-1 (часть), FR-3 (часть), FR-28 (часть)

## Цель
Минимальный сквозной путь: MCP-клиент по stdio вызывает `get_ticker(symbol, category)` → use case `GetTicker` → порт `ExchangeClient` → Bybit-адаптер (публичный `GET /v5/market/tickers`, без ключей и БД) → компактный ответ. Плюс вся инженерная обвязка, на которой строятся остальные тикеты.

## Scope
- Структура `src/guarded_exchange/` по PLAN раздел 3 (только нужные модули, пустые пакеты слоёв допустимы).
- uv, Python 3.12, `uv.lock`; версии зависимостей проверить на PyPI и зафиксировать в PLAN раздел 2 (строки «актуальная на 001»).
- ruff (lint+format), `mypy --strict`, import-linter контракт слоёв (PLAN раздел 3), pytest + pytest-asyncio + coverage, pre-commit, GitHub Actions (lint, types, import-linter, tests) без сети к внешним API.
- `config.py` (pydantic-settings), `bootstrap.py` (composition root), structlog JSON в stderr с request id и процессором маскирования секретов.
- Bybit-адаптер: `httpx.AsyncClient` с явными connect/read таймаутами, retry с backoff только для GET, маппинг DTO → доменные `Ticker`, `Decimal`; маппинг ошибок сети/кодов в доменные исключения (минимум: `exchange_unavailable`, `invalid_params`).
- `interface/mcp/server.py` на `MCPServer`, запуск `run("stdio")`; `errors.py` — единый маппинг доменных исключений в tool error `{code, message, next_step}` (ADR-013, базовая версия).
- pytest-маркер `req("FR-…")` и заготовка генерации `docs/traceability.md`.
- Команда запуска сервера (`guarded-exchange serve` или `uv run ...`) и короткий раздел в README (EN) «how to run the skeleton».

## Non-scope
- БД, ключи, пользователи, политика, остальные инструменты.
- Спайки (002–004).

## Acceptance criteria
- [x] Unit: use case `GetTicker` на `FakeExchange` возвращает доменный тикер; ошибка порта превращается в доменное исключение. — `tests/unit/test_get_ticker.py`
- [x] Integration: Bybit-адаптер на `httpx.MockTransport` с записанной фикстурой tickers — маппинг полей в `Decimal`, таймаут → `exchange_unavailable`, ошибочный `retCode` → доменная ошибка. — `tests/integration/test_bybit_client.py` (фикстуры `tests/fixtures/bybit/`, записаны с api-demo.bybit.com 2026-10-27)
- [x] E2E: in-process MCP `Client` вызывает `get_ticker` → structured content соответствует `outputSchema`; ошибка → `isError: true` с `code`. — `tests/e2e/test_mcp_inprocess.py` (валидация `jsonschema` по `outputSchema` из `tools/list`)
- [x] E2E stdio: подпроцесс сервера отвечает на `tools/list` и `get_ticker` (биржа подменена через конфиг на MockTransport/фейк), stdout содержит только протокол, логи — в stderr. — `tests/e2e/test_mcp_stdio.py`
- [x] CI зелёный: ruff, ruff format --check, mypy --strict, lint-imports, pytest; ни одного обращения к внешним API. — `.github/workflows/ci.yml`; autouse-сторож сети в `tests/conftest.py` + `tests/unit/test_network_guard.py`
- [x] import-linter ловит нарушение (проверено временным нарушением локально, описано в отчёте).

## Заметки
Решения и отклонения от PLAN/тикета (2026-10-27, исполнитель):
- **`httpx2` вместо `httpx`.** `mcp` 2.3.0 зависит от `httpx2` 2.16 (продолжение `httpx` под Pydantic, тот же API), `httpx` застыл на 0.28.1. Чтобы не тащить два HTTP-стека, адаптер и тесты на `httpx2` (`AsyncClient`, `MockTransport`, `Timeout`). PLAN раздел 2 обновлён. `respx` не используется (рассчитан на `httpx`).
- **Ошибки инструмента со structured content.** `ToolError` SDK отдаёт только текст, поэтому инструмент объявлен как `-> Annotated[CallToolResult, TickerOut]`: SDK публикует `outputSchema` из `TickerOut` и валидирует успешный результат, а ошибку мы возвращаем сами как `CallToolResult(is_error=True, structured_content={code, message, next_step, ...})` (`interface/mcp/errors.py`, `results.py`). Текстовый `content` дублирует тот же JSON — для клиентов, которые показывают модели только текст.
- **Валидация аргументов — в use case, а не pydantic SDK.** Ошибка pydantic-валидации аргументов в SDK приходит модели свободным текстом без `code`. Поэтому `symbol`/`category` объявлены как `str` с `WithJsonSchema` (модель видит `enum`/`pattern`), а проверку делают доменные `normalize_symbol`/`parse_category` → `invalid_params` с `details.field`. Trade-off: схема и рантайм-проверка описаны в двух местах. Отсутствие обязательного аргумента по-прежнему даёт текстовую ошибку SDK.
- **Decimal в выходе — строка** (`DecimalStr`: `PlainSerializer` + `WithJsonSchema {"type":"string","pattern":...}`), без хвостовых нулей и экспоненты; отсутствующие поля опускаются (`exclude_none`), а не `null` (FR-3). Проценты (`change_24h_pct`, `funding_rate_pct`) — уже ×100.
- **`domain/market.py`** — новый модуль для `Ticker` (в PLAN раздел 3 добавлен). `Category` и разбор символа — в `domain/instruments.py`.
- **Коды Bybit:** 10001 → `invalid_params` (текст биржи только в `untrusted_text`, FR-41); 10000/10016, сеть, таймаут, HTTP 5xx → retry (только GET) → `exchange_unavailable`; 10006 и HTTP 403 → `exchange_unavailable` без retry (полноценный `rate_limited` с `retry_after` — тикет 022); пустой список → `not_found`; прочие коды → `exchange_rejected`.
- **CLI и слои.** PLAN: interface не импортирует bootstrap. Поэтому `interface/cli/main.py` строит typer-приложение из переданного колбэка `serve`, а точка входа — `guarded_exchange.bootstrap:cli` (`guarded-exchange serve`, `python -m guarded_exchange serve`).
- **import-linter:** 4 контракта — слои `bootstrap → interface | infrastructure → application → domain` (interface и infrastructure независимы), конфиг читает только bootstrap, domain без `mcp/httpx2/pydantic/structlog/typer`, application без `mcp/httpx2/pydantic_settings/typer`. Проверено временными нарушениями: `application → infrastructure` + `application → httpx2` (сломаны 2 контракта) и `interface.mcp.server → infrastructure` (сломан контракт слоёв, exit 1).
- **NFR-2:** autouse-фикстура в `tests/conftest.py` запрещает `connect`/`getaddrinfo` на не-loopback адреса (тест на сам сторож есть). stdio-e2e указывает реальный сервер (`GX_BYBIT_BASE_URL`) на loopback HTTP-заглушку с записанными ответами; `tee` копирует сырой stdout для проверки «только JSON-RPC».
- **Traceability:** `uv run pytest --collect-only -q --traceability=docs/traceability.md` (именно с `=`: иначе pytest примет путь за тестовый и не загрузит conftest). CI проверяет, что таблица актуальна.
- structlog: кэш логгеров выключен (иначе логгер, использованный до `configure_logging`, навсегда пишет в stdout — опасно для stdio). `configure_logging` вызывается до `MCPServer()`, т.к. тот вызывает `logging.basicConfig`.
- Live-проверка руками (не в CI): по stdio против api-demo.bybit.com `get_ticker` BTCUSDT/linear, ethusdt/spot — успех; NOPEUSDT → `invalid_params` с `untrusted_text`.

Ревью, раунд 1 (APPROVE с minor, закрыто в этом тикете):
- Адаптер ловит все `httpx2.HTTPError` (+`InvalidURL`, `StreamError`): не-транспортные ошибки (decoding, redirects) → `exchange_unavailable` без retry; тест с `DecodingError`/`TooManyRedirects`.
- `run_tool`: любое неожиданное исключение → `log.exception` + `code=internal_error` с `details.request_id`, без текста исключения; e2e-тест с `FakeExchange`, бросающим `RuntimeError`.
- CI: порог покрытия domain+application ≥ 90% (`coverage report --include=... --fail-under=90`), сейчас 100%.
- Имя поля `retry_after_s` (секунды) зафиксировано в PLAN ADR-013/ADR-011 и тикете 022; оставшиеся `httpx`/`respx` в PLAN заменены на `httpx2`/`httpx2.MockTransport`.
- `.env` из текущего каталога больше не читается: только `--env-file` или `GX_ENV_FILE` (опция главнее), отсутствующий явный файл → ошибка и exit 2 (fail closed); тесты `tests/unit/test_config.py`, README обновлён.
- stdio-e2e: подпроцессу переданы `HTTPS_PROXY/HTTP_PROXY/ALL_PROXY=http://127.0.0.1:9`, `NO_PROXY=127.0.0.1,localhost` (проверено руками: с мёртвым прокси реальный сервер получает `exchange_unavailable` вместо похода на Bybit).
- Allowlist для `bybit_base_url` не делался — вынесено на решение пользователя.

Предложения (вне scope):
- Единый маппинг pydantic-ошибок аргументов SDK в `invalid_params` (через `Extension.intercept_tool_call` или middleware) — проверить в спайке 002, чтобы не дублировать схему и проверку в каждом инструменте.
- Лицензия в `pyproject.toml`/README не указана — решить при релизе (033).
