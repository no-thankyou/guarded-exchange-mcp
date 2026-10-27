# 006: Политика: потолок, ужесточение, get_my_policy
Статус: TODO
Зависит от: 005
Требования: FR-11, FR-12, FR-18, FR-19 (поля), FR-23 (поля), FR-42 (поля), FR-44, FR-45

## Цель
Эффективная политика пользователя = strictest(потолок админа, настройки пользователя) по полям (D-23); CLI для админа и пользователя; агент читает свою политику через MCP `get_my_policy`; ослабление отклоняется.

## Scope
- Domain `policy.py`: `AdminCeiling`, `UserOverrides`, `EffectivePolicy`, `strictest`, `validate_override`; дефолты testnet/mainnet (PLAN раздел 6) и нового пользователя (FR-45).
- Таблица `policies`, репозиторий, use cases `ReadPolicy`, `SetCeiling` (admin), `TightenPolicy` (user); всё в журнал.
- CLI (typer): `policy show`, `admin policy set-ceiling`, `policy tighten`.
- MCP `get_my_policy` (только чтение, FR-44); проверка профиля в одном месте application (FR-11) — `permission_denied`.

## Non-scope
- Kill switch (015), mainnet-переключение (027), фильтрация tools/list (по итогам 002, отдельно).

## Acceptance criteria
- [ ] Unit (параметризованный): strictest по каждому полю — allowlist пересечение, AND/OR флагов, min чисел, объединение правил подтверждения.
- [ ] Unit: попытка пользователя ослабить любое поле → отказ с указанием поля; админ опустил потолок ниже настроек — действует потолок (FR-12).
- [ ] Unit: новый пользователь — read_only, пустой allowlist, testnet (FR-45); дефолты FR-18.
- [ ] E2E: `get_my_policy` возвращает эффективную политику; в MCP нет инструментов изменения политики (FR-44, проверка реестра).
- [ ] Integration: изменения политики пишутся в журнал.

## Заметки
—
