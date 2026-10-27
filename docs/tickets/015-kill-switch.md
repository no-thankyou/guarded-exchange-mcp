# 015: Kill switch
Статус: TODO
Зависит от: 011
Требования: FR-16, FR-44

## Цель
Пользователь (через MCP или CLI) и админ (CLI) включают kill switch: все действия, увеличивающие риск, запрещены, снижающие и чтение работают; снять — только через CLI, admin kill switch снимает только админ.

## Scope
- MCP `activate_kill_switch` (без rate limit, D-16); CLI `kill-switch on/off`, `admin kill-switch on/off`.
- Включение берёт `lock_user` (для TOCTOU с executor, D-8); событие в журнал.
- Проверка в `submit_action` для INCREASES; спот-ордер — всегда INCREASES (решение пользователя: строго).

## Non-scope
- Отмена pending при включении (018/017).

## Acceptance criteria
- [ ] Unit: под kill switch place_order (не reduce-only), увеличение плеча, amend в большую сторону → `kill_switch_active`; отмена, reduce-only, закрытие, SL/TP → разрешены.
- [ ] Unit: пользователь не может снять admin kill switch; через MCP снять нельзя вовсе (проверка реестра).
- [ ] E2E: агент включает kill switch → следующий place_order отклонён.

## Заметки
—
