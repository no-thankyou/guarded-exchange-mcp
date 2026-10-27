# 016: Pending, CLI-подтверждение и executor (однопроцессный)
Статус: TODO
Зависит от: 012, 015
Требования: FR-19, FR-20, FR-22, FR-23, FR-24, FR-25, FR-47, FR-29 (замена ключа → аннулирование)

## Цель
Действие, требующее подтверждения, создаёт pending (FR-47б); человек одобряет/отклоняет через CLI (если канал включён админом); executor исполняет одобренное сам; агент видит статус и может отменить своё pending.

## Scope
- Domain `confirmation.py`: автомат состояний (PLAN 5.1), `action_digest` (D-4), TTL; таблица `pending_confirmations`.
- Use cases: `SubmitAction` → pending (лимит одновременных pending count+insert, D-17; повтор с тем же digest возвращает тот же pending, D-20); `ResolveConfirmation` (только не-MCP); `CancelConfirmation`.
- MCP: `get_confirmation_status`, `cancel_confirmation`; CLI `confirm approve|reject <id>` — доверен только если включён админом (FR-42, E-5).
- `ConfirmedActionExecutor`: сигнал + scan, CAS `APPROVED→EXECUTING`, общий `ExecuteConfirmed` с базовой перепроверкой (доступ, credential, kill switch, лимиты), дедлайн `approved_at + 30 с` (D-3), `asyncio.shield`.
- Замена/удаление ключа аннулирует PENDING/APPROVED (D-9); всё в журнал с каналом решения.

## Non-scope
- Полная таблица FR-48 (017), Telegram (018), несколько процессов (019), MRTR (020).

## Acceptance criteria
- [ ] Unit: двойное одобрение — исполнение ровно одно (CAS); одобрение после TTL → `expired`; отказ → `rejected` в журнале.
- [ ] Unit: превышение лимита одновременных pending → `pending_limit_reached`.
- [ ] Unit: повтор вызова без ключа во время PENDING возвращает тот же pending (через 40 с тоже).
- [ ] Unit: без доверенного канала → `confirmation_channel_unavailable`, pending не создан (FR-25).
- [ ] Unit: `cancel_confirmation` переводит только своё PENDING/APPROVED в CANCELLED и не исполняет.
- [ ] Integration: CLI approve за 1 с до TTL → исполнено (D-3); после замены ключа pending аннулирован.

## Заметки
—
