# 003: 👤 Спайк: MRTR-elicitation в Claude Code и Claude Desktop
Статус: DONE
Зависит от: 002
Требования: AC-1, FR-20, FR-42, NFR-6

## Цель
Руками проверить, как реальные клиенты обрабатывают form-elicitation по протоколу 2026-07-28 (и legacy), чтобы выбрать канал для AC-1 и заполнить таблицу клиентов в README.

## Scope
- Тестовый MCP-сервер из `spikes/003/` с одним инструментом, который запрашивает подтверждение формой (на основе 002).
- Исполнитель готовит сервер, инструкцию и чек-лист; пользователь подключает к Claude Code и Claude Desktop и сообщает результат (скриншоты/логи).
- Фиксация: объявляет ли клиент `elicitation.form`, рисует ли форму, что приходит при accept/decline/cancel, работает ли MRTR или только legacy, хуки Claude Code `Elicitation`.
- Результат — таблица «клиент × возможность × дата × версия клиента» в PLAN (ADR-005) и черновик для README.

## Non-scope
- Код продукта.

## Acceptance criteria
- [x] Таблица заполнена для Claude Code и Claude Desktop с версиями и датой проверки. *Сужено пользователем 2026-10-29: Claude Desktop — «не проверено, нет клиента под Linux».*
- [x] Принято и записано в PLAN решение для AC-1: UI-канал (MRTR или legacy) или внешний канал для каждого клиента.

## Заметки
Требует пользователя: исполнитель не может сам открыть GUI клиента.

Подготовка (2026-10-28, исполнитель): готовы сервер, инструкция и smoke-тесты; acceptance criteria не выполнены — они требуют ручного прогона в GUI клиентов. Таблица в PLAN и решение для AC-1 не записаны: версии клиентов и поведение не выдумываем.
- `spikes/003/elicitation_probe.py` — stdio-сервер (`mcp` 2.3.0), инструмент `confirm_test_order`; шаблон ADR-005 / D-1 из `build_dual_era_server()` спайка 002: MRTR на 2026-07-28 (второй раунд только по `ctx.request_state`, `inputResponses` без state → `invalid_params`, повтор только со state переспрашивает тот же pending), push `elicitation/create` на ранних протоколах, проверка capability до создания pending, подтверждение = `accept` и `content.confirm is True`. На legacy используется `ctx.session.elicit_form` (сырой `ElicitResult`), а не `ctx.elicit`: последний валидирует content по схеме и на «accept без поля» бросает `ValueError` — для спайка нужен сырой ответ. Логи — JSON-строки в stderr и в файл из `SPIKE003_LOG_FILE` (пути к логам у клиентов разные, файл — нет). Диагностический `SPIKE003_FORCE_ASK=1` — спросить без объявленной capability, чтобы поймать фиктивный `decline` Desktop (issue из research).
- `spikes/003/README.md` — инструкция на русском: Claude Code (`claude mcp add ... -- uv run --directory`, `/mcp`, `/hooks`, `--debug-file`, хук `Elicitation`), Claude Desktop (`claude_desktop_config.json`, пути macOS/Windows; Linux — не подтверждено, пользователь записывает фактический путь; логи `~/Library/Logs/Claude/`), чек-лист сценариев 0–8, пустая таблица «клиент × возможность × дата × версия», что прислать.
- `spikes/003/test_elicitation_probe.py` — 19 тестов: accept/decline/cancel/accept с `confirm=false` на обоих протоколах, клиент без capability (отказ без pending и без `-32600`), второй вызов → новый pending, ответы без state, повтор только со state, правило `supports_form`, force-ask, настоящий stdio-процесс с логом в файл. Запуск: `uv run pytest spikes/003 -v`.
- Находка для PLAN (не правил, предложение): ADR-005 и «Спайк 002» проверяют канал условием `ctx.client_capabilities.elicitation.form is not None`. Документация Claude Code (code.claude.com/docs/en/mcp, «Elicitation modes Claude Code declares», прочитано 2026-10-27) говорит, что на части соединений ранних ревизий он объявляет голый `elicitation: {}`, а по спецификации 2025-11-25 это означает «только form». SDK в своей проверке (`mcp/server/mcpserver/resolve.py`) считает `{}` формой. Предлагаю в ADR-005 п.2 заменить условие на правило SDK: `elicitation is not None and (form is not None or url is None)` (как `supports_form` в спайке); окончательно — после логов пользователя.

Что осталось (после ответа пользователя): перенести таблицу в PLAN (ADR-005), записать решение для AC-1 по каждому клиенту (MRTR / legacy / внешний канал), черновик для README, статус → IN_REVIEW.

Итог (2026-10-29): пользователь прогнал Claude Code 2.1.312 на Linux Mint 22.3 (2026-10-28) — MRTR работает, legacy не используется, accept/accept без галочки/decline/cancel различимы; таблица и итог — `spikes/003/README.md`, решение для AC-1 — PLAN ADR-005 п.13 (UI-канал только MRTR на 2026-07-28, legacy-ветки в v1 нет, правило SDK для `elicitation.form`, подсказка в `message` формы). Не проверено и принято пользователем как допустимое: прерывание хода с открытой формой, хук `Elicitation`, Claude Desktop. Черновик для README — там же в `spikes/003/README.md`.
