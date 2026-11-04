# 014: Перпетуалы: SL/TP, закрытие позиции, плечо
Статус: IN_REVIEW
Зависит от: 012
Требования: FR-6, FR-7, FR-8, FR-14, FR-27 (D-21), FR-43

## Цель
Агент ставит/меняет SL/TP позиции, закрывает позицию полностью или частично, меняет плечо (только перпетуалы); каждое действие классифицировано по риску; неизвестный исход разведывается по состоянию позиции.

## Scope
- MCP: `set_trading_stop`, `close_position`, `set_leverage`; спот → понятный отказ (FR-6/7).
- Закрытие — reduce-only Limit IOC в пределах позиции; SL/TP — `position/trading-stop`; плечо — 110043 = успех; увеличение плеча — INCREASES и проверка FR-14.
- FR-27 без client id: плечо и SL/TP — сверка с `position/list`.

## Non-scope
- Подтверждение (016+).

## Acceptance criteria
- [x] Unit (edge case): стоп/закрытие позиции с notional больше per-order лимита разрешены (FR-13, FR-43). `test_a_stop_on_a_position_above_the_order_limit_is_allowed_and_read_back`, `test_closing_a_position_above_the_order_limit_is_a_reduce_only_market_order`
- [x] Unit: закрытие с объёмом больше позиции урезается; на споте — отказ. `test_a_partial_close_is_cut_to_the_position_and_a_short_is_closed_with_a_buy`, `test_spot_is_refused_with_a_plain_message_and_nothing_is_sent`, e2e `test_spot_gets_a_plain_refusal`
- [x] Unit: плечо выше политики → `limit_exceeded`; уменьшение плеча — REDUCES. `test_a_leverage_above_the_policy_is_limit_exceeded_and_journaled`, `test_lowering_the_leverage_reduces_risk_and_needs_no_limit_even_above_the_policy`, kill switch: `test_the_kill_switch_blocks_raising_the_leverage_before_anything_is_sent`, `test_a_kill_switch_does_not_stop_closing_or_protecting_a_position`
- [x] Integration: 110043 → успех; неизвестный исход set_leverage → сверка позиции. `test_leverage_already_at_that_value_110043_is_success`, `test_a_leverage_the_account_already_has_is_a_success_read_back_from_the_position`, `test_a_leverage_change_that_timed_out_is_settled_by_the_positions_leverage` (unknown + несовпадение чтения → `outcome_unknown`, без повторного POST)

## Итоги
- Три инструмента на общем `ActionPath` (`application/position_actions.py`): `set_trading_stop`, `set_leverage` читают позицию (`get_symbol_position`) для read-back и разведки (D-21); `close_position` строит reduce-only market `PlaceOrder` по прочитанной позиции и отправляет его через `SubmitPlaceOrder` (композиция: `_read_market/_plan/_send`), поэтому клиентский id, разведка по orderLinkId и ответ как у `place_order`.
- Риск (FR-43): close и SL/TP — REDUCES; плечо выше текущего (или текущее неизвестно) — INCREASES (kill switch, allowlist, `max_leverage`); равное или ниже — REDUCES. Спот → `invalid_params` (`linear_only`).
- Адаптер: `PositionActions` (порт), `110043` и `34040` («не изменено») — успех; прочие неизвестные/недокументированные коды → `OutcomeUnknown` (правило 012). В `submit_action.py` добавлен только алиас `MarketView`.

## Заметки
- Предложение: снятие SL/TP (`"0"`) не реализовано (убирает защиту; по FR-43 «установка/изменение» — REDUCES, а снятие по смыслу INCREASES) — отдельное решение для SPEC.
- Предложение: гонка двух параллельных `set_leverage` одного пользователя — риск считается по плечу, прочитанному до блокировки; аналог `_guard_locked` для амендов не добавлен (демо-объём).
- Коды ошибок trading-stop вне семейств 10001/110xxx трактуются как unknown (консервативно); реальные коды стоит уточнить на demo-прогоне (`-m testnet`).
