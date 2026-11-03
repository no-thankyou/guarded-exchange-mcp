# 013: Отмена, отмена всех, изменение ордера
Статус: TODO
Зависит от: 012
Требования: FR-5, FR-21 (данные о внешних), FR-27 (D-21), FR-43 (Amend/Cancel)

## Цель
Агент отменяет ордер, все ордера по инструменту (конкретными id, с пометкой внешних) и изменяет ордер; изменение классифицируется по риску относительно текущего ордера.

## Scope
- MCP: `cancel_order`, `cancel_all_orders`, `amend_order`.
- Cancel-all: снимок списка id открытых ордеров (включая внешние) → отмена конкретных id; на linear — явный `orderFilter`, TP/SL позиции не трогаются.
- Amend: INCREASES при большем объёме или менее выгодной цене (FR-43), иначе REDUCES; лимиты — только для INCREASES.
- FR-27 для cancel/amend по `orderId` (асинхронность: «принято» ≠ «применено», D-21).

## Non-scope
- Подтверждение (016+).

## Acceptance criteria
- [ ] Unit: классификация amend по всем комбинациям объём/цена/сторона.
- [ ] Unit: cancel-all формирует список id и помечает внешние ордера.
- [ ] Integration: cancel-all на linear отправляет `orderFilter`, TP/SL позиции не удаляются (фикстура).
- [ ] Integration: неизвестный исход cancel/amend → разведка по статусу ордера.

## Заметки
- Из ревью 012 (раунд 1): классифицировать коды `amend`/`cancel` по правилу 012 «неизвестно по умолчанию вне документированных семейств» и явно занести в неизвестные (исход возможен, нужна разведка по `orderId`, D-21): spot `170147` «Order cancellation timeout», `170150` «Cancel order failed», `170191` «Can not cancel order, please try again later», `170212` «Cancel order request processing», `170310` «Order modification timeout», `170311` «Order modification failed»; linear `110079` «The order is processing and can not be operated, please try again later». Сверено с https://bybit-exchange.github.io/docs/v5/error 2026-11-03.
