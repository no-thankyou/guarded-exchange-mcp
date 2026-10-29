# 020: UI-канал подтверждения (MRTR) и AC-1
Статус: TODO
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
- [ ] E2E (in-process Client): AC-1 — `get_positions` → `set_trading_stop` −3% → форма → accept → результат + журнал с каналом.
- [ ] E2E: второй раунд с изменёнными аргументами → отказ (FR-22).
- [ ] E2E: первый вызов с `inputResponses` без `requestState` → `invalid_params`, pending не создан; ретрай только с `requestState` → тот же pending; accept с `confirm=false` / без поля → не подтверждение.
- [ ] E2E: повтор второго раунда после исполнения → тот же результат, одно исполнение.
- [ ] Unit: гонка UI-accept и CLI approve → исполнение одно, канал победителя в журнале.
- [ ] Unit: mainnet / `prefer_external_channel` → UI не используется.
- [ ] Integration: рестарт процесса между раундами → второй раунд принят (тот же ключ request_state).
- [ ] Ручная проверка AC-1 в клиенте по итогам 003 — инструкция в отчёте.

## Заметки
—

После спайка 003 (2026-10-29): см. PLAN ADR-005 п.13 — только MRTR на 2026-07-28 без legacy-ветки, правило SDK для `elicitation.form`, подсказка «Пробел — отметить, Enter — отправить» в `message` формы, текст для README — `spikes/003/README.md`.
