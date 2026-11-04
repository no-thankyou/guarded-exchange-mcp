"""Output schemas of the trading tools: the action's own answer, or the pending confirmation any of them
may return instead (FR-47 (b)). The SDK validates a tool's result against its declared output model, and
a plain union would be wrapped in ``{"result": ...}``; a root model keeps the answer unwrapped."""

from pydantic import ConfigDict, RootModel

from guarded_exchange.interface.mcp.schemas import PlaceOrderOut
from guarded_exchange.interface.mcp.schemas_confirmation import ConfirmationOut
from guarded_exchange.interface.mcp.schemas_order_changes import AmendOrderOut, CancelAllOut, CancelOrderOut
from guarded_exchange.interface.mcp.schemas_position_actions import SetLeverageOut, SetTradingStopOut

_OBJECT = ConfigDict(json_schema_extra={"type": "object"})  # MCP: an output schema is an object schema


class PlaceOrderResponse(RootModel[PlaceOrderOut | ConfirmationOut]):
    """``place_order`` and ``close_position``."""

    model_config = _OBJECT


class CancelOrderResponse(RootModel[CancelOrderOut | ConfirmationOut]):
    model_config = _OBJECT


class AmendOrderResponse(RootModel[AmendOrderOut | ConfirmationOut]):
    model_config = _OBJECT


class CancelAllResponse(RootModel[CancelAllOut | ConfirmationOut]):
    model_config = _OBJECT


class SetTradingStopResponse(RootModel[SetTradingStopOut | ConfirmationOut]):
    model_config = _OBJECT


class SetLeverageResponse(RootModel[SetLeverageOut | ConfirmationOut]):
    model_config = _OBJECT
