# Research: хранилище, журнал (tamper-evidence), шифрование ключей биржи

Дата исследования: 2026-10-27. Вопрос — SPEC раздел 9, пункт 5.

## 1. Вопрос и затронутые требования

Что хранить и где: пользователи, политики (FR-12, FR-44), ключи биржи в зашифрованном виде (FR-30, FR-31, FR-49), ожидающие подтверждения с TTL (FR-22, FR-23, FR-47, FR-48), ключи идемпотентности и окно 30 с (FR-26), счётчики rate limit (FR-34, FR-35) и лимит действий без подтверждения (FR-46), журнал (FR-36, FR-37, FR-38) и его tamper-evidence (FR-50). Сквозные ограничения: изоляция (FR-33), отзыв доступа с аннулированием pending (FR-29), тестируемость без сети (NFR-2), p95 накладных ≤ 50 мс (NFR-4), запуск одной командой (NFR-6), «без преждевременных абстракций» и «БД в тестах — реальная, testcontainers» (`ENGINEERING.md`).

Подвопросы: (а) SQLite для локального режима vs PostgreSQL для удалённого, SQLAlchemy 2.x async + Alembic vs raw SQL, можно ли одним кодом без потери гарантий; (б) нужен ли Redis; (в) способ tamper-evidence и конкурентная запись в цепочку; (г) шифрование ключей биржи и ротация; (д) testcontainers в CI.

## 2. Факты (проверено 2026-10-27)

### 2.1 Версии библиотек
| Пакет | Версия | Дата релиза | requires_python | Источник |
|---|---|---|---|---|
| SQLAlchemy | 2.1.6 | 2026-10-21 | >=3.11 | https://pypi.org/pypi/sqlalchemy/json, https://pypi.org/pypi/sqlalchemy/2.1.6/json |
| Alembic | 1.20.1 | 2026-10-22 | >=3.10 | https://pypi.org/pypi/alembic/1.20.1/json |
| aiosqlite | 0.22.1 | 2025-12-23 | >=3.9 | https://pypi.org/pypi/aiosqlite/json |
| psycopg (3) | 3.3.6 | 2026-09-18 | >=3.10 | https://pypi.org/pypi/psycopg/json |
| asyncpg | 0.32.0 | 2026-10-06 (wheels cp312–cp314, cp314t) | >=3.9 | https://pypi.org/pypi/asyncpg/0.32.0/json |
| cryptography | 50.0.2 | 2026-09-30 | >=3.9 (кроме 3.9.0/3.9.1) | https://pypi.org/pypi/cryptography/50.0.2/json |
| testcontainers | 4.15.0 | 2026-07-24, есть extra `postgres` | >=3.10 | https://pypi.org/pypi/testcontainers/json |
| PostgreSQL | 18 (18.6 текущий minor), 14 — EOL 2026-11-12 | 18.0 — 2025-09-25 | — | https://www.postgresql.org/support/versioning/ |

Расхождение: веб-поиск по зеркалам PyPI показывал для testcontainers только 4.15.0rc1/rc2 и 4.14.2 как последний stable; официальный JSON PyPI показывает 4.15.0 от 2026-07-24 — доверяем PyPI.

