# 024: Журнал: HMAC-цепочка и audit verify
Статус: DEFERRED (v2, решение пользователя 2026-10-29, MVP)
Зависит от: 017
Требования: FR-36, FR-37, FR-50

## Цель
Журнал становится tamper-evident: per-user HMAC-цепочка, блокировка `audit_heads`, команда `audit verify` находит изменение, удаление и вставку в обход системы.

## Scope
- ADR-008: canonical JSON, `prev_mac`, `mac`, `key_version`; миграция существующих записей в цепочку; D-13.
- CLI `audit verify` — отчёт о первом расхождении; триггеры переживают batch-миграции (D-11).
- Полнота FR-36: все торговые и административные события, канал подтверждения, ответ биржи, без секретов.

## Non-scope
- Checkpoints и чтение через MCP (025).

## Acceptance criteria
- [ ] Integration (edge case): прямой UPDATE/DELETE/INSERT в обход триггеров → `verify` указывает первую испорченную запись.
- [ ] Integration: две конкурентные записи в одну цепочку → seq без дыр и дублей.
- [ ] Unit: canonical JSON стабилен (Decimal строками, порядок ключей).

## Заметки
—
