# 013: Отмена, отмена всех, изменение ордера
Статус: IN_REVIEW
Зависит от: 012
Требования: FR-5, FR-21 (данные о внешних), FR-27 (D-21), FR-43 (Amend/Cancel)

## Цель
Агент отменяет ордер, все ордера по инструменту (конкретными id, с пометкой внешних) и изменяет ордер; изменение классифицируется по риску относительно текущего ордера.

## Scope
- MCP: `cancel_order`, `cancel_all_orders`, `amend_order`.
- Cancel-all: снимок списка id открытых ордеров (включая внешние) → отмена конкретных id; на linear — явный `orderFilter`, TP/SL позиции не трогаются.
- Amend: INCREASES при большем объёме или менее выгодной цене (FR-43), иначе REDUCES; лимиты — только для INCREASES.
- FR-27 для cancel/amend по `orderId` (асинхронность: «принято» ≠ «применено», D-21).

## Non-scope
- Подтверждение (016+).

## Acceptance criteria
- [x] Unit: классификация amend по всем комбинациям объём/цена/сторона — `tests/unit/test_order_changes_domain.py::test_amend_increases_risk_only_with_a_larger_size_or_a_worse_price` (2 стороны × 3 объёма × 3 цены), `::test_amending_a_reduce_only_order_always_reduces_risk`, `::test_the_classification_table_has_both_directions_for_each_side`; через use case — `tests/unit/test_order_changes.py::test_an_increasing_amend_faces_the_order_value_limit`, `::test_an_increasing_amend_is_refused_under_the_kill_switch`.
- [x] Unit: cancel-all формирует список id и помечает внешние ордера — `tests/unit/test_order_changes_domain.py::test_cancel_all_lists_the_plain_open_orders_and_marks_external_ones`, `::test_cancel_all_takes_at_most_its_maximum_and_says_there_is_more`; `tests/unit/test_order_changes.py::test_cancel_all_cancels_the_snapshot_by_id_and_leaves_tpsl_alone`; через MCP — `tests/e2e/test_mcp_order_changes.py::test_cancel_all_through_mcp_names_each_order_and_counts_external_ones`.
- [x] Integration: cancel-all на linear отправляет `orderFilter`, TP/SL позиции не удаляются (фикстура `tests/fixtures/bybit/order_changes/order_realtime_linear_btcusdt_with_position_sl.json`) — `tests/integration/test_bybit_order_changes.py::test_the_cancel_all_snapshot_asks_for_plain_orders_with_an_explicit_order_filter`, `::test_cancel_all_on_linear_cancels_the_listed_ids_and_never_the_position_stop_loss` (см. «Итоги», п. 1: `orderFilter=Order` уходит в запросе снимка, а эндпоинт `cancel-all` не вызывается вовсе).
- [x] Integration: неизвестный исход cancel/amend → разведка по статусу ордера — `tests/integration/test_bybit_order_changes.py::test_a_cancel_that_timed_out_is_found_cancelled_by_its_order_id`, `::test_an_amend_the_exchange_did_not_confirm_is_settled_by_the_orders_status` (applied / not applied), `::test_codes_after_which_the_change_may_have_happened_are_unknown_and_never_retried`; на fakes — `tests/unit/test_order_changes.py::test_an_unknown_cancel_is_settled_by_the_orders_status_without_resending`, `::test_an_unknown_cancel_the_order_does_not_show_stays_unknown`, `::test_an_unknown_amend_is_settled_by_reading_the_order`, `::test_an_unknown_cancel_stops_the_rest_and_is_settled_by_one_read`.
- [x] (по сообщению оркестратора о 015) Kill switch для новых действий: `tests/unit/test_order_changes.py::test_cancels_still_work_under_the_kill_switch`, `::test_an_increasing_amend_is_refused_under_the_kill_switch`, `::test_the_kill_switch_is_checked_again_in_the_decision`.

