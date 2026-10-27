# 001: Скелет проекта и get_ticker насквозь
Статус: TODO
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
- [ ] Unit: use case `GetTicker` на `FakeExchange` возвращает доменный тикер; ошибка порта превращается в доменное исключение.
- [ ] Integration: Bybit-адаптер на `httpx.MockTransport` с записанной фикстурой tickers — маппинг полей в `Decimal`, таймаут → `exchange_unavailable`, ошибочный `retCode` → доменная ошибка.
- [ ] E2E: in-process MCP `Client` вызывает `get_ticker` → structured content соответствует `outputSchema`; ошибка → `isError: true` с `code`.
- [ ] E2E stdio: подпроцесс сервера отвечает на `tools/list` и `get_ticker` (биржа подменена через конфиг на MockTransport/фейк), stdout содержит только протокол, логи — в stderr.
- [ ] CI зелёный: ruff, ruff format --check, mypy --strict, lint-imports, pytest; ни одного обращения к внешним API.
- [ ] import-linter ловит нарушение (проверено временным нарушением локально, описано в отчёте).

## Заметки
—
