# Research: значения по умолчанию (slippage, лимит действий без подтверждения, rate limits)

Дата проверки всех фактов: **2026-10-27**. Биржа ещё не выбрана, поэтому биржевые данные даны для трёх кандидатов: Bybit V5, Binance (Spot и USDⓈ-M Futures), OKX V5.

## 1. Вопрос и затронутые требования

SPEC, раздел 9, пункт 6: обоснованные значения по умолчанию для трёх механизмов.

- **Допуск slippage для market-ордеров.** Требования: FR-13 (notional считается по худшей ожидаемой цене: лучшая цена встречной стороны плюс допуск), FR-21 (человеку показывают оценку и худшую допустимую цену), FR-48 (market исполняется не хуже показанной худшей цены, неисполненный остаток не исполняется), FR-18 (mainnet строже), NFR-5 (вторая биржа не должна менять правила).
- **Лимит торговых действий без подтверждения за скользящее окно.** Требования: FR-46 (защита от дробления), FR-19 (порог «подтверждать дороже X»), FR-26 (несколько клиентов одного пользователя одновременно).
- **Rate limits на пользователя и согласование с лимитами биржи.** Требования: FR-34, FR-35, FR-28 (ошибка «превышен rate limit» с «повторите через N»), NFR-4 (p95 накладных расходов ≤ 50 мс), NFR-1 (fail closed).

## 2. Факты с источниками

### 2.1 Slippage и защита цены market-ордеров