## Заметки
- Из ревью 012 (раунд 1): классифицировать коды `amend`/`cancel` по правилу 012 «неизвестно по умолчанию вне документированных семейств» и явно занести в неизвестные (исход возможен, нужна разведка по `orderId`, D-21): spot `170147` «Order cancellation timeout», `170150` «Cancel order failed», `170191` «Can not cancel order, please try again later», `170212` «Cancel order request processing», `170310` «Order modification timeout», `170311` «Order modification failed»; linear `110079` «The order is processing and can not be operated, please try again later». Сверено с https://bybit-exchange.github.io/docs/v5/error 2026-11-03.

### Итоги реализации (2026-11-03)
1. **Cancel-all без эндпоинта `cancel-all`.** Scope требует «отмену конкретных id», AC — «явный `orderFilter`». Оба выполнены так: снимок читается `GET /v5/order/realtime?symbol=…&orderFilter=Order` (явный фильтр, обе категории), затем каждый ордер снимка отменяется `POST /v5/order/cancel` по `orderId`. `POST /v5/order/cancel-all` не вызывается: даже с `orderFilter=Order` он отменил бы ордера, появившиеся после снимка (не показанные агенту; в 016 — человеку, FR-22), а без фильтра на linear — TP/SL позиции. Trade-off: N запросов вместо одного (≤ 20 за вызов, последовательно, общий дедлайн отправки; остановка на первом неизвестном исходе/переходящем отказе) — дольше и тратит лимит биржи, зато трогается ровно увиденное. Домен вдобавок отбрасывает условные/неоткрытые строки (защита, если фильтр биржи не сработает — это и проверяет фикстура со стоп-лоссом позиции).
2. **Какие ордера можно трогать / внешние.** Только прочитанные этим вызовом: cancel/amend сначала читают ордер по `orderId` (`realtime` → `history`), снимок — как выше. Внешние разрешены (FR-5) и помечены `origin` в ответе и журнале (`order_attempt.order.origin`, `orders[*].origin`, `external_count`) — данные для FR-21 в 016. Условные ордера и TP/SL позиции — отказ `invalid_params` (`conditional_order`): снятие стопа увеличивает риск, а TP/SL — тикет 014. Ордер не открыт/не найден — `not_found` (`order_not_open`/`order_not_found`), журналируется как `pre_check`.
3. **Один путь.** `submit_action.py` разделён на `ActionPath` (весь путь 011/012/015) и хуки действия; `SubmitPlaceOrder` — подкласс, все 1180 тестов place_order прошли без изменений сразу после рефакторинга (до новых действий). Kill switch из 015 перенесён в общий путь: ранняя проверка — до ключа, если риск известен из аргументов, иначе (amend) — сразу после чтений; под `lock_user` — всегда.
4. **Риск amend (FR-43):** больше объём ИЛИ хуже цена → INCREASES (kill switch, allowlist, `max_order_notional_usdt` на `qty × price` всего ордера, плечо); иначе REDUCES. Reduce-only ордер при изменении — всегда REDUCES, рост урезается до `filled + размер позиции` (`clamped_to_position`). Новая цена — на шаге и внутри коридора `price-limit` (как limit в 011), объём — вниз к шагу, выше исполненного, ≥ min_qty; min notional — кроме reduce-only. Ничего не меняется — `invalid_params` (`no_change`).
5. **Асинхронность (D-21):** ответ — что известно: `cancelled`/`amended` только по чтению ордера, иначе `cancel_requested`/`amend_requested` (не ошибка), `filled`, если ордер исполнился раньше отмены. Неизвестный исход — чтение по `orderId` (POST не повторяется): видна цель → результат с `result_source=reconnaissance`; нет — `outcome_unknown` с `details.reconnaissance` и подсказкой «повтор отмены/того же amend безопасен».
6. **Коды** (п. «Заметки» + сверка 2026-11-03): все семь из ревью 012 — неизвестный исход (ни один не утверждает, что ордер остался прежним; `170191`/`110079` формально «нельзя», но «попробуйте позже»/«в обработке» — состояние ордера в движении, чтение дешёвое и безопасное); добавлены `170190` «Cancel order has been finished» (чья отмена — не сказано) и `40004` («modified during the process of replacing, check the order status»). «Ордер не открыт» `110001`, `110008`, `110010`, `170142`, `170213` → `not_found` (ничего не изменено).
7. **Идемпотентность:** явный ключ — как у ордеров (повтор → `replay`, ключ на другое действие → `idempotency_conflict`); fingerprint-окна нет (D-20 (3); amend задаёт абсолютные значения).
8. **Проверка мутациями** (каждая мутация — откат одного решения, прогон затронутых тестов; скрипт вне репозитория): убиты все 20 — игнор худшей цены в классификации; reduce-only не особый; условные не отфильтрованы (сначала выжила: в тесте стоп был `Untriggered` и отсекался по статусу — добавлен условный `New`); внешние не помечены; снимок без `orderFilter`; нет разведки после неизвестной отмены; нет сверки cancel-all; `170310`/`110079`/`170191` как определённые; «не открыт» как неизвестные; нет ранней и нет повторной под lock проверки kill switch; пропуск лимитов amend; игнор подтверждения отмены; cancel-all не останавливается на неизвестном; переходящий отказ cancel-all проглочен; нет чтения после принятия; ответ про чужой ордер принят; hedge mode при чтении по id.
9. **Проверки:** `uv run pytest -q` — зелёный, `ruff check`, `ruff format --check`, `mypy` (strict), `lint-imports` — зелёные. `docs/traceability.md` перегенерирован.

