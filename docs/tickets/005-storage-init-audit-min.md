# 005: БД, init, локальный пользователь, минимальный журнал
Статус: TODO
Зависит от: 001
Требования: FR-37, FR-45 (часть), NFR-1, NFR-2

## Цель
Команда `guarded-exchange init` создаёт БД SQLite, применяет миграции, генерирует master key (файл 0600 вне каталога БД) и локального пользователя; сервер при старте проверяет схему и fail closed. Минимальный append-only журнал фиксирует `init` и создание пользователя.

## Scope
- SQLAlchemy 2.1 Core + aiosqlite + Alembic; таблицы `users`, `audit_log` (без MAC, D-13), `audit_heads`.
- `db/dialect.py`: TypeDecorator Decimal-как-TEXT, время INTEGER epoch ms (D-10), `lock_user`; `UnitOfWork(write=True)` → `BEGIN IMMEDIATE`, read — deferred; WAL, `busy_timeout`, `foreign_keys=ON`, `synchronous=NORMAL`.
- Миграции только командой `init`/`migrate` под эксклюзивной блокировкой (D-11); старт сервера проверяет ревизию = head, триггеры на месте, SQLite ≥ 3.35.
- Триггеры `BEFORE UPDATE/DELETE → RAISE` на `audit_log`; репозиторий журнала только `append`/`list`.
- Порты `UserRepository`, `AuditLogRepository`, `UnitOfWork`, `Clock`, `IdGenerator`; in-memory fakes для unit-тестов.
- Сервер в локальном режиме берёт principal = локальный пользователь из конфига.

## Non-scope
- Политика (006), ключи (007), HMAC-цепочка (024), Postgres (028).

## Acceptance criteria
- [ ] Integration: `init` на пустом каталоге создаёт БД, master key 0600 и пользователя; повторный `init` идемпотентен и не перезаписывает ключ.
- [ ] Integration: сервер без миграций / с непоследней ревизией / без триггеров не стартует и пишет понятную причину.
- [ ] Integration: UPDATE/DELETE `audit_log` через SQL → ошибка триггера (FR-37).
- [ ] Integration: две конкурентные write-транзакции из двух процессов не дают `database is locked` при busy_timeout (базовый тест).
- [ ] Unit: Decimal и время переживают round-trip без потерь; `now` берётся из `Clock`.

## Заметки
—
