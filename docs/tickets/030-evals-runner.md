# 030: Evals выбора инструментов: кейсы и раннер
Статус: IN_REVIEW
Зависит от: 029
Требования: FR-40

## Цель
≥ 40 кейсов (ru/en, ≥ 25% опасных) с детерминированным эталоном и раннер с метриками, несколькими прогонами и жёстким бюджетом $0.50.

## Scope
- `evals/cases.jsonl`: `call{tool, args⊂}` / `clarify` / `refuse`; кейсы read-only профиля и prompt injection в данных.
- Сервер in-process на fake-бирже; оценивается первое изменяющее решение; 3–5 прогонов; prompt caching; счётчик стоимости по `usage` и стоп по бюджету.
- Отчёт markdown: точность инструмента, аргументов, корректных отказов/уточнений.

## Non-scope
- Сравнение описаний (031).

## Acceptance criteria
- [x] Unit: оценщик на записанных ответах (совпадение, частичное, отказ, уточнение) — `tests/unit/evals/test_evals.py::test_grade_by_the_first_changing_decision`, `test_the_runner_reads_for_real_and_stops_at_the_first_change`, `test_a_read_only_profile_hides_the_changing_trading_tool`.
- [x] Unit: бюджет превышен → прогон останавливается с частичным отчётом — `test_over_budget_the_run_stops_with_a_partial_report`, `test_a_model_without_pricing_is_refused_before_any_call`.
- [x] Валидатор кейсов: ≥ 40, ≥ 25% опасных, оба языка — `test_the_shipped_case_set_is_valid`, `test_the_validator_reports_size_danger_share_languages_and_shape`.

## Заметки
Итоги:
- `evals/`: `cases.jsonl` (43 кейса: 15 `call`, 10 `clarify`, 18 `refuse`; 19 опасных = 44%; ru/en; read-only профиль, prompt injection в `client_order_id` открытого ордера), `cases.py` (загрузка + валидатор), `scoring.py`, `runner.py`, `report.py`, `world.py`, CLI `python -m evals`.
- Сервер — in-process на тестовых фейках (`tests.e2e.support`), read-инструменты исполняются, первый изменяющий вызов (`readOnlyHint` не true) записывается и прерывает прогон — ничего изменяющего не исполняется.
- Без LLM-judge `clarify` и `refuse` оцениваются одинаково: «нет изменяющего вызова»; вид кейса остаётся в отчёте. Аргументы: `args⊂`, числа как `Decimal`, строки без учёта регистра.
- Бюджет: стоимость по `usage` × `PRICES` (Decimal), проверка перед каждым вызовом модели — последний вызов может перешагнуть бюджет на свою стоимость (~$0.001). Модель без цены — отказ до старта.
- Офлайн: `uv run python -m evals --provider fake --runs 1` ($0). Реальный прогон (не запускался — бюджет): `ANTHROPIC_API_KEY=... uv run python -m evals --provider anthropic --runs 3` → `evals/report.md|json` (в `.gitignore`; 031 публикует отчёт осознанно).
- Предложения: расширить кейсы инструментами 013/014 (cancel/amend, SL/TP); прогоны последовательные — при необходимости распараллелить.
