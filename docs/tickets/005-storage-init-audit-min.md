# 005: БД, init, локальный пользователь, минимальный журнал
Статус: DONE
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
- [x] Integration: `init` на пустом каталоге создаёт БД, master key 0600 и пользователя; повторный `init` идемпотентен и не перезаписывает ключ. — `tests/integration/test_init_and_startup.py` (`test_init_on_an_empty_directory_...`, `test_second_init_is_idempotent_and_keeps_the_key`, `test_init_command_in_a_real_process`, `test_init_refuses_to_replace_a_lost_key`); unit — `tests/unit/test_init_installation.py`, `tests/unit/test_master_key.py`
- [x] Integration: сервер без миграций / с непоследней ревизией / без триггеров не стартует и пишет понятную причину. — `tests/integration/test_init_and_startup.py::test_server_does_not_start_and_says_why` (нет БД, пустая БД, ревизия 0001, неизвестная ревизия 9999, нет UPDATE-триггера, DELETE-триггер с `WHEN 0`; exit 2 + причина в stderr), `test_storage_check_explains_why_it_refuses`, `test_server_refuses_in_a_real_process_without_touching_stdout`, `test_server_without_its_local_user_does_not_start`, `test_too_old_sqlite_is_refused`
- [x] Integration: UPDATE/DELETE `audit_log` через SQL → ошибка триггера (FR-37). — `tests/integration/test_init_and_startup.py::test_update_or_delete_of_the_journal_is_rejected_by_the_database` (+ `test_journal_still_accepts_inserts`)
- [x] Integration: две конкурентные write-транзакции из двух процессов не дают `database is locked` при busy_timeout (базовый тест). — `tests/integration/test_sqlite_multiprocess.py` (2 процесса × 40 транзакций «прочитать → подождать → записать», seq без дыр); негативный контроль: с `BEGIN` вместо `BEGIN IMMEDIATE` тест падает (проверено руками), детерминированная демонстрация — `test_sqlite_storage.py::test_why_immediate_deferred_read_then_write_fails_without_waiting`
- [x] Unit: Decimal и время переживают round-trip без потерь; `now` берётся из `Clock`. — `tests/unit/test_db_types.py`, `tests/unit/test_init_installation.py::test_first_init_creates_key_user_and_journal_in_one_write_transaction`; через реальную SQLite — `test_sqlite_storage.py::test_decimal_and_time_survive_a_real_sqlite_round_trip`, `test_now_is_taken_from_the_clock_not_the_database`

## Заметки
Решения и отклонения (2026-10-27, исполнитель):
- **Зависимости:** `sqlalchemy[asyncio,aiosqlite]>=2.1.6,<2.2` (greenlet 3.5.6), `alembic>=1.20.1`, `aiosqlite>=0.22.1` — последние на PyPI 2026-10-27; PLAN раздел 2 обновлён. import-linter: domain и application не импортируют `sqlalchemy/alembic/aiosqlite`.
- **Порты:** `UnitOfWorkFactory.__call__(*, write) -> AsyncContextManager[UnitOfWork]` (commit при выходе, rollback при исключении), `UnitOfWork.users/.audit`, `UserRepository(add/get/get_by_name)`, `AuditLogRepository(append/list)`, `Clock.now()`, `IdGenerator.new_id()`. **Добавлен порт `MasterKeyStore(exists/create)`** (в тикете не было): правило «не генерировать новый ключ для уже инициализированной установки» — политика, ей место в use case, а файл — в адаптере. Read-UoW запрещает запись (SQL и fake), чтобы use case не мог случайно писать без write-блокировки.
- **BEGIN IMMEDIATE:** рецепт SQLAlchemy (`isolation_level=None` в `connect` + свой `BEGIN` в событии `begin`); режим выбирается execution option `gx_write`, который UoW ставит на соединение до `begin()`. Pragmas на каждом соединении: `busy_timeout` (первым), WAL, `synchronous=NORMAL`, `foreign_keys=ON`. Миграции тоже идут одной транзакцией `BEGIN IMMEDIATE` (DDL транзакционен, т.к. legacy-режим драйвера выключен; в env.py `transactional_ddl=True`).
- **`lock_user`** на SQLite — no-op, но падает, если вызван не из write-UoW (гарантия иначе молча не держится); ветка Postgres — `NotImplementedError` до 028. Нумерация журнала — один атомарный upsert `audit_heads ... ON CONFLICT DO UPDATE last_seq+1 RETURNING` (работает и в Postgres), + PK(chain_id, seq).
- **Время:** `EpochMillis` отвергает naive и суб-миллисекундные значения (вместо тихого усечения); `SystemClock` округляет вниз до мс — значение из `Clock` всегда переживает round-trip. **Decimal:** `DecimalText` хранит `str(Decimal)` (точный, сохраняет `0.10`), отвергает float/int/NaN. В canonical JSON журнала Decimal нормализуется без `normalize()` — тот округляет до 28 знаков (баг пойман тестом).
- **Журнал:** цепочка `system` для событий установки; первый `init` пишет `installation_initialized` и `user_created` (actor `cli:init`) в той же write-транзакции, что и пользователь. «Установка инициализирована» ⇔ в цепочке `system` есть записи. Повторный `init` без изменений журнал не трогает (идемпотентность = нет изменения состояния; запись о no-op решено не делать). Без MAC (D-13).
- **Init:** весь `init` под `fcntl.flock` на `<db>.init.lock` (только POSIX; Windows вне scope). Миграция → проверка готовности → use case. Каталоги создаются 0700, файл БД 0600. Отдельной команды `migrate` нет: `init` идемпотентен и мигрирует существующую БД (тикет допускает `init`/`migrate`).
- **Проверка при старте (`serve`):** SQLite ≥ 3.35; файл БД существует (проверка БД не создаёт и не мигрирует — тест); ревизия = head (отдельные сообщения для «не инициализирована», «старее» и «неизвестна/новее»); триггеры проверяются по SQL-тексту регуляркой `BEFORE {UPDATE|DELETE} ON audit_log [FOR EACH ROW] BEGIN SELECT RAISE(ABORT,` — триггер с `WHEN 0` или пустым телом не засчитывается; затем `ResolveLocalPrincipal` (нет пользователя / revoked → отказ). Всё → exit 2, причина в stderr и в JSON-лог `startup_refused`; stdout не трогается. Ревизия читается прямо из `alembic_version` (без `MigrationContext` — он шумит в логах на каждом старте).
- **Миграции:** две ревизии (0001 users, 0002 audit_log/audit_heads/триггеры) — в т.ч. чтобы был реальный тест «ревизия не последняя». Миграции написаны руками и заморожены; `schema.py` сверяется с ними тестом (`alembic.autogenerate.compare_metadata` → пустой diff). `alembic.ini` нет: env.py принимает только переданное соединение, CLI alembic намеренно не работает.
- **Config:** `GX_DB_PATH`, `GX_MASTER_KEY_FILE` (XDG-дефолты, разные каталоги), `GX_LOCAL_USER=local`, `GX_SQLITE_BUSY_TIMEOUT_MS=5000`. Пути обязаны быть абсолютными (`~` раскрывается): MCP-клиент запускает сервер из произвольного cwd. Ключ внутри каталога БД → ошибка конфигурации. `load_settings` теперь превращает `ValidationError` в `ConfigError` (раньше неверный `GX_LOG_LEVEL` давал трейсбек).
- **Principal:** `serve` резолвит локального пользователя до старта MCP и пишет `principal_resolved` в лог; в инструменты он пока не передаётся (нет инструментов, которым он нужен; `interface/mcp` не трогали из-за параллельного спайка 002). Передача principal в `UseCases`/контекст — в 006/008.
- **E2E stdio** теперь сначала делает `run_init` во временном каталоге и проверяет `principal_resolved` в stderr.

