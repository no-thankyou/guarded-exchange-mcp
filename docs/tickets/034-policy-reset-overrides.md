# 034: Сброс пользовательских ужесточений политики админом
Статус: DONE
Зависит от: 006
Требования: FR-12, FR-44

## Цель
Админ может снять сохранённые ужесточения пользователя (`policy tighten`), не правя БД руками. Сейчас ужесточение необратимо: одна случайная команда `policy tighten --profile read_only` навсегда отключает торговлю (ревью 006, решение пользователя 2026-10-28).

## Scope
- Use case `ResetOverrides` (admin): удаляет все или указанные поля переопределений пользователя; эффективная политика = потолок админа. Под `lock_user`, write-UoW.
- CLI: `admin policy reset-overrides --user <name> [--field <name> ...]` с явным подтверждением (`--yes` для неинтерактивного режима).
- Журнал: событие `policy_overrides_reset` в цепочке пользователя (actor `cli:admin`, список снятых полей, прежние и новые значения).
- Только CLI админа: в MCP команды нет (FR-44), пользовательский `policy` CLI её не получает.
- README, раздел Policy: заменить предупреждение о необратимости на описание команды.

## Non-scope
- Ослабление потолка (уже есть `admin policy set-ceiling`).
- Сброс kill switch (015).

## Acceptance criteria
- [x] Unit: после `ResetOverrides` эффективная политика равна потолку; сброс одного поля не трогает остальные; сброс у пользователя без переопределений — no-op без записи в журнал. — `tests/unit/test_policy_use_cases.py::test_reset_of_all_overrides_makes_the_effective_policy_the_ceiling` (все 13 полей effective == ceiling, профиль снова `trade`, `lock_user`, write-UoW), `::test_reset_of_one_field_keeps_the_other_user_settings` (+ payload журнала), `::test_reset_without_matching_user_settings_is_a_no_op_without_a_journal_entry[all|field-not-set]`, `::test_reset_for_a_user_without_a_policy_row_is_a_no_op`, `::test_reset_keeps_a_default_ceiling_default`; домен — `tests/unit/test_policy_domain.py::test_without_fields_unsets_only_the_named_overrides`
- [x] Unit: неизвестное поле / неизвестный пользователь → понятная ошибка, ничего не меняется. — `tests/unit/test_policy_use_cases.py::test_reset_with_an_unknown_field_or_user_fails_clearly_and_changes_nothing` (4 случая: неизвестное поле рядом с известным, пустой список, неизвестный пользователь со списком и без), `tests/unit/test_policy_domain.py::test_policy_field_names_parse_from_text_and_unknown_ones_are_listed`, CLI — `tests/unit/test_cli.py::test_reset_overrides_rejects_unknown_fields_and_needs_a_user` (exit 2 до обращения к БД)
- [x] Integration: событие `policy_overrides_reset` в журнале пользователя с полями и actor `cli:admin`; CLI без `--yes` и без ответа на stdin отказывает (ответ из pipe считается ответом, как в `keys delete`). — `tests/integration/test_policy_storage.py::test_cli_admin_reset_overrides_restores_trading_and_journals_it` (реальная SQLite через composition root: без `--yes` и без stdin → exit 1, журнал не тронут; неизвестное поле → exit 2, неизвестный пользователь → exit 1; сброс `profile` → торговля вернулась, `max_leverage` остался; точный payload события; повторный сброс — без записи); unit — `tests/unit/test_cli.py::test_reset_overrides_without_yes_and_without_a_terminal_refuses`, `::test_reset_overrides_shows_what_it_removes_and_asks_first`
- [x] E2E: реестр MCP по-прежнему не содержит инструментов изменения политики. — `tests/e2e/test_mcp_policy.py::test_the_registry_has_no_tool_that_changes_the_policy` (в шаблон имён добавлено `reset`), новый `::test_mcp_tools_cannot_reach_any_policy_changing_use_case` (в `UseCases` MCP нет `SetCeiling`/`TightenPolicy`/`ResetOverrides`); пользовательский `policy` CLI — `tests/unit/test_cli.py::test_reset_overrides_is_an_admin_command_only`

