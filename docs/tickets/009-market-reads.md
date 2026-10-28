# 009: Чтение рынка: стакан, свечи, правила инструмента
Статус: IN_REVIEW
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
- [x] Integration: маппинг instruments-info/orderbook/kline на фикстурах (spot и linear). — `tests/integration/test_bybit_market_reads.py` (фикстуры `tests/fixtures/bybit/{orderbook,kline,instruments}_*.json`, публичные ответы api-demo.bybit.com 2026-10-28, без ключей)
- [x] Unit: инструмент не в USDT → ошибка; ограничение глубины/числа свечей соблюдается. — `tests/unit/test_market_domain.py` (`test_instrument_not_quoted_in_usdt_is_rejected_with_a_hint`, `test_orderbook_depth_is_bounded`, `test_candles_limit_is_bounded`), `tests/unit/test_read_market.py` (`test_rules_of_an_instrument_not_quoted_in_usdt_are_refused`, `test_orderbook_depth_is_respected_even_if_the_exchange_returns_more`, `test_candles_limit_is_respected_even_if_the_exchange_returns_more`)
- [x] E2E: три инструмента вызываются через in-process клиента. — `tests/e2e/test_mcp_market_reads.py` (`test_get_orderbook`, `test_get_candles`, `test_get_instrument_rules` — spot и linear, structured content валиден по `outputSchema`)
- [x] E2E: пропущенный аргумент, неверный тип и значение вне enum у `get_ticker` → `invalid_params` со structured content и `details.field`; `inputSchema` по-прежнему содержит `enum`/`pattern`. — `tests/e2e/test_mcp_market_reads.py::test_get_ticker_bad_arguments_are_invalid_params`, `test_get_ticker_input_schema_still_has_enum_and_pattern`

## Заметки
Решения и отклонения (2026-10-28, исполнитель):
- **`GuardedMCPServer` — отдельный модуль `interface/mcp/guarded_server.py`** (PLAN говорил «в server.py»): так тикет 006 добавит `list_tools` в тот же класс без конфликта с реестром в `server.py`. Пока там только override `call_tool`; `create_server` возвращает `GuardedMCPServer`. PLAN (разделы 3, ADR-012, «Спайк 002») обновлён.
- **Маппинг ошибок аргументов** — `interface/mcp/errors.py::invalid_arguments_result`: `code=invalid_params`, `details={field, reason}` (первая ошибка: loc и pydantic type), `message` перечисляет все ошибки. Отличие от спайка: `msg` pydantic выводится только для **allowlist** встроенных типов, чьё сообщение описывает правило, а не значение (`missing`, `literal_error`, `string_pattern_mismatch`, `*_parsing`, `less_than_equal`, …); любой другой тип (не только `value_error`) — только `loc: type`. Fail closed против эха значения (например, вставленного ключа). В лог идут только имена полей (`tool_invalid_arguments`). Остальные `ToolError` (неизвестный инструмент, `UnexpectedToolError`) — как в SDK.
- **`get_ticker` на настоящих типах:** `symbol: Field(pattern=...)`, `category: Literal["spot","linear"]`, `WithJsonSchema` убран. Следствие: `category="SPOT"` теперь отклоняется на границе MCP (`literal_error`), раньше домен принимал регистронезависимо; у use case (другие входы) нормализация прежняя. `symbol` в нижнем регистре по-прежнему проходит pattern и нормализуется доменом.
- **Ограничения объёма (FR-3), домен `market.py`:** стакан `depth` 1–50 (по умолчанию 10), свечи `limit` 1–200 (по умолчанию 50). Проверка дважды: схема MCP (`ge/le`) и домен (`orderbook_depth`/`candles_limit`, `bool` не считается числом); use case дополнительно обрезает ответ порта (`Orderbook.top`, `Candles.latest`), если адаптер вернул больше.
- **Интервалы свечей** — доменный `CandleInterval` (`1m`…`12h`, `1d`, `1w`, `1mo`; `1mo`, а не `1M`, чтобы регистронезависимый разбор не путал с минутой); коды Bybit (`1…720`, `D/W/M`) — только в адаптере (`KLINE_INTERVALS`). `Literal` в MCP и enum сверяет тест.
- **Формат ответа:** уровни стакана — пары `[price, qty]`, свечи — строки `[start_at, open, high, low, close, volume_base]` + поле `columns` (компактнее объектов в ~3 раза на 200 свечах); `turnover` в ответ не попадает (есть в домене). Свечи — от старых к новым (Bybit отдаёт наоборот).
- **Правила инструмента:** `InstrumentRules(instrument, status, tick_size, qty_step, min_qty, min_notional?, max_leverage?)` с инвариантами (шаги > 0, плечо ≥ 1). Spot: `qty_step = basePrecision`, `min_notional = minOrderAmt`, `min_qty = qty_step` (spot `minOrderQty` deprecated и игнорируется, реальный минимум — `min_notional`); `max_leverage = None`. Linear: `qtyStep`, `minOrderQty`, `minNotionalValue`, `leverageFilter.maxLeverage`. Статусы Bybit (`PreLaunch/PendingOpen/Trading/Delivering/Closed`) → доменный `InstrumentStatus`, неизвестный → `unknown` (не торгуется). Linear не `LinearPerpetual` → `invalid_params` (`reason=not_perpetual`): доменный `linear` = перпетуал. Коды монет проверяются `^[A-Z0-9]{1,20}$`, т.к. попадают в наше сообщение (`quoted in BTC`) — иначе `exchange_unavailable` (FR-41).
- **USDT only (FR-4):** доменная `require_supported_quote` → `invalid_params` с `details {field: symbol, reason: quote_not_supported, quote}` и подсказкой `<BASE>USDT`. Применяется в `GetInstrumentRules` (правила нужны для ордеров); `get_ticker/orderbook/candles` по не-USDT парам работают — чтение данных безвредно, и FR-1 его не запрещает.
- **Кэш правил** — `application/instrument_rules.py::InstrumentRulesCache` (порт `ExchangeClient` + `Clock`), TTL `GX_INSTRUMENT_RULES_TTL_S` (по умолчанию 600 с), ключ (symbol, category), ошибки не кэшируются, размер ограничен 4096 (LRU). Проверка USDT — после кэша, на каждом вызове. Без защиты от одновременных промахов (два запроса могут оба сходить на биржу — для чтения безвредно). Кэш на процесс: несколько процессов сервера держат свои копии.
- **Адаптер:** `orderbook` — `limit=depth`, время снимка — `result.ts`; несортированные/дублирующиеся уровни, чужой символ, нет `ts` → `exchange_unavailable`. `kline` — пустой список = пустая серия (новый инструмент), OHLC-инварианты проверяет домен. Неизвестный символ: orderbook/kline/linear instruments-info → 10001 → `invalid_params` с `untrusted_text`; spot instruments-info → пустой список → `not_found` (проверено на api-demo).
- Ручная проверка (не CI) против api-demo.bybit.com in-process: все четыре инструмента, `ETHBTC` → `quote_not_supported`, `NOPEUSDT` → 10001, `get_ticker` без `category` → `invalid_params`.
- `tests/fakes.py`: `FakeExchange` реализует новые методы порта, фабрики `make_orderbook/make_candles/make_rules`, `use_cases_on(exchange)` — сборка `UseCases` на фейке для e2e.

