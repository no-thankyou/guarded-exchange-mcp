# 026: Удалённый режим: Streamable HTTP и bearer-токены
Статус: TODO
Зависит от: 006
Требования: FR-29 (проверка на каждом запросе), FR-33, ADR-004

## Цель
Сервер запускается по Streamable HTTP; админ выпускает пользователю токен (в БД — хэш); principal берётся только из токена; статус пользователя проверяется на каждом запросе.

## Scope
- `streamable_http_app()` за ASGI, `TransportSecuritySettings(allowed_hosts)`, `TokenVerifier`, порт `Authenticator`.
- CLI `admin user create|list`, `admin token issue|revoke`; журнал.
- Все use cases уже `user_id`-scoped; «не найдено» = «чужое».

## Non-scope
- Отзыв с аннулированием pending и AC-2 (027), Postgres (028).

## Acceptance criteria
- [ ] Integration (реальный HTTP): без токена / с неверным / с отозванным → 401; principal не подменяется заголовками и аргументами.
- [ ] Integration: токен хранится только хэшем.

## Заметки
—
