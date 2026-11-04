# 016: Pending, CLI-подтверждение и executor (однопроцессный)
Статус: DONE
Зависит от: 012, 015
Требования: FR-19, FR-20, FR-22, FR-23, FR-24, FR-25, FR-47, FR-29 (замена ключа → аннулирование)

## Цель
Действие, требующее подтверждения, создаёт pending (FR-47б); человек одобряет/отклоняет через CLI (если канал включён админом); executor исполняет одобренное сам; агент видит статус и может отменить своё pending.

## Scope
- Domain `confirmation.py`: автомат состояний (PLAN 5.1), `action_digest` (D-4), TTL; таблица `pending_confirmations`.
- Use cases: `SubmitAction` → pending (лимит одновременных pending count+insert, D-17; повтор с тем же digest возвращает тот же pending, D-20); `ResolveConfirmation` (только не-MCP); `CancelConfirmation`.
- MCP: `get_confirmation_status`, `cancel_confirmation`; CLI `confirm approve|reject <id>` — доверен только если включён админом (FR-42, E-5).
- `ConfirmedActionExecutor`: сигнал + scan, CAS `APPROVED→EXECUTING`, общий `ExecuteConfirmed` с базовой перепроверкой (доступ, credential, kill switch, лимиты), дедлайн `approved_at + 30 с` (D-3), `asyncio.shield`.
- Замена/удаление ключа аннулирует PENDING/APPROVED (D-9); всё в журнал с каналом решения.

## Non-scope
- Полная таблица FR-48 (017), Telegram (018), несколько процессов (019), MRTR (020).

## Acceptance criteria
- [x] Unit: двойное одобрение — исполнение ровно одно (CAS); одобрение после TTL → `expired`; отказ → `rejected` в журнале. (`tests/unit/test_confirmations.py::test_a_double_approval_executes_exactly_once`, `test_two_concurrent_approvals_have_one_winner`, `test_approving_after_the_ttl_is_expired_and_executes_nothing`, `test_a_rejection_is_journaled_with_its_channel_and_nothing_is_sent`; SQL CAS — `tests/integration/test_confirmation_storage.py::test_only_one_of_two_decisions_wins_and_an_expired_one_cannot_be_approved`)
- [x] Unit: превышение лимита одновременных pending → `pending_limit_reached`. (`tests/unit/test_confirmations.py::test_more_waiting_confirmations_than_the_limit_are_refused`, `test_waiting_confirmations_expire_in_the_sweep_and_free_their_slot`)
- [x] Unit: повтор вызова без ключа во время PENDING возвращает тот же pending (через 40 с тоже). (`tests/unit/test_confirmations.py::test_a_repeat_without_a_key_returns_the_same_pending_even_after_the_fingerprint_window`, `test_a_repeat_with_the_same_key_meets_the_same_pending_and_a_different_call_conflicts`)
- [x] Unit: без доверенного канала → `confirmation_channel_unavailable`, pending не создан (FR-25). (`tests/unit/test_confirmations.py::test_without_an_enabled_channel_it_is_refused_and_no_pending_is_created`, `test_the_cli_cannot_decide_unless_the_admin_enabled_it`)
- [x] Unit: `cancel_confirmation` переводит только своё PENDING/APPROVED в CANCELLED и не исполняет. (`tests/unit/test_confirmations.py::test_cancelling_your_own_pending_or_approved_confirmation_executes_nothing`, `test_the_agent_reads_its_status_and_cannot_see_another_users_confirmation`, `test_an_executing_confirmation_cannot_be_cancelled`; AC-4 — `tests/e2e/test_mcp_confirmation.py::test_no_tool_can_approve_and_an_agent_that_tries_gets_nothing_executed`)
- [x] Integration: CLI approve за 1 с до TTL → исполнено (D-3); после замены ключа pending аннулирован. (`tests/unit/test_confirmations.py::test_an_approval_one_second_before_the_ttl_still_executes`, `test_an_approval_that_is_not_started_in_time_expires_after_approval`, `test_deleting_the_key_cancels_waiting_and_approved_confirmations`; CLI на реальной SQLite — `tests/integration/test_confirmation_storage.py::test_the_cli_lists_shows_external_orders_and_approves_only_when_the_admin_enabled_it`)