Ревью, раунд 1 (APPROVE с minor, закрыто 2026-10-28):
- Spot `min_qty = qty_step`, `minOrderQty` игнорируется (тест с `minOrderQty` > `basePrecision`).
- Нетрадуемые инструменты (`instruments-info` без `status` отдаёт только `Trading`/`PendingOpen`) — код не менялся, вопрос reduce-only закрытия записан в Scope тикета 011.
- Unit-тесты `GuardedMCPServer` (`tests/unit/test_guarded_server.py`): `ValidationError` из тела и `ToolError` из тела — результат SDK, не `invalid_params`.
- `details.field` и лог — только первый элемент `loc` (имя аргумента); в `message` вложенные строковые части `loc` (ключи словаря от клиента) заменяются на `*`, индексы остаются.
- `orderbook_depth`/`candles_limit` принимают только `int` (не `bool`, `float`, `Decimal`, `str`).
- `InstrumentRulesCache`: LRU (`move_to_end` при попадании), просроченная запись удаляется при чтении, даже если обновление упало.
- `InstrumentRulesOut.status` — `Literal` значений `InstrumentStatus` (тест сверяет с enum и `outputSchema`); в PLAN раздел 3 — почему кэш в application.

Для тикета 006: `GuardedMCPServer` в `interface/mcp/guarded_server.py` — добавить туда override `list_tools`; `create_server` уже возвращает этот класс.

Предложения (вне scope):
- `get_candles` с `start`/`end` (история дальше 200 свечей, пагинация по времени).
- В `InstrumentRules` добавить `max_qty`/`max_market_qty` (`maxOrderQty`/`maxMktOrderQty`, spot `maxLimitOrderQty`/`maxMarketOrderQty`) — пригодится валидации ордеров 011.
- Проверка USDT для `get_ticker`/`get_orderbook`/`get_candles`, если пользователь захочет строгую единообразность.
