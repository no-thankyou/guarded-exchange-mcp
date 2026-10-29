# 025: Checkpoints журнала и чтение журнала
Статус: DEFERRED (v2, решение пользователя 2026-10-29, MVP)
Зависит от: 024, 018
Требования: FR-38, FR-50, FR-41

## Цель
Периодические checkpoints голов цепочек вне БД (файл, Telegram-чат админа); пользователь читает свой журнал через MCP и CLI, админ — любой.

## Scope
- Checkpoint `{chain_id: (seq, head)}` с HMAC; `verify` сверяет с последним checkpoint.
- MCP `get_audit_log` (свой журнал, фильтры, пагинация, `untrusted_text`); CLI `audit show [--user]`.

## Non-scope
- Удалённый admin по HTTP.

## Acceptance criteria
- [ ] Integration: удаление хвоста до checkpoint обнаруживается; после — честно не обнаруживается (тест фиксирует ограничение, README).
- [ ] E2E: пользователь видит только свой журнал; свободный текст помечен.

## Заметки
—
