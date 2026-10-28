# 008: Чтение аккаунта: балансы, позиции, открытые ордера
Статус: IN_REVIEW
Зависит от: 007
Требования: FR-2 (часть), FR-3, FR-41

## Цель
Агент читает балансы, позиции и открытые ордера своего аккаунта; ордера помечены происхождением (через систему / внешний), свободный текст — `untrusted_text`.

## Scope
- MCP: `get_balances`, `get_positions`, `get_open_orders`; use cases с `user_id`-scoped доступом к credential.
- Происхождение: префикс `gx-` в `orderLinkId` (быстрый фильтр) + сверка с журналом там, где есть.
- LLM-friendly ответы: единицы, без лишних полей, лимит/пагинация (FR-3); hedge mode → `unsupported_position_mode` (D-26).
- Ошибки: `exchange_credentials_invalid`, `rate_limited` (по ответу биржи), `exchange_unavailable`.

## Non-scope
- История (010), торговля.

## Acceptance criteria
- [x] Integration (MockTransport): маппинг wallet-balance, position/list, order/realtime в доменные объекты на фикстурах.
- [x] Unit: внешний и системный ордер помечены верно.
- [x] E2E: read-only пользователь читает аккаунт; без ключа — понятная ошибка с next_step.
- [x] Unit: текстовые поля с биржи попадают только в `untrusted_text` (FR-41).

## Заметки
- Из 005: `lock_user` на SQLite — no-op (сериализацию даёт `BEGIN IMMEDIATE` write-UoW; вне write-UoW он падает). Значит, на SQLite «блокировка пользователя» = блокировка всей БД на время write-транзакции: держать их короткими, без сетевого I/O (D-10). Ветка Postgres (`FOR UPDATE`) — в 028.
- Из 007: ключ пользователя для чтения брать из `uow.credentials.get_active(user_id, EXCHANGE_BYBIT, settings.bybit_environment)` и расшифровывать `CredentialCipher.open(sealed, credential.binding)` (ошибка → `CredentialUnreadable`, код `exchange_credentials_invalid`); перепроверка прав для чтения не обязательна (D-24 — только перед изменяющими действиями), но истёкший/удалённый ключ — понятная ошибка. Подписанный GET уже есть в `BybitClient._get(..., keys=...)`: query строится один раз, подписывается и отправляется как есть.