## Заметки
Решения (2026-10-28, исполнитель):
- **Domain:** `parse_policy_fields` (текст → имена полей; `max-leverage` = `max_leverage`; неизвестные → `InvalidParams` со списком допустимых) и `without_fields` (снять поля из `UserOverrides`, `UNSET`). Миграций нет: хранение то же (`policies.overrides`).
- **Application `ResetOverrides(uow, clock)(user_name, fields=None) -> OverridesReset(view, reset_fields)`:** имена полей проверяются до открытия транзакции; дальше write-UoW, `lock_user`, read-modify-write строки. Снимаются только реально заданные поля (порядок — порядок полей политики); если таких нет — `reset_fields == ()`, ни записи политики, ни события в журнале. Потолок сохраняется как был (дефолтный остаётся `NULL`, а не замороженной копией). Пустой список полей `[]` — `InvalidParams` (явное «ничего» скорее ошибка; «всё» — это `None`).
- **Журнал `policy_overrides_reset`** (actor `cli:admin`, цепочка пользователя): `fields`, `removed` (снятые значения пользователя), `effective_before` / `effective_after` — только по снятым полям. Полную эффективную политику не пишу: она восстанавливается из предыдущих событий, а diff читается проще.
- **Отозванный пользователь:** админу сброс разрешён (как и `set-ceiling`), в отличие от `tighten` — тест `test_admin_may_reset_a_revoked_users_settings`.
- **CLI:** `--user` **обязателен** (в отличие от `set-ceiling`, где по умолчанию локальный пользователь): команда ослабляет эффективную политику, пусть цель будет явной. Перед вопросом команда через `policy show` печатает, что будет снято (`user setting of X to remove: field = value`); нечего снимать → сообщение и exit 0 без вопроса и без вызова сброса. Подтверждение `typer.confirm(default=False)`; «нет», конец ввода или отсутствие терминала → stderr `not confirmed: nothing changed (pass --yes to skip the question)`, exit 1. Trade-off: при `--yes` тоже делается чтение `show` (лишняя read-транзакция, зато неизвестный пользователь падает до вопроса в интерактивном режиме и логика одна). Конвейер `echo y | ...` подтверждает — это сознательно (как у `keys delete`); строгий «только TTY» не делал.
- **Эффективная политика после сброса = потолок** с одной оговоркой из 007: read-only ключ биржи по-прежнему ограничивает профиль (`cap_profile`), это не переопределение пользователя.

Предложения (вне scope):
- Журналировать отказанный/неподтверждённый сброс не нужно (ничего не изменилось), но для аудита действий админа можно позже добавить отдельную цепочку `admin` (сейчас события админа пишутся в цепочку пользователя).
- `policy show` мог бы помечать поля, где настройка пользователя строже потолка, подсказкой `reset-overrides --field ...`.

Ревью, раунд 1 (APPROVE, 4 minor исправлены оркестратором 2026-10-29):
- m1. Интерактивный сброс без `--field` снимает только показанные и подтверждённые поля (`frozenset(to_reset)`), а не «всё»: ужесточение, сделанное, пока вопрос открыт, остаётся — `tests/unit/test_cli.py::test_reset_overrides_after_the_question_resets_only_the_listed_settings`. `--yes` без `--field` по-прежнему передаёт `None`.
- m2. Тест сброса всех полей сравнивает потолок со снимком до сброса, а не с view из той же записи.
- m3. README и AC3 уточнены: отказ без `--yes` — когда на stdin нет ответа; `y` из pipe — ответ (как в `keys delete`).
- m4. `parse_policy_fields`: `details={"field": "fields"}` (имя параметра use case).
- К обсуждению: SPEC FR-12 / роль «Админ» не упоминает сброс ужесточений — пользователь решил SPEC не менять (2026-10-29), основание — решение пользователя из ревью 006 в «Цели»; правило «ослабление политики не подтверждает pending» — записано в тикет 017.