Ревью, раунд 1 (CHANGES_REQUESTED, закрыто 2026-10-28):
- M1. `INSERT OR REPLACE`/`REPLACE INTO` переписывали запись журнала (DELETE-триггер при REPLACE не срабатывает без `recursive_triggers`). Миграция **0003**: `BEFORE INSERT ... WHEN EXISTS(... chain_id, seq) → RAISE`; `PRAGMA recursive_triggers=ON` в `configure_sqlite`; триггер входит в проверку при старте; оба случая — в параметризации FR-37-теста (атакующее соединение — сырой `sqlite3` без pragma).
- m1. Проверка триггеров — точное сравнение нормализованного (регистр, пробелы) SQL по имени с константами `infrastructure/db/triggers.py`; ловит `WHEN 0`, `WHERE 0` в теле, отсутствие. Отличие от предложения: миграции **не импортируют** константы, а хранят замороженную копию SQL (миграция не должна меняться вместе с кодом приложения); совпадение доказывает интеграционный тест (свежая БД проходит проверку).
- m2. `DatabaseError` (битый/чужой файл) в `check_storage` и `upgrade_to_head` → `StorageNotReady` с причиной; `upgrade_to_head` до `command.upgrade` отказывает на неизвестной ревизии и файл не трогает. `serve` → exit 2, `init` → exit 1 (как остальные ошибки init); stdout пуст, без трейсбека — тесты.
- m3. Проверка при старте открывает файл `file:...?mode=ro` (uri) без WAL/synchronous pragma; тесты: 0-байтный файл не меняется и `-wal/-shm` не появляются, инициализированная БД побайтно не меняется.
- m4. Master key: tmp-файл в том же каталоге (O_EXCL|O_NOFOLLOW, 0600) + fsync → `os.link` → unlink tmp → fsync каталога; `FileExistsError` → `MasterKeyError`; тесты на отсутствие tmp-файлов и на сбой записи.
- m5. Проверка master key в `serve` перенесена в scope и AC тикета 007.
- m6, m7. Ссылка в `schema.py` исправлена; module-level `pytestmark` с `req` убраны в тестах тикета, маркеры на конкретных тестах, `docs/traceability.md` пересобран.
- README: Windows вне scope (flock). В заметки 006/008 — `lock_user` на SQLite no-op.

Предложения (вне scope):
- Хранить в БД отпечаток master key (например, HMAC от константы подключом) и сверять при старте — поймает подмену/путаницу ключа раньше, чем 007 попытается расшифровать.
- `doctor` (032) может переиспользовать `check_storage` и проверку прав на файл ключа.
- Dev-команда для генерации новой ревизии Alembic (сейчас ревизии пишутся руками по образцу 0001/0002).