## Заметки
—

### Итоги реализации (2026-11-04)
1. Pending — общий для всех `ActionPath` (011, 013, 014): три хука (`_confirm_notional`, `_parse_payload`, `_confirmed_plan`), `shown` = `_attempt_details`; действие без `_confirm_notional` остаётся fail closed. Тесты проходят по всем семи действиям.
2. Pending — `ConfirmationPending` (исключение-ответ, не ошибка); `run_tool` отдаёт `status: pending`. Выходные схемы торговых инструментов — `RootModel[Out | ConfirmationOut]` (SDK валидирует результат по объявленной модели).
3. Канал CLI включает `GX_CONFIRM_CLI_ENABLED` (по умолчанию выкл., нужен и серверу, и CLI). Одобрение лишь ставит `APPROVED`; исполняет `ConfirmedActionExecutor` в процессе сервера (скан 1 с; запускается из `serve`). Сервер должен работать.
4. `id` pending = `action_id` → `client_order_id` детерминирован; исход закрывает pending в той же транзакции, что и журнал `order_outcome` (actor `system:executor`).
5. Захваты идемпотентности при создании pending освобождаются; повтор ищется по ключу (24 ч) и по digest открытого PENDING. D-20 (5) (запись обновляется итогом) не сделано: повтор с ключом возвращает текущий статус pending.
6. Замена/удаление ключа отменяет `PENDING/APPROVED` (`invalidate_pending`, D-9). Отмена pending при включении kill switch (D-7) — нет: исполнение откажет на перепроверке; предложение на потом.
7. Рестарт: `EXECUTING` старше 30 с, не в работе процесса → разведка (place/close: `find_order`; остальные — `UNKNOWN`); `APPROVED` старше 30 с → `EXPIRED_AFTER_APPROVAL`. Один процесс; лидерство — 019.
8. Происхождение `system` по префиксу id в CLI помечено «не проверено»; сверка с журналом (из заметок 013) — не сделана (в `TouchedOrder` нет client id).
9. Проверки: pytest, ruff check, ruff format --check, mypy, lint-imports — зелёные; `docs/traceability.md` перегенерирован.

### Ревью, раунд 1 (2026-11-04)
Исправлено: повтор вызова (по ключу в любом статусе pending за 24 ч или по digest открытых PENDING/APPROVED/EXECUTING) ищет свой pending **до** правила подтверждения (`earlier_pending` в `_confirm_if_required`) и получает его, даже если подтверждение уже не требуется (цена упала ниже порога, потолок ослаблен) — иначе уходило два ордера под одним ключом. Регрессионные тесты: `test_a_keyed_repeat_after_execution_...`, `..._while_approved_...`, `test_a_repeat_while_pending_after_the_price_dropped_below_the_threshold_stays_one_order[k-1|None]`. M-1: граница цены FR-48 для buy и sell (`test_a_buy_is_held_...`, `test_a_sell_is_held_...`; мутант `price = price` убит). Повтор ключа закрытого pending говорит «новая попытка — новый ключ». `close_position` отказывает, если сторона позиции сменилась (`position_changed`; в `shown` добавлено `close_side`). Тест перепроверки профиля под lock.

### v2 (замечания ревью, не сделано)
- Executor исполняет `APPROVED` последовательно: долгое исполнение одного может задержать следующий за дедлайн `approved_at + 30 с` (он тогда `EXPIRED_AFTER_APPROVAL`); при росте нагрузки — параллельные задачи.
- Скан берёт `limit=50` на статус (`_listed`): при большом числе записей часть ждёт следующего прохода.
- D-7: eager-отмена `PENDING/APPROVED` при включении kill switch.
- После сбоя записи исхода pending остаётся `EXECUTING`, потом разведка даёт `UNKNOWN` без деталей отправки (`details`), кроме `reconnaissance`.
