# 020: UI-канал подтверждения (MRTR) и AC-1
Статус: IN_REVIEW
Зависит от: 003, 017
Требования: FR-20, FR-21, FR-22, FR-42, FR-47а, AC-1, ADR-005 (D-1, D-2, D-4, D-5, D-6)

## Цель
Если политика доверяет UI клиента и клиент объявил elicitation, подтверждение идёт в UI клиента в рамках вызова; агент получает результат действия. Проходит сценарий AC-1.

## Scope
- Реализация на high-level `MCPServer` по шаблону PLAN «Спайк 002» (раздел 4) и итогам 003: признак второго раунда — только `ctx.request_state is not None`; `inputResponses` без `requestState` → `invalid_params`; подтверждение — только `ElicitResult` с `action == "accept"` и `content.confirm is True`, всё остальное — отказ; `RequestStateSecurity(keys=[подключ request_state], audience="guarded-exchange")`.
- D-1: второй раунд — отдельная ветка до идемпотентности, не тратит write-токен; повтор первого раунда при PENDING → тот же pending.
- D-2: один CAS `PENDING→EXECUTING` + общий `ExecuteConfirmed`; второй раунд, заставший не-PENDING, возвращает статус/результат.
- D-4/D-5: digest, principal = `pending.user_id`, канал на момент решения, `cancel` → CANCELLED, ключ `request_state` из master key.
- `trust_client_ui` (testnet да / mainnet нет), `prefer_external_channel`; фиктивный decline журналируется как «declined by client UI».

## Non-scope
- AC-4 adversarial (021).

## Acceptance criteria
- [x] E2E (in-process Client): AC-1 — `get_positions` → `set_trading_stop` −3% → форма → accept → результат + журнал с каналом. — `test_ac1_positions_then_stop_loss_is_confirmed_in_the_client_ui_and_journaled`
- [x] E2E: второй раунд с изменёнными аргументами → отказ (FR-22). — `test_a_second_round_with_other_arguments_is_refused_and_nothing_is_executed`, `test_a_state_minted_for_one_tool_cannot_confirm_another`
- [x] E2E: первый вызов с `inputResponses` без `requestState` → `invalid_params`, pending не создан; ретрай только с `requestState` → тот же pending; accept с `confirm=false` / без поля → не подтверждение. — `test_input_responses_without_request_state_are_refused_and_create_nothing`, `test_a_state_only_retry_asks_again_about_the_same_pending`, `test_anything_but_accept_with_confirm_true_is_not_a_confirmation`, `test_a_repeat_during_the_elicitation_gets_the_same_pending_and_sends_nothing`
- [x] E2E: повтор второго раунда после исполнения → тот же результат, одно исполнение. — `test_repeating_the_second_round_after_execution_returns_the_result_and_executes_once`
- [x] Unit: гонка UI-accept и CLI approve → исполнение одно, канал победителя в журнале. — `test_a_form_accept_and_a_cli_approve_at_the_same_time_execute_once`, `test_a_cli_approval_first_leaves_the_form_answer_with_the_state_it_found`
- [x] Unit: mainnet / `prefer_external_channel` → UI не используется. — `test_a_policy_that_does_not_trust_the_ui_never_shows_the_form`, `test_with_the_cli_enabled_such_a_policy_parks_the_action_for_the_cli`, `tests/e2e/test_ac4_adversarial.py::test_mainnet_never_trusts_the_client_ui_by_default`
- [x] Integration: рестарт процесса между раундами → второй раунд принят (тот же ключ request_state). — `test_a_restart_between_the_rounds_keeps_the_form_valid_with_the_same_key` (новый объект сервера над тем же хранилищем и ключом, выведенным из master key; не отдельный процесс и не SQLite)
- [ ] 👤 Ручная проверка AC-1 в клиенте по итогам 003 — инструкция в отчёте.

## Заметки
Итоги:
- `interface/mcp/confirm_ui.py`: раунд 1 — обычный use case; если он припарковал действие, политика доверяет UI и клиент объявил form на 2026-07-28 — `InputRequiredResult` с формой из серверного pending + `requestState` (id + `terms_digest`). Раунд 2 распознаётся только по `ctx.request_state`, use case действия не вызывает (D-1), ответ читается fail closed. Подключено ко всем семи торговым инструментам (`run_confirmable`).
- `application/client_ui_confirmation.py`: один CAS `PENDING→APPROVED|REJECTED|CANCELLED` под `lock_user`, канал `client_ui` в журнале (`note`: «declined by client UI» и т.п.), политика проверяется на момент решения, затем общий `ExecuteConfirmed` (выделен из executor 017, общий `running`). Проигравший гонку получает найденный статус.
- Возможность UI передаётся в use case через contextvar `offering_client_ui` (иначе менять сигнатуры семи use case); `ConfirmationSupport.client_ui_usable(policy)`. Повтор (`earlier_pending` из ревью 016) тоже получает `ask_client_ui` и тот же pending.
- `RequestStateSecurity(keys=[HKDF request_state], audience="guarded-exchange")` через `create_server(request_state_key=)`; без ключа — эфемерный (тесты).
- Подмену аргументов во втором раунде отсекает SDK (хэш сырых arguments в запечатанном состоянии, -32602); исполняется всегда `pending.action`, не аргументы раунда 2.
- Новые ошибки `confirmation_declined`, `confirmation_not_executed`. README: абзац про UI-канал. 029: AC-5 тесты разскипаны, зелёные.
- 👤 Ручная проверка AC-1: подключить сервер в Claude Code >= 2.1.312 (testnet, ключ, `admin policy set-ceiling --profile trade --allow linear:BTCUSDT`), «покажи позиции и поставь стоп на 3% ниже входа» — должна появиться форма; Space + Enter = исполнение, Enter без галочки = отказ. Claude Desktop не проверен.

После спайка 003 (2026-10-29): см. PLAN ADR-005 п.13 — только MRTR на 2026-07-28 без legacy-ветки, правило SDK для `elicitation.form`, подсказка «Пробел — отметить, Enter — отправить» в `message` формы, текст для README — `spikes/003/README.md`.
