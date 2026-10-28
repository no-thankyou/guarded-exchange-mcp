# 008: Чтение аккаунта: балансы, позиции, открытые ордера
Статус: TODO
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
- [ ] Integration (MockTransport): маппинг wallet-balance, position/list, order/realtime в доменные объекты на фикстурах.
- [ ] Unit: внешний и системный ордер помечены верно.
- [ ] E2E: read-only пользователь читает аккаунт; без ключа — понятная ошибка с next_step.
- [ ] Unit: текстовые поля с биржи попадают только в `untrusted_text` (FR-41).

## Заметки
- Из 005: `lock_user` на SQLite — no-op (сериализацию даёт `BEGIN IMMEDIATE` write-UoW; вне write-UoW он падает). Значит, на SQLite «блокировка пользователя» = блокировка всей БД на время write-транзакции: держать их короткими, без сетевого I/O (D-10). Ветка Postgres (`FOR UPDATE`) — в 028.
- Из 007: ключ пользователя для чтения брать из `uow.credentials.get_active(user_id, EXCHANGE_BYBIT, settings.bybit_environment)` и расшифровывать `CredentialCipher.open(sealed, credential.binding)` (ошибка → `CredentialUnreadable`, код `exchange_credentials_invalid`); перепроверка прав для чтения не обязательна (D-24 — только перед изменяющими действиями), но истёкший/удалённый ключ — понятная ошибка. Подписанный GET уже есть в `BybitClient._get(..., keys=...)`: query строится один раз, подписывается и отправляется как есть.
