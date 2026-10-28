# 007: Ключи биржи: шифрование, CLI, проверка прав
Статус: TODO
Зависит от: 004, 005
Требования: FR-30, FR-31, FR-49, FR-29 (часть: soft-delete)

## Цель
Пользователь через CLI добавляет, заменяет и удаляет ключ биржи; ключ хранится зашифрованным (AES-256-GCM, AAD, HKDF); права проверяются по allowlist (D-24), с веткой FR-49 по результату спайка 004.

## Scope
- `infrastructure/crypto.py`: keyring, HKDF-подключи (`credentials`, `audit`, `request_state`, `telegram`, D-14), AAD `credential_id‖user_id‖exchange`.
- Таблица `exchange_credentials` с environment, permissions, `permissions_checked_at`, `admin_attested`, status active/deleted (D-12, D-9).
- Use case `AddCredential`/`ReplaceCredential`/`DeleteCredential` (soft-delete); проверка прав: allowlist, `readOnly` → профиль чтения, `expiredAt`, предупреждение без IP whitelist.
- Ветка «права проверить нельзя»: testnet — только с `admin attest` в CLI (журнал), mainnet — отказ (SPEC FR-49, GP-3).
- Перепроверка прав: при старте и перед изменяющим действием, если старше 1 ч (хук для 011); расширение прав → `credentials_permissions_changed`.
- Подписанные запросы Bybit (HMAC-SHA256) в адаптере; ввод ключа в CLI без эха; секреты не попадают в логи/журнал.
- Allowlist адресов биржи (решение пользователя 2026-10-28, из ревью 001): `bybit_base_url` — только известные хосты Bybit по https (mainnet, demo, testnet); произвольный хост или http — только с явным тестовым флагом конфига. Иначе `ConfigError`, fail closed до первого подписанного запроса.
- Проверка master key при старте `serve` (перенесено из ревью 005, m5): `FileMasterKeyStore.exists()` уже отвергает права шире 0600, симлинк и битый ключ; `serve` должен загружать ключ до старта MCP и fail closed.

## Non-scope
- Чтение аккаунта (008).

## Acceptance criteria
- [ ] Unit: ключ с любым правом вне allowlist (включая неизвестное) отклоняется; read-only ключ принимается и ограничивает профиль.
- [ ] Unit: недоступная проверка прав → testnet без attest отказ, с attest принят и записан в журнал; mainnet — отказ всегда.
- [ ] Integration: шифротекст, скопированный в строку другого пользователя, не расшифровывается (AAD).
- [ ] Integration: подпись запроса совпадает с эталоном (фикстура/pybit-вектор).
- [ ] Integration: секрет не встречается в логах и журнале (capture логов + поиск по БД).
- [ ] Unit: перепроверка через час видит расширенные права → изменяющие действия блокируются.
- [ ] Integration: `serve` при отсутствующем master key / ключе с правами шире 0600 / битом ключе не стартует: exit 2, понятная причина в stderr, stdout пуст.
- [ ] Unit: `bybit_base_url` вне allowlist или по http без тестового флага → `ConfigError` (exit 2); известные хосты Bybit принимаются; с тестовым флагом loopback-адрес разрешён.

## Заметки
—
