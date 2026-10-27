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
- `GuardedMCPServer` (ADR-013, спайк 002): override `call_tool` — ошибки валидации аргументов SDK → `invalid_params` (`details.field/reason`, без эха значений, для `value_error` только loc+type). `get_ticker` и новые инструменты объявляют аргументы настоящими типами (`Literal`, `Field(pattern=...)`), `WithJsonSchema` убрать; нормализация в доменных value objects остаётся.

## Non-scope
- Валидация ордеров (011).

## Acceptance criteria
- [ ] Integration: маппинг instruments-info/orderbook/kline на фикстурах (spot и linear).
- [ ] Unit: инструмент не в USDT → ошибка; ограничение глубины/числа свечей соблюдается.
- [ ] E2E: три инструмента вызываются через in-process клиента.
- [ ] E2E: пропущенный аргумент, неверный тип и значение вне enum у `get_ticker` → `invalid_params` со structured content и `details.field`; `inputSchema` по-прежнему содержит `enum`/`pattern`.

## Заметки
—
