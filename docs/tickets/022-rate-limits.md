# 022: Rate limits трёх уровней и лимиты биржи
Статус: DEFERRED (v2, решение пользователя 2026-10-29, MVP)
Зависит от: 011
Требования: FR-34, FR-35, FR-28

## Цель
Пользователь или зациклившийся агент не исчерпывает лимиты сервера и биржи; при приближении — `rate_limited` с `retry_after_s` (секунды, поле ошибки по ADR-013).

## Scope
- `ratelimit.py`: бакеты на пользователя (read/write), на UID биржи, глобальный IP (ADR-011, PLAN раздел 6); отмены, `activate_kill_switch`, `cancel_confirmation`, второй раунд MRTR не тратят write.
- Обновление по `X-Bapi-Limit-Status`; circuit breaker на 10006 по (UID, группа) и на 403 глобально (D-16).
- Executor при отказе лимитера ждёт до дедлайна, затем FAILED(rate_limited).

## Non-scope
- FR-46 (023).

## Acceptance criteria
- [ ] Unit (FakeClock): исчерпание бакета → `rate_limited` с корректным `retry_after_s`; пополнение.
- [ ] Unit: отмены не тратят write-бакет.
- [ ] Integration: заголовки биржи обновляют остаток; 10006 открывает breaker до reset timestamp.

## Заметки
- Из 008: доменная `RateLimited` (`rate_limited`) уже есть; `retCode 10006` маппится в неё с `retry_after_s` из `X-Bapi-Limit-Reset-Timestamp` (`BybitClient._retry_after`, 1…600 с, отсчёт от серверного `time` ответа, а не от локальных часов; без заголовка — 1 с). HTTP 403 пока `exchange_unavailable`; breaker и свои бакеты — здесь.
