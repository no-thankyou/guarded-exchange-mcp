# 019: Несколько stdio-процессов: лидерство и fencing
Статус: TODO
Зависит от: 018
Требования: FR-26, FR-47, ADR-003, ADR-006 (D-3)

## Цель
Несколько процессов сервера на одной SQLite (Claude Code + Desktop) работают корректно: Telegram-поллер и executor только у лидера, лидерство переходит при смерти процесса, нет двойного исполнения и ложного FAILED.

## Scope
- Лидерство `fcntl.flock` (локально) + `executor_epoch`; попытка захвата раз в 2 с; scan APPROVED раз в 1 с.
- Fencing: CAS `→EXECUTING` пишет epoch и `exec_started_at`; восстановление EXECUTING с меньшим epoch не раньше +30 с.
- Проверка живости внешнего канала: нет лидера → `confirmation_channel_unavailable` (D-7).

## Non-scope
- Postgres lease и LISTEN/NOTIFY (028).

## Acceptance criteria
- [ ] Integration (два реальных процесса): pending создан в процессе-не-лидере → одобрен → исполнен лидером ровно один раз.
- [ ] Integration: лидер убит → второй процесс становится лидером и исполняет APPROVED.
- [ ] Integration: EXECUTING старого лидера не трогается до +30 с, двойной отправки нет.
- [ ] Integration: нагрузка двух процессов без `database is locked`.

## Заметки
—