### Ревью, раунд 1 (2026-11-03)
Вердикт CHANGES_REQUESTED: рефакторинг `ActionPath` и решение №1 (снимок + отмена по id) приняты; выжили 16 мутаций ревьюера на новой логике. Исправлено в той же ветке:
1. **Тесты на выжившие мутации** — `tests/unit/test_order_changes_review1.py` (стиль `test_submit_action_review1/2.py`, `..._reconnaissance.py`), домен — `tests/unit/test_order_changes_domain.py`, коды — `tests/integration/test_bybit_order_changes.py`. Повторный прогон 16 мутаций ревьюера (скрипт вне репозитория), все кроме одной убиты:
   - R1 shield в `_change_one` — `test_a_caller_cancelled_during_the_post_still_records_the_outcome_and_keeps_the_key[cancel|amend]`;
   - R2 ветка бага адаптера — `test_a_bug_in_the_adapter_is_an_unknown_outcome_journaled_as_such`;
   - R3 `_send_within_deadline` — `test_a_cancel_that_never_answers_hits_the_deadline_and_is_looked_up` (`call_with_watchdog`);
   - R4 shield cancel-all — `test_a_caller_cancelled_during_the_post_..._keeps_the_key[cancel_all]`;
   - R5 `_nothing_happened` (a=`cancel_requested`, b=`rate_limited`) — `test_an_accepted_cancel_then_a_rate_limit_is_a_result_and_the_key_is_kept`;
   - R6 сбой чтения сверки — `test_a_failed_verification_read_never_turns_a_state_into_closed`;
   - R7 неизвестный, исчезнувший из списка → `closed` — `test_an_unknown_cancel_whose_order_left_the_list_is_closed`;
   - R8 `has_more` при сверке — `test_a_list_with_more_pages_settles_nothing`;
   - R9 исчерпанный общий дедлайн — `test_an_exhausted_overall_deadline_sends_nothing_more`;
   - R10 баг в одной отмене останавливает остальные — `test_a_bug_in_one_cancel_stops_the_rest`;
   - R11 Outcome UNKNOWN/ACCEPTED — `test_the_stored_outcome_is_unknown_exactly_when_an_order_is_unknown`;
   - R12 порог подтверждения amend — `test_an_amend_at_the_confirmation_threshold_needs_confirmation`;
   - R13 `170190`, R14 `40004` в `_UNKNOWN_CODES` — R13 убита; **R14 эквивалентна**: `40004` вне документированных семейств, поэтому «неизвестно» и без явного списка (запись в списке — документация; поведение покрыто тестом);
   - R15 сторона позиции при росте reduce-only — `test_a_reduce_only_buy_against_a_long_cannot_grow`;
   - R16 reduce-only без min notional — `test_only_a_reduce_only_order_is_exempt_from_the_minimum_order_value`.