Решения и отклонения (2026-10-28, исполнитель):
- **Порт `AccountReader`** (`application/ports.py`) отдельно от `ExchangeClient`: ключ — аргумент каждого метода (`get_balances(keys)`, `get_positions(keys, symbol=, limit=)`, `get_open_orders(keys, category, symbol=, limit=)`), как у `KeyPermissionsReader`. Реализует тот же `BybitClient`. Так публичный `ExchangeClient` не знает о ключах, а `FakeAccount` подменяет только приватную часть.
- **Ключ для чтения — `application.credentials.ReadCredential(user_id)`**: одна read-транзакция (D-10), затем запрос к бирже. Принципал не найден/отозван/`None` → тот же `permission_denied`, что у `AuthorizeAccess` (FR-33); пользователь другой среды → `permission_denied` (`environment_mismatch`); нет активного ключа → `exchange_credentials_invalid` (`no_key`, next_step «`guarded-exchange keys add`, outside the chat»); истёк → `expired`; удалён между чтениями → `key_replaced_or_deleted`; не расшифровывается → `CredentialUnreadable`. Домен — `require_reading_allowed` (активен и не истёк) и `no_key()` (теперь его же использует `RequireTradingCredential`). Права не перепроверяются (D-24): ключ с расширившимися правами торговать не может, но читать — да (тест `test_grown_permissions_block_trading_but_not_reading`); ключ на attest без проверенных прав тоже читает.
- **Домен `domain/account.py`:** `AccountBalances`/`CoinBalance`, `Position` (только linear, `size > 0`, `PositionSide long|short`), `Order` (+ enum `OrderSide/OrderType/OrderStatus/TimeInForce/ConditionalKind/OrderOrigin`), `Page[T](items, has_more)`, `UntrustedText`. Ошибки `RateLimited` (`rate_limited`) и `UnsupportedPositionMode` (`unsupported_position_mode`) — в `domain/errors.py`. `market._bounded` стал публичным `bounded_count` (тот же код для `limit` аккаунта).
- **Происхождение:** адаптер (D-26: формат id — его) — `mapper.order_origin`: `system`, если `orderLinkId` начинается ровно с `gx-` (регистр и позиция важны), иначе `external`; пустой id — `external`. **Сверки с журналом нет**: в журнале пока нет записей об отправленных ордерах (появятся в 011/012) — «там, где есть» = пока нигде. Ограничение явное (README, docstring `CLIENT_ORDER_ID_PREFIX`): префикс может поставить кто угодно с доступом к аккаунту. Позиции происхождением не помечены: позиция — сумма исполнений из любых источников, честной метки у неё нет (FR-2 для позиций — предложение ниже).
- **FR-41:** `orderLinkId` → `Order.client_order_id: UntrustedText` → в ответе только `orders[].untrusted_text.client_order_id` (обрезка до 64 символов). Все остальные строки ответа — числа, время UTC, наши enum/Literal или идентификаторы, прошедшие строгий шаблон в адаптере (symbol `^[A-Z0-9]{2,30}$`, монета `^[A-Z0-9]{1,20}$`, `orderId` `^[A-Za-z0-9-]{1,64}$`); несовпадение → `exchange_unavailable`, а не проброс текста. Неизвестные значения enum биржи (`orderStatus`, `orderType`, `timeInForce`, `stopOrderType`) → `unknown`/`other`. Unit-тест обходит JSON всех трёх ответов и проверяет каждую строку вне `untrusted_text` регуляркой «число | время | enum | идентификатор».
- **FR-3:** `limit` 1–50 (по умолчанию 20) для позиций и ордеров — схема MCP + домен `account_items_limit`; use case дополнительно режет ответ порта. «Есть ли ещё» — **без курсора**: адаптер запрашивает `limit + 1` и ставит `has_more`, если лишняя строка пришла (у `order/realtime` максимум биржи 50: при `limit=50` — `has_more`, если страница полная и есть `nextPageCursor`). Подсказка агенту — сузить по `symbol`. Курсор Bybit не пробрасывали: он URL-кодирован и требует отправки «как есть» (подпись по сырому query) — без проверки на живой бирже это риск (предложение ниже). Балансы: до 50 монет по убыванию USD, остальные — счётчиком `omitted_coins`.
- **Запросы:** `wallet-balance?accountType=UNIFIED` (только UTA; per-coin `availableToWithdraw`/`free` устарели — доступное для маржи берём на уровне аккаунта `totalAvailableBalance`, USD); `position/list?category=linear` + `symbol` или `settleCoin=USDT` (без symbol биржа отдаёт только `size > 0`; с symbol — строку с `size 0`, она отфильтровывается); `order/realtime?category=…&openOnly=0` + `symbol`, для linear без symbol — `settleCoin=USDT`. Все GET с подписью через существующий `_get(..., keys=)`, ретраи только на 5xx/таймаут/10000/10016.
- **Hedge mode:** любая строка `position/list` (и любой linear-ордер) с `positionIdx != 0` → `unsupported_position_mode` для всего чтения: половина хеджированной книги, показанная как one-way, ввела бы в заблуждение.
- **Rate limit:** `10006` теперь `RateLimited` (было `exchange_unavailable`) — для всех GET, включая рыночные; `retry_after_s` = `X-Bapi-Limit-Reset-Timestamp − now`, ограничено 0…600 с, без заголовка — 1 с. HTTP 403 (IP-лимит) оставлен `exchange_unavailable` — бакеты и breaker в 022.
- **Фикстуры:** `wallet-balance` и пустой `position/list` — записи demo из спайка 004; открытые позиции, hedge mode и открытые ордера (linear/spot) **построены офлайн** по документации Bybit и полям записей спайка (`tests/fixtures/bybit/account/*.json`, поле `_source` в каждом файле). Ключ пользователя не читался, на биржу не ходили.
- **Composition root:** `app_server(..., cipher=...)` и `build_use_cases(..., cipher)` — шифр для ключей нужен уже серверу MCP (раньше только CLI); `serve` передаёт `AesGcmCredentialCipher(keyring)` — master key и так грузится до старта.
- Миграций нет (новых таблиц не нужно).

Как потрогать руками: `uv run pytest tests/e2e/test_mcp_account_reads.py -v` (весь стек на MockTransport: AES-GCM, подпись ключом пользователя, outputSchema). На demo (не CI): `uv run guarded-exchange keys add` (ключ с Read-Only или Contract Orders/Positions + Spot Trade), затем в Claude Code / через stdio-клиент — «покажи балансы», «открытые позиции», «открытые ордера на linear»; ордер, выставленный руками в UI Bybit, придёт с `origin: external` и id в `untrusted_text`. Без ключа — `exchange_credentials_invalid` с подсказкой `keys add`.

Предложения (вне scope):
- Курсорная пагинация (`cursor` → `nextPageCursor`), после ручной проверки на demo, что курсор надо отправлять без повторного URL-кодирования и как он подписывается.
- Происхождение позиции (FR-2 говорит «ордера и позиции»): например, «открыта/увеличена ордером системы» по журналу исполнений — после 011/012; сейчас поле не выводится.
- Более строгий шаблон `orderId` (UUID для linear, цифры для spot) — сейчас любой `[A-Za-z0-9-]{1,64}`; id назначает биржа, не пользователь, поэтому риск FR-41 низкий.
- `get_open_orders` без `category` (обе категории одним вызовом) — два запроса к бирже и слияние; решить по evals.
- Классические (не UTA) аккаунты: `accountType=CONTRACT/SPOT` — сейчас только `UNIFIED`, иначе ошибка биржи.