### 2.2 SQLAlchemy 2.1
- Extras: `sqlalchemy[aiosqlite]`, `sqlalchemy[postgresql-asyncpg]`, `sqlalchemy[postgresql-psycopg]` (psycopg>=3.0.7, !=3.1.15); `greenlet` больше не ставится по умолчанию, только через `[asyncio]` (https://pypi.org/pypi/sqlalchemy/2.1.6/json, https://docs.sqlalchemy.org/en/21/changelog/migration_21.html).
- В 2.1 драйвер PostgreSQL по умолчанию — psycopg 3 («Default PostgreSQL driver changed to psycopg (psycopg 3)»); Python 3.10 больше не поддерживается (https://docs.sqlalchemy.org/en/21/changelog/migration_21.html).
- Upsert: `sqlalchemy.dialects.sqlite.insert()` и `sqlalchemy.dialects.postgresql.insert()` дают одинаковые `on_conflict_do_nothing()` / `on_conflict_do_update()`; в SQLite upsert с 3.24.0, `RETURNING` с 3.35 (поддержан диалектом с 2.0) (https://docs.sqlalchemy.org/en/21/dialects/sqlite.html).
- SQLite + pysqlite/aiosqlite: драйвер по умолчанию в «legacy» режиме транзакций, не шлёт BEGIN для SELECT; следствия по документации — SERIALIZABLE не полностью работает, SAVEPOINT и transactional DDL некорректны. Есть рецепт: `isolation_level=None` на connect и свой `BEGIN` в событии `begin`; на Python 3.12+ можно `connect_args={"autocommit": False}` (PEP 249 режим); документация предупреждает, что отключение legacy-режима «more susceptible to locked database errors» (https://docs.sqlalchemy.org/en/21/dialects/sqlite.html).
- aiosqlite «uses a background thread for each connection» и не даёт настоящего non-blocking IO; для файловой БД пул — `AsyncAdaptedQueuePool` (с 2.0.38), для `:memory:` — `StaticPool`, в котором корутины делят одно соединение (ROLLBACK одной может откатить работу другой) (там же).
- `PRAGMA foreign_keys = ON` нужно выполнять на каждом соединении (там же).

### 2.3 SQLite
- `BEGIN DEFERRED` + сначала SELECT, затем запись → «upgrade» до write-транзакции может вернуть `SQLITE_BUSY`, если другое соединение уже пишет; `BEGIN IMMEDIATE` берёт write-блокировку сразу (https://www.sqlite.org/lang_transaction.html).
- WAL: читатели и писатель работают параллельно, но «there can only be one writer at a time»; все процессы должны быть на одном хосте, WAL не работает на сетевой ФС (https://www.sqlite.org/wal.html).
- Нет отдельного типа даты/времени (TEXT/REAL/INTEGER); NUMERIC affinity превращает числовой текст в INTEGER/REAL (IEEE double, ~15.95 значащих цифр) → `Decimal` в NUMERIC-колонке теряет точность; `STRICT` таблицы с 3.37.0 (https://www.sqlite.org/datatype3.html).
- Python 3.12 `sqlite3`: `Connection.autocommit` (False — PEP 249, рекомендованное значение; дефолт пока `LEGACY_TRANSACTION_CONTROL`, сменится в будущем); модуль требует SQLite ≥ 3.7.15 — версия SQLite зависит от сборки Python/ОС (https://docs.python.org/3.12/library/sqlite3.html).
- Alembic: SQLite почти не поддерживает ALTER, нужен batch mode (`render_as_batch=True`, «move and copy» таблицы); на других БД обычный ALTER (https://alembic.sqlalchemy.org/en/latest/batch.html).

### 2.4 PostgreSQL 18
- `INSERT ... ON CONFLICT DO UPDATE` гарантирует атомарный исход INSERT или UPDATE «even under high concurrency»; `RETURNING` возвращает только вставленные/обновлённые строки (https://www.postgresql.org/docs/current/sql-insert.html).
- Transaction-level advisory locks освобождаются автоматически в конце транзакции; `SELECT ... FOR UPDATE` блокирует строки до конца транзакции (https://www.postgresql.org/docs/current/explicit-locking.html).
- Триггеры на `TRUNCATE` возможны только `FOR EACH STATEMENT` (https://www.postgresql.org/docs/current/sql-createtrigger.html).

### 2.5 Tamper-evident журналы в реальных системах
- RFC 9162 (Certificate Transparency 2.0): Merkle tree с domain separation (leaf `HASH(0x00||d)`, node `HASH(0x01||l||r)`), inclusion proof (запись в дереве), consistency proof (старое дерево — префикс нового, т.е. append-only), Signed Tree Head (timestamp, size, root). Лог, показывающий разным клиентам разные версии (split view), стандарт не предотвращает — это вне scope, нужны внешние наблюдатели (https://www.rfc-editor.org/rfc/rfc9162.html).
- Trillian — «in maintenance mode», новым операторам рекомендуют Tessera (https://github.com/google/trillian). Tessera — Go-библиотека tile-based логов (C2SP tlog-tiles), подписанные checkpoints, witnesses cosign checkpoints; бэкенды GCP/AWS/POSIX (https://github.com/transparency-dev/tessera). Python-реализации нет — для нас это ориентир, а не зависимость.
- Russ Cox «Transparent Logs for Skeptical Clients» (2019-03-01): клиент хранит только последний (size, top hash); подписанный head не даёт серверу отречься от несогласованности; fork обнаруживается сравнением кэшированных head разными клиентами (https://research.swtch.com/tlog).
- Crosby, Wallach, «Efficient Data Structures for Tamper-Evident Logging», USENIX Security 2009: дерево вместо линейной hash chain даёт логарифмические доказательства; модель — недоверенный logger и аудиторы (https://www.usenix.org/conference/usenixsecurity09/technical-sessions/presentation/efficient-data-structures-tamper-evident). Детали про truncation из полного текста в этой сессии извлечь не удалось (PDF не распарсился).
- pgAudit: пишет в стандартный лог PostgreSQL; «best-effort and not transactional», нет гарантии записи для каждого committed транзакции, суперпользователей надёжно не аудирует; о tamper-evidence ничего нет (https://github.com/pgaudit/pgaudit). Т.е. pgaudit — аудит SQL-операций БД, а не замена прикладного журнала FR-36.
- RFC 8785 (JCS, Informational): канонический JSON для хэширования/подписи — сортировка ключей по UTF-16 code units, без пробелов, UTF-8, числа по правилам ECMAScript (IEEE double) (https://www.rfc-editor.org/rfc/rfc8785.html). Следствие: если все денежные величины сериализовать строками (`Decimal` → str), проблема канонизации чисел исчезает, и достаточно `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` с запретом float.

### 2.6 Шифрование (cryptography 50.x)
- Fernet: AES-128-CBC + PKCS7 + HMAC-SHA256, ключ — urlsafe base64 32 байта (16 подпись + 16 шифрование); timestamp в токене открытым текстом; `MultiFernet` — шифрует первым ключом, расшифровывает любым, `rotate()` перешифровывает (https://cryptography.io/en/latest/fernet/). Associated data в спецификации нет: HMAC покрывает только `Version ‖ Timestamp ‖ IV ‖ Ciphertext` (https://github.com/fernet/spec/blob/master/Spec.md).
- AESGCM: ключ 128/192/256 бит, рекомендован 96-битный nonce, «NEVER REUSE A NONCE with a key», `associated_data` аутентифицируется, но не шифруется; ошибка — `InvalidTag`; есть `AESGCMSIV` (устойчивее к повтору nonce) (https://cryptography.io/en/latest/hazmat/primitives/aead/).
- Лимит ~2^32 шифрований одним ключом при случайных IV (NIST SP 800-38D §8.3) — подтверждён только вторичными источниками (https://csrc.nist.gov/csrc/media/Events/2023/third-workshop-on-block-cipher-modes-of-operation/documents/accepted-papers/Practical%20Challenges%20with%20AES-GCM.pdf, https://eprint.iacr.org/2024/1111.pdf); для нас нерелевантен по объёму (единицы-сотни ключей).

### 2.7 testcontainers-python
- `PostgresContainer(image="postgres:latest", port=5432, username, password, dbname, driver="psycopg2")`; `driver=None` даёт URL без драйвера; пример в доках — `with PostgresContainer("postgres:16") as postgres: create_engine(postgres.get_connection_url())` (https://testcontainers-python.readthedocs.io/en/latest/modules/postgres/README.html). Точную сигнатуру `get_connection_url` и поддержку `driver="psycopg"` в исходниках проверить не удалось (raw-ссылка на GitHub вернула 404) — проверить в тикете.

## 3. Варианты и trade-offs

### 3.1 Какая БД
| Вариант | Плюсы | Минусы |
|---|---|---|
| A. SQLite локально + PostgreSQL удалённо, один код на SQLAlchemy Core + Alembic | локальный режим без Docker (stdio-сервер для Claude Desktop запускается одной командой); удалённый — «настоящая» БД, хорошо для интервью | два диалекта: матрица тестов ×2, мелкие ветки по диалекту (upsert-импорт, блокировки), batch-миграции для SQLite |
| B. Только PostgreSQL (локально тоже через docker compose) | один диалект, честные row locks, меньше тестов | локальный stdio-режим требует поднятого Postgres → хуже NFR-6 и UX Claude Desktop |
| C. Только SQLite (и удалённо) | минимум кода и инфраструктуры | один writer, только один хост; в портфолио не показывает Postgres/multi-tenant под нагрузкой |

Почему «один код» реалистичен (вариант A): все нужные гарантии выражаются в общем подмножестве SQL — unique constraints, `INSERT ... ON CONFLICT ... RETURNING`, условный `UPDATE ... WHERE status = 'pending' AND expires_at > :now RETURNING` (атомарный claim), транзакции. Различаются только: (1) способ сериализации конкурентных писателей — в Postgres `SELECT ... FOR UPDATE` на строке пользователя / `pg_advisory_xact_lock`, в SQLite — единственный writer на БД, но нужно `BEGIN IMMEDIATE` (иначе read→write upgrade даёт `SQLITE_BUSY`) + WAL + `busy_timeout`; (2) типы — `Decimal` нельзя хранить в NUMERIC на SQLite (теряется точность), время без tz. Решение: денежные величины — TEXT (строка Decimal) на обеих БД через `TypeDecorator`, время — либо `timestamptz`/TEXT ISO UTC через `TypeDecorator`, либо integer epoch ms (сравнения TTL в SQL одинаковы на обеих БД). Это ~2 TypeDecorator и одна функция `lock_user()` с веткой по диалекту в инфраструктуре — не утечка в application.

Важный нюанс локального режима: Claude Desktop и Claude Code запускают каждый свой stdio-процесс сервера → «несколько клиентов одного пользователя» (FR-26, FR-47, edge case раздела 6) в локальном режиме — это несколько процессов на одном SQLite-файле. Следствия: всё, что должно работать между клиентами (идемпотентность, окно 30 с, FR-46, однократное исполнение подтверждённого действия), обязано жить в БД, а не в памяти процесса; WAL поддерживает несколько процессов на одном хосте. (Порождает ли каждый клиент отдельный процесс — утверждение по общей модели stdio-транспорта MCP, в этой сессии не перепроверялось.)

### 3.2 SQLAlchemy vs raw SQL
- Raw SQL в репозиториях: максимально прозрачно, но разные paramstyle (`?` sqlite, `%s` psycopg, `$1` asyncpg), миграции и DDL пришлось бы вести вручную для двух БД — фактически свой мини-ORM.
- SQLAlchemy 2.1 **Core** (Table + `select/insert/update`), без ORM-маппинга: диалектонезависимый SQL, Alembic autogenerate, доменные dataclasses маппятся в репозитории вручную (соответствует ENGINEERING.md: SQL только в infrastructure, DTO не протекают в домен). ORM (unit of work, identity map, lazy loading в async) здесь не нужен и добавляет ловушки (`MissingGreenlet`).
- Драйвер Postgres: psycopg 3 (дефолт SQLAlchemy 2.1, один драйвер для sync Alembic и async приложения) vs asyncpg (быстрее, но отдельный драйвер; для нашего объёма разница не важна).

### 3.3 Redis
Не нужен в v1. FR-26 (идемпотентность, окно 30 с) и FR-46 (действия без подтверждения за окно) — safety-критичны и должны работать между процессами/клиентами → БД с unique constraint и транзакцией. FR-34 (частота запросов на пользователя) и FR-35 (вес запросов к бирже) — не safety, а защита ресурсов: in-memory token bucket / sliding window в процессе (Clock-порт для тестов), без записи в БД на каждый read-вызов (NFR-4). Ограничение: в локальном режиме с несколькими процессами лимит считается на процесс; в удалённом — на инстанс (v1 — один инстанс). Redis оправдан только при горизонтальном масштабировании удалённого режима — это отдельное решение в будущем; сейчас он ломает «одну команду» для локального режима и добавляет порт без второй реализации.

### 3.4 Модель данных (ориентир для PLAN)
- `users` (status active/revoked, env testnet/mainnet, kill switch user/admin), `policies` (ceiling админа и user-overrides отдельно; эффективная политика вычисляется в домене, FR-12).
- `exchange_credentials` (id, user_id, ciphertext, nonce, kek_version, permissions, status, created_at) — ссылка `credential_id` из pending: замена/удаление ключей аннулирует pending со старым id (FR-29).
- `pending_confirmations` (id — случайный непредсказуемый токен, user_id, credential_id, canonical action JSON + `action_hash`, status pending/approved/rejected/expired/cancelled/executing/done/failed, expires_at, decided_by/channel). Однократность (FR-22, FR-47): переходы только условным `UPDATE ... WHERE id=:id AND user_id=:u AND status='pending' AND expires_at > :now RETURNING`; исполнение «захватывается» `approved → executing` одним процессом. Истечение — лениво при чтении/решении + фоновая задача, пишущая «expired» в журнал. Отзыв доступа (FR-29) — в одной транзакции `users.status='revoked'` и `UPDATE pending SET status='cancelled'`.
- `idempotency_records` (user_id, key, request_hash, status in_progress/completed/unknown, result JSON, created_at). Явный ключ: `INSERT ... ON CONFLICT (user_id, key) DO NOTHING RETURNING` → вставилось — исполняем; нет — читаем запись: другой `request_hash` → конфликт (FR-26), тот же — вернуть результат (или «в процессе»). Без ключа: та же таблица/вторая таблица с `fingerprint = hash(tool, canonical args)` и `expires_at = now+30s`, upsert `ON CONFLICT (user_id, fingerprint) DO UPDATE SET ... WHERE existing.expires_at <= :now RETURNING` — одна атомарная команда без внешних блокировок, работает на обеих БД (на SQLite нужен ≥3.35 из-за RETURNING).
- FR-46: не отдельный счётчик, а `COUNT(*)` по таблице исполненных действий (или журналу) `WHERE user_id AND confirmed=false AND executed_at > now - window` с индексом; проверка и резервирование слота — под per-user сериализацией (`lock_user`), иначе два клиента на границе лимита пройдут оба.
- `audit_log` — см. 3.5. Изоляция (FR-33): каждый метод репозитория принимает `user_id` и включает его в WHERE; «не найдено» и «чужое» дают одинаковую ошибку. Postgres RLS — возможное усиление, для v1 избыточно.

### 3.5 Tamper-evidence журнала (FR-50)
Модель угрозы FR-50: кто-то правит хранилище в обход системы. Что обнаруживает каждый вариант:

| Способ | Изменение | Удаление/вставка в середине | Усечение хвоста | Полная переписка цепочки | Цена |
|---|---|---|---|---|---|
| Hash chain SHA-256 (без секрета) | да, если атакующий не пересчитал хвост | да, при том же условии | нет | **нет** — пересчитать может любой с доступом к БД | минимальная |
| HMAC-цепочка (ключ вне БД) | да | да | нет без anchor | да, если ключ не скомпрометирован | минимальная + управление ключом |
| + anchor/checkpoint вне БД (seq, head, подпись/HMAC) | да | да | да — до последнего anchor; хвост после него — нет | да, до последнего anchor, даже при утечке ключа позже | файл/внешний канал, периодическая задача |
| Merkle tree (RFC 9162 / Tessera) + signed tree heads + witnesses | да | да | да, при наличии внешних хранителей head | да | высокая: дерево, proofs, внешние witnesses; Python-библиотеки уровня Tessera нет |

Замечания:
- Merkle tree ценен, когда есть внешние недоверяющие клиенты, которым нужны короткие inclusion/consistency proofs (CT, Go sumdb). У нас проверяет сам оператор/пользователь командой `verify` полным проходом — O(n) по своему журналу приемлемо; Merkle не даёт новой обнаруживаемости по сравнению с цепочкой + anchor, только эффективность доказательств.
- Ни одна схема не защищает от атакующего, владеющего хостом целиком (БД + ключ + anchors на том же диске) — это нужно честно написать в README. Защита усиливается тем, что anchor уходит туда, куда атакующий БД не дотягивается: внешний канал подтверждений (сообщение с head), stdout-логи, отдельный файл/каталог с другими правами.
- Append-only на уровне БД (FR-37) — дополнительный, не криптографический слой: триггеры `BEFORE UPDATE/DELETE → RAISE` (SQLite и Postgres), statement-триггер на `TRUNCATE` в Postgres, в удалённом режиме — роль приложения только с `INSERT, SELECT` на `audit_log`.
- Конкурентная запись: глобальная цепочка сериализует все записи (в SQLite и так один writer; в Postgres — горячая точка на одной строке head); **цепочка per-user** (`chain_id = user_id`, отдельная `system`-цепочка для админ-событий без пользователя) параллелится между пользователями, пользователь может проверить и экспортировать свой журнал, не видя хэшей чужих записей (FR-33, FR-38). Сериализация внутри цепочки: строка `audit_heads(chain_id, last_seq, last_hash)` под `FOR UPDATE` (PG) / `BEGIN IMMEDIATE` (SQLite) плюс `UNIQUE(chain_id, seq)` как страховка — гонка двух писателей на одном seq закончится unique violation и повтором, а не форком цепочки. Минус per-user: удаление всей цепочки пользователя обнаруживается только через anchor со списком (chain_id, seq, head) — значит anchor должен включать все цепочки (или корень над их head).
- Хэшируемый материал записи: `chain_id, seq, ts, event_type, actor, payload (canonical JSON, Decimal строками, без float), prev_mac, key_version`. Запись журнала — в той же транзакции, что и изменение состояния (создание pending, решение, отзыв), чтобы не было состояния без записи; для обращений к бирже — запись «attempt» до отправки и «outcome» после (согласуется с FR-27: «результат неизвестен» тоже исход).

### 3.6 Шифрование ключей биржи (FR-31)
| Вариант | Плюсы | Минусы |
|---|---|---|
| Fernet / MultiFernet | проще всего, встроенная ротация (`rotate()`) | нет associated data → зашифрованный ключ пользователя A можно скопировать в строку пользователя B прямо в БД, и он расшифруется; AES-128-CBC; открытый timestamp |
| AES-256-GCM с AAD, master key из окружения, версия ключа в строке | AAD = `credential_id ‖ user_id ‖ exchange` привязывает шифротекст к строке → подмена между пользователями даёт `InvalidTag`; стандартный AEAD | nonce нужно генерировать случайно и никогда не повторять; ротация — CLI-команда перешифровки всех строк |
| Envelope encryption: per-credential DEK (AES-GCM), DEK обёрнут KEK из env/KMS | ротация KEK = перешифровка только DEK; естественный путь к KMS/Vault | лишний уровень для единиц ключей и без KMS — преждевременная абстракция в v1 |

Подмена шифротекста — реальный риск именно в этом домене: агент пользователя B начал бы торговать на аккаунте A. Поэтому AEAD с AAD важнее, чем envelope. Ключ: один master secret из окружения (`pydantic-settings`, `SecretStr`), из него через HKDF выводятся отдельные подключи для шифрования credentials и для HMAC журнала (key separation, одна переменная окружения для «одной команды»). Ротация: keyring `{version: key}`, новая версия — для записи, старые — для чтения; CLI `rotate-keys` перешифровывает credentials; для журнала старые HMAC-ключи сохраняются для verify (записи не перешифровываются — журнал неизменяем). Локальный режим: master key генерируется командой `init` в файл с правами 0600 вне каталога БД, либо в OS keychain (библиотека `keyring` — не проверялась в этой сессии).

### 3.7 Testcontainers в CI
`testcontainers[postgres]` 4.15.0 с Docker на GitHub Actions ubuntu-раннерах (наличие Docker на раннерах в этой сессии не перепроверялось); образ закрепить (`postgres:18-alpine` или точный minor). Integration-тесты репозиториев параметризовать по двум бэкендам: SQLite (временный файл, не `:memory:` — из-за StaticPool и одной общей коннекции) и Postgres (testcontainers), с маркером, чтобы unit-тесты шли без Docker. Оговорка NFR-2: pull образа — сетевая операция инфраструктуры CI, не обращение к бирже/LLM; при строгом прочтении NFR-2 кэшировать образ или формулировать NFR-2 как «без внешних API».

## 4. Рекомендация

1. **SQLAlchemy 2.1 Core (async) + Alembic, два диалекта: `sqlite+aiosqlite` для локального режима, `postgresql+psycopg` (psycopg 3) для удалённого.** ORM не использовать; raw SQL не использовать. Диалектные различия — в infrastructure: upsert-импорт, `lock_user()` (FOR UPDATE vs опора на `BEGIN IMMEDIATE`), `TypeDecorator` для Decimal-как-TEXT и UTC-времени. SQLite: WAL, `busy_timeout`, `foreign_keys=ON`, write-транзакции через `BEGIN IMMEDIATE`, проверка версии SQLite ≥ 3.35 при старте (fail closed). Порядок: в первых milestones только SQLite, Postgres — в milestone удалённого режима, тогда же матрица integration-тестов.
2. **Без Redis.** Идемпотентность (явный ключ и окно 30 с), FR-46, pending и однократное исполнение — в БД через unique constraints, upsert и условные UPDATE. FR-34/FR-35 — in-memory лимитеры в процессе.
3. **Журнал: per-user HMAC-SHA256-цепочка над canonical JSON + периодический подписанный (HMAC) checkpoint всех head вне БД + триггеры append-only + команда `verify`.** Merkle tree / Tessera — non-goal v1, упомянуть в README как следующий шаг (внешние witnesses). В README — честная таблица «что обнаруживается»: изменение/удаление/вставка — да; усечение хвоста после последнего checkpoint — нет; атакующий с ключом и anchors — нет.
4. **Ключи биржи: AES-256-GCM с AAD (credential_id, user_id, exchange), случайный 96-битный nonce, версионированный master key из окружения, HKDF-подключи для шифрования и HMAC журнала; ротация — CLI.** Envelope encryption — только при появлении KMS.

Главные риски: (а) SQLite `database is locked` при нескольких stdio-процессах — смягчается WAL + IMMEDIATE + busy_timeout, нужен тест с двумя процессами; (б) расхождение поведения диалектов — смягчается общей матрицей integration-тестов; (в) потеря master key = потеря ключей биржи и невозможность verify журнала — документировать backup; (г) tamper-evidence только «evident», не «proof» — не обещать больше, чем даёт модель угрозы.

Остаётся неизвестным / проверить в PLAN или тикете:
- Сигнатура `PostgresContainer.get_connection_url()` и поддержка `driver="psycopg"` в 4.15.0 (исходники не открылись).
- Версия SQLite в целевых окружениях (python:3.12-slim образ, macOS/Windows сборки Python) — нужен ≥ 3.35 (RETURNING), желательно ≥ 3.37 (STRICT).
- Порождает ли каждый MCP-клиент свой процесс stdio-сервера и как это стыкуется с удалённым транспортом — смежный research по MCP.
- Поведение SQLAlchemy `with_for_update()` на SQLite (ожидаемо игнорируется — в SQLite нет `FOR UPDATE`) — проверить тестом, не полагаться.
- Куда физически отправлять checkpoint в локальном режиме (файл, внешний канал подтверждений) — зависит от research по внешнему каналу.
- Работа `keyring` в headless/Docker окружении, если выбрать OS keychain для master key.
