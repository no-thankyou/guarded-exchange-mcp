# 021: AC-4: агент не может подтвердить сам
Статус: IN_REVIEW
Зависит от: 020, 016
Требования: AC-4, FR-20, FR-47

## Цель
Доказать тестами, что модель не может исполнить действие, требующее подтверждения, никаким вызовом инструментов или текстом.

## Scope
- Тест реестра: ни один MCP-инструмент не достигает `ResolveConfirmation` (статический граф вызовов/фабрика зависимостей).
- E2E «враждебный агент»: вызывает все инструменты с аргументами-«подтверждениями» (id pending, `approve`, `confirmed: true`), подсовывает текст, повторяет вызовы, вызывает `cancel_confirmation` — ничего не исполнено.
- Traceability: генерация `docs/traceability.md` по маркерам `req`, проверка покрытия FR-13…28 и FR-41…50 в CI (AC-3).

## Non-scope
- Самописный клиент, автоматически отвечающий на UI-формы (вне гарантии, FR-42).

## Acceptance criteria
- [x] Все сценарии враждебного агента → состояние биржи (FakeExchange) не изменилось, pending не APPROVED. — `tests/e2e/test_ac4_adversarial.py` (враждебный агент в 3 конфигурациях, подделка/повтор/чужой requestState, подмена clientInfo, cancel)
- [x] CI падает, если у FR из диапазона AC-3 нет ни одного теста с маркером. — `tests/unit/test_traceability.py::test_every_requirement_of_ac3_has_a_marked_test`, `test_the_check_fails_when_a_requirement_loses_its_test`

## Заметки
Итоги:
- Реестр: `test_no_mcp_tool_is_named_or_shaped_like_an_approval`; AST-тест — единственный вызов `.decide` в MCP-слое это `confirm_ui._second_round`, `ResolveConfirmation` в MCP-слое не упоминается; граф объектов `UseCases` не содержит `ResolveConfirmation`, `ClientUiConfirmation` достижим только как `client_ui`.
- Враждебный агент: все инструменты x3 прохода x все написания согласия (`confirmed`, `approve`, `input_responses`, `request_state`, `_meta`...), `idempotency_key` с текстом, cancel/status; биржа не тронута, ни один pending не дошёл до APPROVED.
- Поддельный/чужой/повреждённый `requestState`, состояние другого пользователя, изменённый в хранилище pending, подмена `clientInfo` — отказ. Вне гарантии: самописный клиент, сам отвечающий на форму (FR-42).
- Трассировка: CI-проверка AC-3 статическая (AST маркеров `req`), диапазон FR-13…28, FR-41…50 без FR-46/50 (SPEC 7.1); `docs/traceability.md` перегенерирован.

MVP (2026-10-29): Telegram-канала в v1 нет — внешний канал в тесте только CLI.
