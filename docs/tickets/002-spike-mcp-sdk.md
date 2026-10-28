# 002: Спайк: возможности MCP Python SDK 2.x
Статус: DONE
Зависит от: 001
Требования: FR-11, FR-36, FR-47, FR-20 (исследование)

## Цель
Ответить по фактам (код-эксперименты на закреплённой версии `mcp`) на вопросы, от которых зависят M3 и ADR-012, и записать результат в PLAN.

## Scope
- Можно ли вернуть `InputRequiredResult` (MRTR, протокол 2026-07-28) из high-level `@mcp.tool`; как получить `inputResponses`/`requestState` во втором раунде; если нельзя — минимальный пример на low-level `Server` рядом с `MCPServer`.
- `RequestStateSecurity`: ключи из конфига, поведение после рестарта, `audience`.
- Фильтрация `tools/list` по principal без provisional API (ADR-012).
- Где доступны `clientInfo`/самоописание клиента и capabilities из `_meta` в контексте вызова (FR-36, выбор канала ADR-005).
- Как in-process `Client` SDK обрабатывает MRTR и `elicitation_callback` (для e2e-тестов M3).
- Результат: заметка «Спайк 002» в PLAN (раздел 4, ADR-005/012) с версиями и минимальными примерами; эксперименты — в `spikes/002/` (не в `src/`).

## Non-scope
- Ручная проверка реальных клиентов (003).
- Код продукта.

## Acceptance criteria
- [x] Каждый вопрос из Scope имеет ответ «да/нет + как», подтверждённый запускаемым примером или ссылкой на исходники SDK конкретной версии.
- [x] ADR-005 и ADR-012 обновлены по результату; если вывод меняет архитектуру interface/mcp — статус BLOCKED с предложением правки PLAN.

## Заметки
Итог (2026-10-27, исполнитель): все ответы — в PLAN, раздел 4 «Спайк 002» (таблица вопрос → да/нет → как, ссылки на исходники `mcp` 2.3.0), правки в ADR-003, ADR-005, ADR-012, ADR-013 и в рисках (раздел 9). Архитектура interface/mcp не меняется, BLOCKED не нужен: high-level `MCPServer` остаётся, low-level `Server` не нужен.
- Эксперименты: `spikes/002/test_mrtr_high_level.py` (MRTR из `@mcp.tool`, клиент с/без `elicitation_callback`, ручной цикл, подмена аргументов/токена, legacy, одна функция для обеих эр), `test_request_state_security.py` (рестарт, ротация ключей, audience, principal, TTL), `test_tools_list_and_client_info.py` (override `list_tools`, middleware-альтернатива, `clientInfo`/capabilities на modern и legacy, реальный Streamable HTTP + `TokenVerifier` через `httpx2.ASGITransport`), `test_invalid_params.py` (вопрос ревью 001). Запуск: `uv run pytest spikes/002 -v` (37 тестов, без сети; `spikes/conftest.py` переиспользует сетевой guard из `tests/conftest.py`, для импорта добавлен `spikes/__init__.py`). В CI pytest их не гоняет (`testpaths = ["tests"]`), но ruff и mypy --strict их проверяют (`spikes` добавлен в `[tool.mypy].files`, для `spikes/**` в ruff отключены S101/PLR2004/PT018).
- Решение по вопросу ревью 001 (единый `invalid_params`): override публичного `MCPServer.call_tool` в подклассе, а не `Extension.intercept_tool_call` (видит только текст, плюс рекламирует себя клиентам как расширение) и не middleware (provisional). Trade-off: опора на неявный контракт «`_handle_call_tool`/`_handle_list_tools` вызывают `self.call_tool`/`self.list_tools`» — страхуется закреплённой версией и e2e-тестами.
- Неочевидное: `requestState` одноразовым не является (повтор принимается); SDK привязывает токен к **сырым** `arguments`; лишние аргументы pydantic SDK молча отбрасывает; без колбэка Python `Client` на форму падает `-32600`, а не `decline`.

Ревью, раунд 1 (CHANGES_REQUESTED, исправлено):
- M1: признак второго раунда — только `ctx.request_state is not None`. Спайк подтвердил факт SDK: первый `tools/call` с `inputResponses` без `requestState` доходит до тела инструмента. Шаблон отвечает на него `invalid_params`, pending не создаётся. Ретрай только со state переспрашивает про тот же pending. Подтверждение — только `ElicitResult` с `action == "accept"` и `content.confirm is True`; `confirm=false`, пустой content, чужой ключ или `cancel` — отказ, а не `KeyError`. Тесты в `test_mrtr_high_level.py`, правки в PLAN: ADR-005 п.7/D-1, таблица и пример в «Спайк 002».
- Minor: reader вызывает скрытый `place_order` напрямую через `tools/call` и доходит до тела; без колбэка проверяется `-32600 "Elicitation not supported"`; для `value_error` в `invalid_params` идут только loc+type (тест с валидатором, который эхом возвращает секрет); в ADR-003 записано поведение при смене master key; PLAN ADR-013 больше не утверждает, что проверка в use case не нужна (SDK-валидация — дополнительный слой на границе MCP).
- Выводы спайка закреплены за тикетами: 006 — фильтрация `tools/list`; 009 — `GuardedMCPServer`, `get_ticker` на настоящих типах и e2e «пропущенный аргумент → `invalid_params`»; 011 — денежные аргументы строкой и `client_info` для FR-36; 020 — шаблон из PLAN вместо «или low-level `Server`».

Предложения (вне scope):
- Перед обновлением `mcp` прогонять `uv run pytest spikes/002` как канарейку или перенести ключевые проверки в `tests/` при реализации 020.
