# 028: Postgres, матрица тестов, docker-compose
Статус: TODO
Зависит от: 027
Требования: NFR-6, ADR-007, ADR-008 (роли), D-3 (lease)

## Цель
Удалённый режим работает на Postgres: диалект, две роли БД, lease лидера с epoch и LISTEN/NOTIFY; integration-тесты идут на SQLite и Postgres; `docker compose up` поднимает сервер.

## Scope
- `psycopg` 3, `dialect.py` для Postgres (FOR UPDATE, upsert), роли мигратор/приложение (`INSERT, SELECT` на журнал).
- Lease в таблице `leases` (D-3), `LISTEN/NOTIFY` для executor.
- testcontainers в CI; docker-compose (сервер + Postgres), named volumes.

## Non-scope
- Multi-instance (ограничение README).

## Acceptance criteria
- [ ] Integration-матрица зелёная на обеих БД.
- [ ] Integration: роль приложения не может UPDATE/DELETE журнал.
- [ ] Integration: lease переходит при остановке лидера.
- [ ] `docker compose up` + smoke-скрипт `get_ticker` по HTTP (в CI без внешних API).

## Заметки
—
