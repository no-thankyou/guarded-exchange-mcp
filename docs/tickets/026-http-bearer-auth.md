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
- Из 006 (ревью): проверить на Streamable HTTP, валидирует ли SDK аргументы по `inputSchema` скрытого инструмента (параметры `Mcp-Param`/`_tool_input_schema`, который «вызывается до middleware и находит скрытые инструменты») **до** `GuardedMCPServer.call_tool`, где стоит проверка прав. Если да — запрещённый пользователь получит протокольную ошибку валидации вместо `permission_denied` и узнает схему скрытого инструмента. Закрыть тестом по HTTP и при необходимости отказом раньше (до валидации) или одинаковым ответом.
