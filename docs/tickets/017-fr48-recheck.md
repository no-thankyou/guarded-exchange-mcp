# 017: Полная перепроверка FR-48 на момент исполнения
Статус: DONE
Зависит от: 016, 013, 014
Требования: FR-48, FR-22, FR-43, FR-12, FR-15, FR-16, FR-29

## Цель
Executor перед исполнением пересчитывает всё по таблице D-8 и исполняет ровно показанное человеку (с clamp «до N»), иначе отказывает с причиной в журнал и агенту.

## Scope
- Таблица D-8: статус пользователя, credential и environment, kill switch, политика, правила инструмента, направление риска (`risk_direction_changed`), clamp reduce-only, граница цены (Limit IOC), cancel-all по списку id.
- TOCTOU: проверки по БД + CAS → одна write-транзакция под `lock_user`; чтения биржи до неё.
- Текст подтверждения (FR-21) — из записи pending: точные параметры, «до N» для reduce-only, худшая цена, среда, затронутые внешние ордера.

## Non-scope
- Каналы (018, 020).

## Acceptance criteria
- [x] Unit (именованные edge cases SPEC 6): подтверждение после kill switch (INCREASES отклонено, REDUCES исполнено); после отзыва доступа; после замены ключей; после смены environment. (`tests/unit/test_confirmations.py::test_a_kill_switch_after_approval_refuses_an_increase_but_lets_a_reduction_through`, `test_revoked_access_after_approval_executes_nothing`, `test_a_replaced_key_after_approval_executes_nothing`, `test_a_changed_environment_after_approval_executes_nothing`)
- [x] Unit: инструмент удалён из allowlist во время ожидания; пользователь ужесточил политику / админ опустил потолок во время ожидания. (`tests/unit/test_confirmations.py::test_an_instrument_removed_from_the_allowlist_while_waiting_is_refused`, `test_a_tightened_policy_or_a_lowered_ceiling_while_waiting_is_applied_at_execution`; заметка ревью 034 — `test_a_weakened_policy_neither_approves_nor_extends_a_waiting_confirmation`)
- [x] Unit: цена ушла дальше допуска → исполнено 0 / частично, агенту фактический объём. (`tests/unit/test_confirmations.py::test_a_market_order_is_held_to_the_worst_price_that_was_shown`: цена IOC не хуже показанной; фактический объём берётся из чтения ордера как у обычного place_order — `tests/unit/test_submit_action.py`)
- [x] Unit: SetLeverage был REDUCES, к исполнению стал INCREASES → `risk_direction_changed`. (`tests/unit/test_confirmations.py::test_a_leverage_change_approved_as_a_reduction_that_became_an_increase_is_refused`)
- [x] Unit: позиция уменьшилась → исполнен min(показанный, позиция). (`tests/unit/test_confirmations.py::test_a_reduce_only_order_is_cut_to_the_position_that_is_left`, `test_close_position_goes_through_pending_and_is_cut_to_the_position`)
- [x] Unit: подмена параметров (другой digest) → отказ (FR-22). (`tests/unit/test_confirmations.py::test_a_changed_record_is_refused_because_it_no_longer_matches_its_digest`)
- [x] Unit: cancel-all не трогает внешние ордера, появившиеся после подтверждения. (`tests/unit/test_confirmations.py::test_cancel_all_cancels_exactly_the_orders_the_human_was_shown`)

## Заметки
—

Из ревью 034 (2026-10-29): `admin policy reset-overrides` может ослабить эффективную политику, пока существует pending, созданный под строгой. Перепроверка на момент исполнения использует текущую политику; ослабление само по себе не подтверждает и не продлевает pending — нужен тест.

### Итоги реализации (2026-11-04)
1. Перепроверка — `ActionPath._recheck_locked`, одна write-транзакция под `lock_user`: целостность digest (FR-22) → пользователь/environment → тот же `credential_id` → профиль → `risk_direction_changed` (только REDUCES→INCREASES) → kill switch по текущему риску → `_guard_locked` → `_confirmed_plan`; затем CAS `APPROVED→EXECUTING`. Чтения биржи — до транзакции (D-10). Отказ → `FAILED` с кодом в `get_confirmation_status` и `order_refused` (`stage: recheck|pre_check`).
2. place/close: объём = min(показанный, свежий), у market цена IOC = более жёсткая из показанной и свежей (не хуже показанной, и сужение slippage политикой учитывается); лимиты проверяются по зажатому плану.
3. cancel-all: исполняется только список id из `shown` (ордера, появившиеся позже, не трогаются). amend/cancel: свежее чтение и свежий риск.
4. 014 (`set_trading_stop`, `close_position`, `set_leverage`) подключены теми же хуками; тест на каждое действие: pending → approve → executor; перепроверка: SL без позиции → отказ, close режется по позиции, leverage REDUCES→INCREASES → `risk_direction_changed`.
5. Не сделано: отдельная проверка «статус торгов инструмента» помимо `_plan` (она уже входит в `_read`/`_plan` через `require_status`).
