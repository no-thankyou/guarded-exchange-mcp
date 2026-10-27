# 018: Telegram-канал подтверждения и pairing
Статус: TODO
Зависит от: 016
Требования: FR-20, FR-21, FR-24, FR-25, FR-42, FR-16 (отмена pending)

## Цель
Человек привязывает Telegram (код из бота → CLI), получает уведомления с точными параметрами и кнопками Approve/Reject; решение принимается по `from.id` привязки.

## Scope
- aiogram 3, long polling (в одном процессе до 019), токен бота зашифрован (D-14).
- Pairing и отвязка — CLI, журнал; `telegram_bindings`.
- Уведомление после коммита pending; ошибка отправки → CANCELLED + `confirmation_channel_unavailable` (D-7); `telegram_message_id` в pending; редактирование при решении/истечении/отмене.
- Kill switch отменяет PENDING/APPROVED INCREASES и редактирует сообщения.
- Порт `ConfirmationChannel`; адаптер тестируется на фейковом Bot API (без сети).

## Non-scope
- Несколько процессов (019).

## Acceptance criteria
- [ ] Integration (фейковый Bot API): callback от чужого `from.id` игнорируется и журналируется; от привязанного — одобряет.
- [ ] Integration: сообщение содержит все поля FR-21; `callback_data` содержит только id.
- [ ] Unit: ошибка sendMessage → pending CANCELLED, агент получает `confirmation_channel_unavailable`.
- [ ] Unit: включение kill switch отменяет ожидающие INCREASES.

## Заметки
—
