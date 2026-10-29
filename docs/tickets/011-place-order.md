# 011: place_order насквозь (testnet, без HITL)
Статус: TODO
Зависит от: 006, 008, 009
Требования: FR-4, FR-10, FR-13, FR-14, FR-15, FR-17, FR-25 (заглушка), FR-36 (часть), FR-43 (PlaceOrder), FR-48 (граница цены), ADR-010

## Цель
Агент размещает market/limit ордер на споте и перпетуалах (reduce-only для перпетуалов) на testnet: правила инструмента, округление объёма вниз, notional по худшей цене, лимиты, allowlist, классификация риска, Limit IOC для market, журнал attempt/outcome. Подтверждение — заглушка: если политика требует подтверждения → `confirmation_channel_unavailable` (fail closed); «без подтверждения» — только явной настройкой админа.

## Scope
- Domain: `money.py` (округление: объём вниз, цена не по tick — ошибка, SPEC GP-1), `actions.PlaceOrder`, `risk.classify`, reduce-only clamp, `limits.check_limits` (FR-13, FR-14, FR-15).
- ADR-010: worst = best × (1 ± slippage), округление в безопасную сторону, `Limit IOC`; фактически исполненный объём — чтением после отправки (D-15).
- `submit_action.py` — единый путь: auth → rules → policy/limits → confirm-заглушка → execute; D-10 (без I/O в транзакции); `action_id` → `client_order_id = gx-…` (D-19).
- Журнал: отказ лимита (FR-17), attempt до отправки, outcome после; самоописание клиента по итогам 002 (FR-36).
- Торговые инструменты доступны только на testnet (в v1 — всегда, mainnet/027 — v2); ошибки ADR-013 для торговли.
- Правила инструмента для **нетрадуемых** инструментов (из ревью 009): `instruments-info` без `status` отдаёт только `Trading`/`PendingOpen` (linear), т.е. для делистингуемого/`Delivering`/`Closed` перпетуала `get_instrument_rules` сейчас даст `not_found`. Reduce-only закрытие позиции на таком инструменте (FR-43, FR-15) не должно упираться в `not_found`: решить здесь — запрашивать с явным `status` или без фильтра правил для снижающих риск действий; новые увеличивающие риск ордера по-прежнему только при `status=trading`.
- Денежные аргументы (`qty`, `price`, …) публикуются строкой с pattern по образцу `DecimalStr` (а не дефолтным `anyOf: [number, string]` для `Decimal`), чтобы модель не слала float (D-4, спайк 002). Самоописание клиента для FR-36 — `ctx.session.client_params.client_info` (может быть `None`).

## Non-scope
- Идемпотентность и неизвестный исход (012), другие действия (013, 014), kill switch (015).

## Acceptance criteria
- [ ] Unit (именованные edge cases): лимит проходит до округления и не проходит после (и наоборот); limit-цена не по tick → ошибка с ближайшими значениями.
- [ ] Unit: market notional считается по худшей цене; slippage > жёсткого максимума 3% отклоняется.
- [ ] Unit: инструмент вне allowlist → отказ; reduce-only по такому инструменту при наличии позиции → разрешён (FR-15).
- [ ] Unit: reduce-only больше позиции урезается до позиции (FR-43).
- [ ] Unit: политика требует подтверждения → `confirmation_channel_unavailable`, на биржу ничего не ушло.
- [ ] Integration: отправляется `Limit IOC` с ценой границы и `orderLinkId=gx-…`; `filled: unknown`, если исполнение ещё не видно.
- [ ] Integration: отказ и попытка/исход в журнале в правильных транзакциях.
- [ ] E2E: `inputSchema` `place_order` описывает `qty`/`price` как строки с pattern; JSON-число вместо строки → `invalid_params`.

## Заметки
- Из 008: префикс `orderLinkId` системы — одна константа `CLIENT_ORDER_ID_PREFIX = "gx-"` в `infrastructure/bybit/mapper.py`, по ней `order_origin` помечает ордера `system/external`; формат `client_order_id` (D-19, ≤ 36 символов Bybit) строить от неё. Сверка `system` с журналом отправленных ордеров (FR-2: префикс — только быстрый фильтр) — в 011/012, когда такие записи появятся.
- Из спайка 004 (2026-10-28, demo): на perp (по умолчанию, `lpaPerp=false`) биржа **молча сдвигает цену любого limit-ордера**, выходящего за коридор `GET /v5/market/price-limit`, к границе коридора (`linear_create_over_buy_limit_default*`); на spot — отказ `170193`, в режиме reject на perp — `110121`. Решить в тикете: (1) обычный limit вне коридора — ошибка до отправки (как SPEC GP-1, «цена — ошибка») или сдвиг с явным сообщением агенту; (2) для market (Limit IOC, ADR-010) — ограничение коридором на стороне сервера. В любом случае цена, показанная в FR-21 и записанная в журнал, должна совпадать с ценой ордера на бирже (читать `price` из ордера после отправки).
- Из 007: перед любым изменяющим действием вызывать `application.credentials.RequireTradingCredential(user_id)` — он перепроверяет права ключа, если последняя проверка старше 1 ч (D-24), отказывает `credentials_permissions_changed` / `exchange_credentials_invalid` / `permission_denied` (read-only ключ), при невозможности перепроверить устаревший ключ — `exchange_unavailable`, а если перепроверку отбил rate limit биржи — `rate_limited` с `retry_after_s` (оба fail closed, `details.reason=permissions_recheck_failed`), и возвращает расшифрованный `ApiKeyPair` для адаптера. Подписанные запросы — `infrastructure/bybit/signing.signed_headers`; для POST подписывать ровно отправляемое тело (`json.dumps(..., separators=(",", ":"))`), вектор POST уже есть в `tests/fixtures/bybit/signature_vectors.json`. Хук не закрывает гонку целиком: между ним и отправкой ордера `keys replace/delete` может успеть — финальная сверка (ключ всё ещё `active`, тот же `credential_id`) под `lock_user` в той же write-транзакции, где пишется `attempt` (D-8, D-10: без сетевого I/O внутри); ключ с `status != active` хук уже не отдаёт (`key_replaced_or_deleted`).
