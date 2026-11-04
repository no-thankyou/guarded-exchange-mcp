# 029: Консольный агент-клиент guarded_agent
Статус: IN_PROGRESS (AC-5 E2E ждёт 020)
Зависит от: 020
Требования: FR-39, AC-5, ADR-016

## Цель
Консольный агент подключается к серверу как MCP-клиент (stdio/HTTP), работает с OpenAI или Anthropic (переключаемо) и показывает подтверждения в консоли.

## Scope
- Пакет `agent/guarded_agent`, зависит только от `mcp` и провайдеров; порт `LLMProvider`, адаптеры OpenAI (Responses, `gpt-6-luna`) и Anthropic (Messages, `claude-haiku-5-5`).
- `elicitation_callback` — консольная форма; конвертация `input_schema` в tools провайдера; лимит шагов цикла.
- Тесты на записанных ответах провайдеров (без сети).

## Non-scope
- Evals (030).

## Acceptance criteria
- [x] Unit: цикл агента на фейковом провайдере вызывает инструменты и обрабатывает `isError` — `tests/unit/agent/test_loop.py::test_the_loop_calls_tools_and_feeds_an_is_error_result_back_to_the_model`, `test_the_loop_stops_at_the_step_limit`; адаптеры провайдеров на ответах в формате SDK — `tests/unit/agent/test_providers.py`; консольная форма — `tests/unit/agent/test_console.py`.
- [~] E2E (in-process сервер + фейковый провайдер): демо-сценарий с подтверждением в консоли (AC-5) — работает сейчас: `tests/e2e/test_agent_demo.py::test_the_agent_reads_positions_and_places_an_order_without_confirmation`, `test_without_a_ui_channel_the_agent_gets_confirmation_channel_unavailable`. **Skip до 020:** `test_ac5_the_human_confirms_in_the_console_and_the_order_is_sent`, `test_ac5_a_declined_confirmation_sends_nothing` — снять `skip` после 020.
- [x] Ручной прогон с реальным провайдером — README «Agent client and tool-selection evals»: `ANTHROPIC_API_KEY=... uv run guarded-agent` (stdio, сервер после `init`) или `--provider openai`; HTTP — `--url ... ` + `GX_TOKEN`.

## Заметки
Итоги:
- `agent/guarded_agent`: порт `LLMProvider` + нейтральные типы (`provider.py`), адаптеры `OpenAIProvider` (Responses, `store=False`, effort `none`, `prompt_cache_key`) и `AnthropicProvider` (Messages, effort `low`, top-level `cache_control`), цикл `Agent` с лимитом шагов (`loop.py`), `ConsoleElicitation` (`console.py`), CLI `guarded-agent` (stdio | HTTP + bearer).
- История переписки хранится в нейтральном виде и пересобирается под провайдера на каждый вызов: проще и тестируемо; цена — для OpenAI не переносятся reasoning items (поэтому effort `none`).
- `isError` и протокольные `MCPError` не роняют цикл — уходят модели как ошибочный tool result (у OpenAI — префикс `ERROR:`, флага нет).
- Импорт-контракт: `guarded_agent` не импортирует `guarded_exchange`. SDK `openai`/`anthropic` — dependency group `agent` (в `default-groups`), сервер их не тянет.
- Версии: `openai` 3.45.0, `anthropic` 2.4.0 (новее, чем в PLAN — сверено по установленным пакетам).
- Предложение: после 020 снять два `skip`; `input()` в консольной форме блокирует event loop на время ответа человека — для консоли приемлемо.
