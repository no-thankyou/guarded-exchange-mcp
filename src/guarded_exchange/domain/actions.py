"""Kinds of trading actions (SPEC 4.2, PLAN 5.1).

Only the kinds for now: the policy names them in its confirmation rule (FR-19). The action
objects themselves (``PlaceOrder``, ``CancelOrder``, ...) arrive with the trading tickets (011+).
"""

from enum import StrEnum


class ActionKind(StrEnum):
    PLACE_ORDER = "place_order"
    AMEND_ORDER = "amend_order"
    CANCEL_ORDER = "cancel_order"
    CANCEL_ALL_ORDERS = "cancel_all_orders"
    SET_TRADING_STOP = "set_trading_stop"
    CLOSE_POSITION = "close_position"
    SET_LEVERAGE = "set_leverage"


ALL_ACTION_KINDS = frozenset(ActionKind)