**Bybit V5**
- Bybit превращает market-ордер в IOC limit-ордер: «Bybit trading engine will convert the market order into an IOC limit order for matching». Есть необязательные параметры `slippageToleranceType` (`TickSize` или `Percent`) и `slippageTolerance`. В режиме Percent максимальная цена покупки = ask1 × (1 + tolerance × 0.01), минимальная цена продажи = bid1 × (1 − tolerance × 0.01). Допустимые значения: Percent от 0.01 до 10, TickSize от 1 до 10000. Для TP/SL и условных ордеров не поддерживается. Если в пределах допуска нет ликвидности, ордер не исполняется. Значение по умолчанию в API не указано. Источник: [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order), проверено 2026-10-27.
- Changelog: `slippageToleranceType` добавлен 2025-02-27. 2025-10-09 диапазоны расширены: Percent с 0.05–1 до 0.01–10, TickSize с 5–2000 до 1–10000. 2025-07-31 добавлен эндпоинт «Set Limit Price Behaviour»: что делать, если цена limit-ордера выходит за ценовую границу (отклонить или подвинуть). Источник: [V5 changelog](https://bybit-exchange.github.io/docs/changelog/v5), 2026-10-27.
- В help center сказано, что в UI market-ордер с допуском slippage **выключен по умолчанию**, а функция доступна на Spot, Spot Margin и Futures. Источник: [Market Order with Slippage Tolerance](https://www.bybit.com/en/help-center/article/Market-Order-with-Slippage-Tolerance), 2026-10-27. Это сводка поисковой выдачи по локализованной копии статьи, полный текст я не читал.
- Ценовые границы (price limit) у деривативов строятся от mark/index price с параметрами X% и Y%. Ордер с ценой за границей не отклоняется, а **сдвигается к границе**. Источник: [Futures Trading Mechanism](https://www.bybit.com/en/help-center/article/Derivatives-Trading-Rules), 2026-10-27, по сводке поиска. В `GET /v5/market/instruments-info` есть поле `riskParameters.priceLimitRatioX/Y`. В примерах документации: linear 0.01/0.02, spot 0.005/0.01, pre-market 0.05/0.1. Формула для linear «changed in May 2026». Источник: [Get Instruments Info](https://bybit-exchange.github.io/docs/v5/market/instrument), 2026-10-27. Значения в примерах — иллюстрация, а не гарантия для конкретного инструмента.
- Лимиты объёма market-ордера: `lotSizeFilter.maxMktOrderQty` (linear), `maxMarketOrderQty` (spot). Spot-значения «adjusted bi-monthly». Источник тот же.

**Binance**
- Spot: у MARKET-ордера нет параметра slippage. Ценовые фильтры `PERCENT_PRICE` и `PERCENT_PRICE_BY_SIDE` отсчитываются от средней цены за `avgPriceMins`. В документации не сказано, как они применяются к MARKET-ордерам. Источник: [Spot Filters](https://developers.binance.com/docs/binance-spot-api-docs/filters), 2026-10-27. Живой `exchangeInfo` для BTCUSDT 2026-10-27 вернул `PERCENT_PRICE_BY_SIDE`: bid 1.2 / 0.5, ask 2 / 0.8, avgPriceMins 5 (широкий коридор), `MAX_NUM_ORDERS` 200, `NOTIONAL` min 5. Источник: [api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT](https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT).
- USDⓈ-M Futures: живой `exchangeInfo` 2026-10-27 для BTCUSDT вернул `PERCENT_PRICE` multiplierUp 1.03 / multiplierDown 0.97, `marketTakeBound` "0.03", `triggerProtect` "0.0300". Источник: [fapi.binance.com/fapi/v1/exchangeInfo](https://fapi.binance.com/fapi/v1/exchangeInfo). **Официального определения `marketTakeBound` я не нашёл.** По названию это граница исполнения market-ордера (3%), но это не подтверждено.
- Futures New Order: `priceMatch` (OPPONENT, OPPONENT_5/10/20, QUEUE…) работает только для LIMIT/STOP/TAKE_PROFIT и не совместим с `price`. `priceProtect` есть только у STOP_MARKET/TAKE_PROFIT_MARKET (algo). `timeInForce`: GTC, IOC, FOK, GTX, GTD, RPI. `newClientOrderId` до 36 символов. Источник: [USDⓈ-M New Order](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api), 2026-10-27. Итого: нативного параметра «slippage tolerance» для market у Binance нет.

**OKX V5**
- Place order: для SPOT/MARGIN market исполняется «по рынку», проходя стакан. Для Expiry/Perpetual market выставляется по самой агрессивной цене, которую допускает Price Limit Mechanism. Для риск-проверок OKX дополнительно резервирует 5% котируемой валюты. Есть `slippagePct` для spot market: диапазон 0–0.05, по умолчанию 0.00% (по формулировке не ясно, 0.05 — это доля, то есть 5%, или проценты). Есть `pxAmendType`: 0 (по умолчанию) — не двигать цену за price limit, 1 — двигать. `clOrdId` до 32 символов. Источник: [OKX API v5 docs, Place order](https://www.okx.com/docs-v5/en/), 2026-10-27.
- Ценовые границы в Get instruments: `floatPxLmtPct` (пример 0.03), `initPxLmtPct` (пример 0.05, первые 10 минут после листинга), `maxPxLmtPct` (пример 0.15, жёсткий потолок отклонения от index). Источник тот же.

**Терминалы и боты**
- ccxt (адаптер Hyperliquid, где market эмулируется limit-ордером): `'defaultSlippage': 0.05`, то есть 5%. Цена: buy = price × (1 + slippage), sell = price × (1 − slippage), затем `price_to_precision`. Источник: [ccxt/python/ccxt/hyperliquid.py](https://raw.githubusercontent.com/ccxt/ccxt/master/python/ccxt/hyperliquid.py), master на 2026-10-27.
- freqtrade: в dry-run market исполняется «with a maximum slippage of 5%», `custom_price_max_distance_ratio` по умолчанию 0.02 (2%). Источник: [freqtrade Configuration](https://www.freqtrade.io/en/stable/configuration/), 2026-10-27.
- Hummingbot amm_arb: `market_1_slippage_buffer` по умолчанию 1, `market_2_slippage_buffer` 0. Единицы на странице не указаны, по контексту это проценты. Источник: [amm arbitrage](https://hummingbot.org/strategies/v1-strategies/amm-arbitrage/), 2026-10-27. Spot-perpetual arbitrage: по сводке поиска 0.05 ([docs](https://hummingbot.org/strategies/spot-perpetual-arbitrage)), не перепроверено.
- FIA (2024), раздел 1.3 «Price Tolerance»: ограничение отклонения цены ордера от референсной цены. Проверяется в торговом приложении до отправки на биржу и при каждом изменении ордера. Конкретных чисел нет. Источник: [FIA, Best Practices For Automated Trading Risk Controls And System Safeguards, July 2024](https://fia.org/sites/default/files/2024-07/FIA_WP_AUTOMATED%20TRADING%20RISK%20CONTROLS_FINAL_0.pdf), стр. 6, 2026-10-27.

**Testnet и демо**
- Bybit Demo Trading (`api-demo.bybit.com`): «public data is identical to that found on mainnet», лимит «Default rate limit, not upgradable», WS Trade не поддерживается. Testnet (`api-testnet.bybit.com`) — отдельная инфраструктура. Источник: [Demo Trading Service](https://bybit-exchange.github.io/docs/v5/demo), 2026-10-27.
- Официальных данных о глубине стаканов testnet я не нашёл. Утверждение «на testnet стаканы тонкие» — гипотеза, её нужно проверить вручную на выбранной бирже (NFR-2, ручной набор).

### 2.2 Аналоги лимита действий без подтверждения (FR-46)

- **SEC Rule 15c3-5(c)(1)(ii)** требует «Prevent the entry of erroneous orders, by rejecting orders that exceed appropriate price or size parameters, on an order-by-order basis or over a short period of time, or that indicate duplicative orders». Значит, регулятор явно признаёт, что per-order лимиты нужно дополнять лимитами на коротком окне и детекцией дублей. Источник: [17 CFR 240.15c3-5, LII](https://www.law.cornell.edu/cfr/text/17/240.15c3-5), 2026-10-27.
- **FIA 2024, раздел 3.2 «Repeated Automated Execution Limits»:** «the maximum number of times a strategy or identical order is filled and then re-enters the market without human intervention. After a configurable number of repeated executions, the strategy should be disabled until an authorized person re-enables it». Значение должен задавать сам трейдер, оно зависит от стратегии. Чисел FIA не даёт. Раздел 3.4 «Message Throttles»: «a message throttle should never reject an order cancellation request due to breached message rate limits». Источник: [FIA 2024 PDF](https://fia.org/sites/default/files/2024-07/FIA_WP_AUTOMATED%20TRADING%20RISK%20CONTROLS_FINAL_0.pdf), стр. 14–16.
- **MiFID II RTS 6 (Delegated Regulation (EU) 2017/589), ст. 15.** П. 1: price collars, maximum order values, maximum order volumes, maximum messages limits. П. 3: «repeated automated execution throttles» — после заданного числа исполнений система автоматически отключается до повторного включения назначенным сотрудником. П. 6: override только временно и с авторизацией. Источник: [legislation.gov.uk, retained version](https://www.legislation.gov.uk/eur/2017/589/article/15), 2026-10-27. В UK статья помечена как revoked (2023 c. 29). В ЕС регламент действует, но EUR-Lex с моей стороны не загрузился, поэтому текст сверен по UK-копии.
- **Брокерские API (порядок величин):** Alpaca Trading API — 200 запросов/мин на аккаунт, повышение до 1000/мин через support при переводе в non-retail. Источник: [Alpaca support](https://alpaca.markets/support/increase-api-rate-limit) (1000/мин и условия, официально). Цифра 200/мин взята только из [форума Alpaca](https://forum.alpaca.markets/t/is-there-a-way-to-increase-the-200-min-api-call-limit-for-the-trading-endpoint/18110), то есть подтверждена неофициально.
- Биржевые лимиты частоты ордеров (п. 2.3) на 2–3 порядка выше любого разумного лимита «без подтверждения». Они защищают инфраструктуру биржи, а не деньги пользователя, поэтому для FR-46 это не ориентир.

### 2.3 Rate limits бирж

| | Bybit V5 | Binance Spot | Binance USDⓈ-M | OKX V5 |
|---|---|---|---|---|
| Per IP | 600 запросов / 5 с на хост; при «403, access too frequent» ждать ≥ 10 мин | REQUEST_WEIGHT 6000 / 1 мин, RAW_REQUESTS 300000 / 5 мин | REQUEST_WEIGHT 2400 / 1 мин | Публичные эндпоинты — по IP |
| Per аккаунт: создание ордеров | create 10/10/20 в секунду (скользящая секунда, по колонкам таблицы; соответствие колонок spot/linear по выгрузке однозначно не определено) | ORDERS 100 / 10 с и 200000 / сутки (unfilled order count, исполненные ордера уменьшают счётчик) | ORDERS 300 / 10 с и 1200 / мин | Place order 60 / 2 с на UID + Instrument ID; общий лимит сабаккаунта 1000 order requests / 2 с |
| Amend / cancel | amend 10/с, cancel 10–20/с, cancel-all 1–20/с по категориям | — | — | — (не извлечено) |
| Чтение | positions, orders, executions, wallet balance — по 50/с на UID | по весу (IP) | по весу (IP) | balance 10 / 2 с, positions 10 / 2 с (UID) |
| Заголовки остатка | `X-Bapi-Limit-Status`, `X-Bapi-Limit`, `X-Bapi-Limit-Reset-Timestamp` | `X-MBX-USED-WEIGHT-1M`, `X-MBX-ORDER-COUNT-10S/1D`, `Retry-After` на 429/418 | `X-MBX-USED-WEIGHT-1M`, `X-MBX-ORDER-COUNT-10S/1M` | Не нашёл в официальной документации |
| Ошибка лимита | retCode 10006 «Too many visits!» | 429 (-1015 «Too many new orders»), затем 418 IP-ban от 2 мин до 3 суток | 429, затем 418, бан от 2 мин до 3 суток | 50011 (rate limit), 50061 (лимит сабаккаунта) |

Источники (все проверены 2026-10-27): [Bybit Rate Limit](https://bybit-exchange.github.io/docs/v5/rate-limit). [Binance Spot limits](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits) и [Unfilled Order Count Rules](https://developers.binance.com/docs/binance-spot-api-docs/faqs/order_count_decrement); числа взяты из живого `exchangeInfo` (spot и futures, см. 2.1). [Binance USDⓈ-M General Info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info). [OKX v5 docs](https://www.okx.com/docs-v5/en/). Bybit changelog 2026-05-21: лимит Get Transaction Log снижен с 50 до 25 req/s, то есть **лимиты меняются**, и хардкодить их рискованно.

Ключевые свойства лимитов:
- Binance и Bybit считают **IP-лимиты по IP**, а не по ключу. Binance: «The limits on the API are based on the IPs, not the API keys». В удалённом режиме все tenants выходят на биржу с одного egress IP сервера, поэтому один зациклившийся пользователь может забанить IP для всех. Это главный аргумент за глобальный бакет сервера (FR-34).
- Лимиты на ордера считаются **по аккаунту/UID**. Bybit per-UID, Binance «counted against each account», OKX UID + инструмент.
- Bybit testnet: отдельных цифр лимитов не нашёл. Bybit demo: «Default rate limit, not upgradable». Binance futures testnet (`demo-fapi.binance.com`): лимиты не указаны.

### 2.4 Сигнализация «повторите через N»
- IETF `draft-ietf-httpapi-ratelimit-headers-11` (23.05.2026, Active Internet-Draft, Standards Track, ещё не RFC) определяет поля `RateLimit-Policy` и `RateLimit`. Источник: [IETF datatracker](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/), 2026-10-27. Для MCP это касается только HTTP-транспорта. Агенту «retry after N» нужно отдавать в теле результата инструмента (FR-28).

## 3. Варианты и trade-offs

### 3.1 Как реализовать границу цены market-ордера (FR-48)

**A. Limit IOC по худшей цене, вычисленной сервером.** Сервер берёт лучшую цену встречной стороны P, считает границу buy = P × (1 + s), sell = P × (1 − s) и округляет к шагу цены в безопасную сторону: buy вниз, sell вверх. Эта граница показывается человеку (FR-21) и по ней же считается notional (FR-13). На биржу уходит limit-ордер с `timeInForce=IOC` (Bybit `timeInForce=IOC`, Binance `LIMIT` + `IOC`, OKX `ordType=ioc`).
- Плюсы: граница **буквально равна** показанной цене, то есть FR-22 и FR-48 выполняются по построению. Работает одинаково на всех трёх биржах (NFR-5). Остаток автоматически отменяется (требование FR-48). Количество задаётся в base-валюте на всех биржах, так что не нужно разбираться с `marketUnit`/`tgtCcy`/`quoteOrderQty` у spot market buy. Тесты без сети просты: граница — чистая функция.
- Минусы: ордер на бирже будет limit, а не market. Для журнала нужно хранить «intent=market, wire=limit IOC». Если P устарел (после подтверждения прошло до 2 минут, FR-23), ордер может не исполниться вообще — это корректный исход FR-48, но UX хуже. Ценовые коридоры биржи могут отклонить или подвинуть limit за границей: Binance futures PERCENT_PRICE ±3% для BTCUSDT, Bybit price limit со сдвигом, OKX `pxAmendType=0` отклоняет. Поэтому потолок s должен быть меньше коридора биржи.

**B. Нативный параметр биржи** (Bybit `slippageTolerance` Percent, OKX `slippagePct` только spot).
- Плюсы: настоящий market-ордер, биржа сама режет исполнение.
- Минусы: **граница считается от ask1/bid1 на момент матчинга**, а не от цены, показанной человеку. Если цена ушла, биржа исполнит хуже показанной границы, и FR-48 нарушается. Binance такого параметра не имеет, OKX — только для spot. Поведение зависит от биржи (против NFR-5), граничные значения диапазонов уже менялись (changelog Bybit 2025-10-09).

**C. Гибрид:** limit IOC (A) плюс нативный параметр как вторая линия. Это лишняя сложность без выигрыша: граница A всегда строже или равна.

**D. Пересчитывать оценку на момент исполнения.** Это противоречит FR-22/FR-48: исполняется то, что показано. Отклонено.

### 3.2 Значение допуска s

Ориентиры: ccxt и freqtrade (dry-run) используют 5% как «сеть безопасности» для ботов. Hummingbot использует буфер около 1%. Коридоры бирж: Binance futures ±3% (BTCUSDT), OKX `floatPxLmtPct` около 3%, Bybit `priceLimitRatioX/Y` 0.5–2% (примеры). Значения 5% и выше на mainnet бессмысленны: они шире биржевых коридоров и не защищают пользователя. Для ордеров ≤ 50 USDT (потолок mainnet FR-18) на ликвидных USDT-парах объём укладывается в первый уровень стакана, так что основной источник отклонения — **движение цены за время ожидания подтверждения** (до 2 минут), а не глубина стакана. Данных о типичном движении за 2 минуты в официальных источниках нет, это нужно измерить.

### 3.3 Лимит действий без подтверждения (FR-46)

- **Что считать.** Вариант 1: все торговые действия без подтверждения, как в буквальном тексте FR-46. Вариант 2: только увеличивающие риск (FR-43). Дробление обходит лимиты именно увеличивающих риск действий. Если заставить подтверждать отмены после серии, это мешает «паническому» сценарию, и это прямо противоречит FIA 3.4 («never reject an order cancellation»). При этом FR-46 требует не отказа, а подтверждения, поэтому вред варианта 1 меньше. Рекомендация — вариант 2, но это **уточнение формулировки SPEC и требует согласия пользователя**.
- **Окно и алгоритм.** Значения маленькие (единицы–десятки), поэтому подходит точный sliding window log: список временных меток на пользователя, хранить N штук. Token bucket здесь хуже: он разрешает burst до capacity и «прощает» постепенно, а нам нужна понятная человеку формулировка «не больше N за M минут». Слот резервируется **атомарно в момент решения политики**, а не после ответа биржи, иначе два клиента одного пользователя (FR-26) одновременно пройдут проверку. Неудачные и отклонённые биржей действия слот не возвращают (fail closed). Подтверждённые человеком действия в счётчик не идут.
- **Связь с суммой.** Эффективный лимит неподтверждённого notional за окно = N × min(per-order лимит, порог X из FR-19). Это удобно объяснять в README.

### 3.4 Rate limits (FR-34/FR-35)

- **Алгоритм для лимитов MCP-сервера:** token bucket (или эквивалентный GCRA). Он разрешает естественные всплески агента (например, 5 read-вызовов подряд при старте), занимает O(1) памяти на ключ, и из него точно вычисляется «retry after N» (время до следующего токена). Sliding window log точнее, но хранит метки на каждый запрос. Fixed window даёт двойной burst на границе окна.
- **Раздельные бакеты:** read и write на пользователя. Write — каждый вызов изменяющего инструмента, включая создание pending-подтверждения: это защищает человека от «confirmation spam», когда зациклившийся агент заваливает его запросами подтверждения. Отмены по FIA 3.4 не должны отказываться из-за throttle, поэтому они не тратят пользовательский write-бакет, но проходят через биржевой guard.
- **Слои защиты биржи (FR-35):**
  - (1) Глобальный бакет на egress IP сервера, общий для всех tenants: защищает от IP-бана Binance (418) и Bybit (403, 10 минут).
  - (2) Бакет на биржевой аккаунт (UID) по категориям: create / amend / cancel / read.
  - (3) Адаптивная коррекция по заголовкам (`X-Bapi-Limit-Status`, `X-MBX-*`), где они есть.
  - (4) На 429 / 10006 / 50011 — circuit breaker на «Retry-After» (или до reset-timestamp) с отказом «повторите через N». Никаких автоматических повторов изменяющих запросов (FR-27).
- **Где хранить:** в v1 один процесс, поэтому in-memory бакеты под `asyncio.Lock` укладываются в NFR-4 с огромным запасом (микросекунды). При нескольких инстансах понадобится общий store (Redis, Lua/атомарные операции). Это решение PLAN, а не дефолтов.

## 4. Рекомендация

### 4.1 Таблица предлагаемых значений по умолчанию

| Параметр | testnet (потолок админа по умолчанию) | mainnet (потолок админа по умолчанию) | Жёсткий максимум админа | Обоснование |
|---|---|---|---|---|
| Допуск slippage s для market (FR-13/FR-48) | **2%** | **0.5%** | 3% (ниже коридоров Binance futures ±3% / OKX ~3%) | ccxt и freqtrade используют 5% как сеть безопасности ботов; это шире биржевых коридоров и не защищает. Mainnet строже (FR-18). На testnet/демо ликвидность и поведение стакана не гарантированы, поэтому шире. Пользователь может только уменьшить (FR-12). Нижняя граница — 1 тик. |
| Реализация границы | limit IOC по худшей цене | limit IOC по худшей цене | — | Единственный вариант, где исполненная граница = показанной (FR-22/FR-48) и одинаковой на трёх биржах (NFR-5) |
| Округление границы | buy — вниз к тику, sell — вверх к тику; notional (FR-13) по округлённой границе | то же | — | Никогда не хуже показанного |
| Лимит действий без подтверждения (FR-46) | **20 за скользящие 10 мин** | **5 за скользящие 10 мин** | — (админ задаёт любое, пользователь только уменьшает) | FIA 3.2 и RTS 6 ст. 15(3): после N повторов нужен человек. 15c3-5: «over a short period of time». На mainnet по умолчанию и так подтверждается всё (FR-18/FR-19), лимит работает, только если админ ослабил подтверждения. 5 × 50 USDT = ≤ 250 USDT неподтверждённого notional за 10 мин |
| Что считается в FR-46 | действия, увеличивающие риск, выполненные без подтверждения | то же | — | Требует согласования формулировки SPEC (см. 3.3) |
| Rate limit read на пользователя (FR-34) | token bucket: capacity 20, refill 2/с (≈120/мин) | то же | настраивает админ | Агент делает единицы вызовов на шаг. Это на порядок ниже per-UID read-лимитов бирж (Bybit 50/с, OKX 10/2 с — самый строгий: 5/с) |
| Rate limit write на пользователя (включая создание pending) | capacity 5, refill 1 / 2 с (≈30/мин) | capacity 3, refill 1 / 5 с (≈12/мин) | настраивает админ | Человек физически не подтвердит больше. Защита от confirmation spam. Сильно ниже биржевых лимитов на ордера (Bybit 10/с, OKX 60/2 с, Binance 100/10 с) |
| Отмены (cancel, cancel-all) | не тратят пользовательский write-бакет | то же | — | FIA 3.4: throttle не должен отклонять отмены. Ограничены биржевым guard |
| Максимум одновременных pending-подтверждений на пользователя | 10 | 5 | — | Против усталости от подтверждений. Дополнение, в SPEC не требуется |
| Глобальный бакет на egress IP (FR-35) | 50% IP-лимита биржи | 50% IP-лимита биржи | — | Bybit 600/5 с → 60 req/s. Binance spot 6000 weight/мин → 3000, futures 2400 → 1200 (с учётом весов). Запас на чужие процессы с того же IP и на неточность учёта |
| Бакет на биржевой UID (FR-35) | 50% per-UID лимита по категории | 50% | — | Bybit create 10/с → 5/с. OKX place 60/2 с → 15/с. Binance orders 100/10 с → 5/с. Выше пользовательского write-бакета, так что срабатывает, только если несколько клиентов делят аккаунт |
| Реакция на лимит биржи | circuit open до `Retry-After` / reset timestamp; по умолчанию 10 с, если биржа не сказала; Bybit 403 → 10 мин | то же | — | Binance эскалирует 429 → 418 (до 3 суток). Изменяющие запросы не повторять (FR-27) |

### 4.2 Главные выводы
- **Граница цены — limit IOC по показанной худшей цене**, а не нативный slippage-параметр: у Bybit нативная граница считается от ask1 на момент матчинга, у Binance параметра нет, у OKX он только для spot.
- **Окно FR-46 — sliding window log с атомарным резервированием слота** в момент решения. Rate limits MCP — token bucket, раздельно read/write, плюс глобальный бакет на IP сервера и бакет на биржевой UID. Сами числа бирж держать в адаптере, а не в доменных правилах (NFR-5).

### 4.3 Риски
- При подтверждении через внешний канал (на mainnet по умолчанию, FR-42) и TTL 2 минуты граница 0.5% может часто приводить к «исполнено 0» на волатильном рынке. Это безопасный исход, но UX хуже. Митигировать нужно через понятное сообщение агенту, а не через расширение границы.
- Ценовые коридоры бирж могут **молча сдвинуть** limit-цену (Bybit price limit, OKX `pxAmendType=1`). Для buy сдвиг вниз нам не вредит, но поведение нужно проверить на testnet. На Bybit есть эндпоинт «Set Limit Price Behaviour», его стоит выставить в «reject», если адаптер так решит.
- Лимиты бирж меняются (Bybit снижал лимит в 2026-05). Их нужно читать из `exchangeInfo`/заголовков, где возможно, и хранить с датой проверки.
- В удалённом режиме один IP на всех tenants, поэтому без глобального бакета возможен IP-бан Binance для всех пользователей.

### 4.4 Что остаётся неизвестным
- Официальная семантика Binance futures `marketTakeBound` (по названию — коридор market-ордера 3%, не подтверждено).
- Единицы OKX `slippagePct` (0.05 — это доля или проценты) и OKX rate-limit заголовки: в документации я их не нашёл, вероятно, их нет.
- Глубина стаканов и лимиты testnet у всех трёх бирж: проверить вручную после выбора биржи. Если выбран Bybit, рассмотреть Demo Trading, у которого публичные данные совпадают с mainnet.
- Соответствие колонок таблицы per-UID лимитов Bybit категориям spot/linear (выгрузка неоднозначна): перепроверить на странице для выбранной категории.
- Реальное распределение движения цены за 0–2 минуты для калибровки 0.5%: можно оценить по свечам 1m через read-инструменты уже после скелета.
- Изменение формулировки FR-46 («считать только увеличивающие риск действия») — решение пользователя.
