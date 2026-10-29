# 010: История ордеров и сделок
Статус: TODO
Зависит от: 008
Требования: FR-2, FR-3, FR-41

## Цель
Агент читает историю ордеров и сделок за период с пагинацией, с пометкой происхождения.

## Scope
- MCP: `get_order_history`, `get_trades` (`/v5/order/history`, `/v5/execution/list`), период, курсор, лимиты Bybit на глубину истории — в описании инструмента.
- Происхождение — по журналу и префиксу `gx-`.

## Non-scope
- Журнал через MCP (025).

## Acceptance criteria
- [ ] Integration: пагинация курсором на фикстурах; период вне допустимого → `invalid_params` с подсказкой.
- [ ] E2E: история возвращается компактно, с `untrusted_text` для свободных полей.

## Заметки
- Из 008: доменный `Order` (`domain/account.py`) и маппинг `order/realtime` (`mapper._order`, `order_origin`, `UntrustedText` для `orderLinkId`) — переиспользовать для `order/history` (там нужны терминальные статусы: расширить `OrderStatus`, сейчас всё неоткрытое → `unknown`). Ключ — `ReadCredential(user_id)`, порт — `AccountReader`. Ограничение объёма — `account_items_limit` + `limit + 1` → `has_more`; курсор не реализован (см. предложения 008).