2. **Коды:** `170143` → `not_found`, `170144` → определённый отказ (обоснование — PLAN «Тикет 013», п. 6); тесты `test_definite_answers_to_a_change[170143|170144]`; `170190`, `40004` добавлены в `_UNKNOWN_CODES`.
3. **TOCTOU amend (D-8)** — проверка под `lock_user` по журналу за 60 с (PLAN «Тикет 013», п. 7): `test_an_amend_after_an_accepted_but_invisible_one_is_refused` (сценарий 1, обход FR-13), `test_a_reducing_looking_amend_cannot_slip_past_the_kill_switch_on_a_stale_read` (сценарий 2, обход kill switch), `test_a_change_confirmed_after_this_calls_read_makes_the_read_stale`, `test_a_change_still_in_flight_blocks_an_amend_of_the_same_order`, `test_a_definitely_refused_earlier_change_does_not_block`; SQL — `test_recent_journal_records_of_some_types_are_read_back_from_sqlite`.
4. **cancel-all `except`** разделён: `Exception` → `outcome_unknown` (`internal_error`) с подсказкой `get_open_orders` — `test_a_bug_after_the_sends_is_an_unknown_outcome_with_a_hint`; прочие `BaseException` — запись и проброс — `test_a_non_error_interruption_is_recorded_and_re_raised`.
5. **Amend: закрытый ордер при чтении** → `order_closed` — `test_an_amend_whose_order_closed_before_it_applied_says_so`.
6. PLAN: отсутствие fingerprint-окна для amend записано как уточнение D-20; `next_step` cancel-all говорит, что повтор берёт НОВЫЙ снимок и отменит и новые (в т.ч. внешние) ордера; `cancel-batch` поднят в предложениях.
7. **Мутации на новые исправления** (тот же скрипт), все убиты: `170143` не в «не открыт»; хук guard удалён; guard игнорирует поздний исход / состояние `*_requested` / изменение в полёте / не пропускает определённо отказанные; `order_closed` → `amend_requested`; баг cancel-all пробрасывается как есть; `since` без фильтра по времени / по типу. Две из них (`*_requested`, в полёте) сначала выжили: в тестах исход совпадал по времени с чтением и ловился правилом «записан после чтения» — тесты разведены по времени, добавлен тест «в полёте».
8. Проверки: `uv run pytest -q`, `ruff check`, `ruff format --check`, `mypy`, `lint-imports` — зелёные; `docs/traceability.md` перегенерирован.

### Предложения (вне scope)
- **Пакетная отмена `POST /v5/order/cancel-batch`** (до 20 id за запрос) вместо N запросов `order/cancel`: сейчас cancel-all на 20 ордеров тратит 20 единиц лимита биржи (linear `order/cancel` — 10/с на UID) и ~5 с последовательных запросов; при нескольких клиентах пользователя это быстро упирается в `rate_limited` посреди отмены (остаток `not_sent`). Batch — одна единица на пакет и та же «отмена ровно увиденных id»; нужен спайк по поэлементным кодам в `retExtInfo.list` и их классификации (неизвестно/определённо).
- Сверять `origin=system` с журналом (`order_attempt.client_order_id`) вместо префикса `gx-`: сейчас внешний бот с префиксом `gx-` будет помечен как «наш». Важно для FR-21 в 016 (показ человеку внешних ордеров); нужен индекс client order id → action в БД.
- В 016 pending для cancel-all должен фиксировать список id снимка в digest, а исполнитель — отменять только их (сейчас это свойство уже есть у пути, но digest считается от аргументов).
- Отмена условных ордеров (не TP/SL позиции) — отдельное действие с классификацией: снятие стопа увеличивает риск, снятие условного входа — снижает.
- Индекс `audit_log(chain_id, ts)` или отдельная таблица «изменения ордера в полёте», если журнал пользователя станет большим (проверка D-8 amend читает хвост цепочки по времени).
- FR-46 (v2) не должен считать отмены и снижающие amend.
