# 009: Чтение рынка: стакан, свечи, правила инструмента
Статус: TODO
Зависит от: 001
Требования: FR-1, FR-3, FR-10 (данные)

## Цель
Агент читает стакан, свечи и правила инструмента; правила маппятся в доменный `InstrumentRules` и кэшируются для валидации ордеров.

## Scope
- MCP: `get_orderbook`, `get_candles`, `get_instrument_rules`.
- Domain `instruments.py`: `Instrument`, `InstrumentRules` (tick, qty step, min qty/notional, max leverage, статус торгов); только USDT-котировки (FR-4) — иначе понятная ошибка.
- Кэш правил с TTL в адаптере/application (порт), ограничение объёма ответа (FR-3).

## Non-scope
- Валидация ордеров (011).

## Acceptance criteria
- [ ] Integration: маппинг instruments-info/orderbook/kline на фикстурах (spot и linear).
- [ ] Unit: инструмент не в USDT → ошибка; ограничение глубины/числа свечей соблюдается.
- [ ] E2E: три инструмента вызываются через in-process клиента.

## Заметки
—
