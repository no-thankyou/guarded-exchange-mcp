"""Bybit V5 response DTOs and their mapping to domain objects.

DTOs mirror the wire format (camelCase strings) and never leave this package: the rest of
the system only sees ``guarded_exchange.domain`` types (ENGINEERING.md, PLAN ADR-002).
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from guarded_exchange.domain.account import (
    AccountBalances,
    CoinBalance,
    ConditionalKind,
    Order,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    Page,
    Position,
    PositionSide,
    QtyUnit,
    TimeInForce,
    UntrustedText,
)
from guarded_exchange.domain.credentials import KeyPermissions
from guarded_exchange.domain.errors import (
    ExchangeRejected,
    ExchangeUnavailable,
    InvalidParams,
    NotFound,
    UnsupportedPositionMode,
)
from guarded_exchange.domain.history import ExecutionKind, HistoryPage, Trade
from guarded_exchange.domain.instruments import Category, Instrument, InstrumentRules, InstrumentStatus
from guarded_exchange.domain.market import (
    Candle,
    CandleInterval,
    Candles,
    Orderbook,
    OrderbookLevel,
    Ticker,
)
from guarded_exchange.infrastructure.bybit.errors import unavailable

_HUNDRED = Decimal(100)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Envelope(BaseModel):
    """Common V5 response wrapper: ``{retCode, retMsg, result, time}``."""

    model_config = ConfigDict(extra="ignore")

    ret_code: int = Field(alias="retCode")
    ret_msg: str = Field(default="", alias="retMsg")
    result: dict[str, Any] = Field(default_factory=dict)
    time_ms: int | None = Field(default=None, alias="time")


class TickerDTO(BaseModel):
    """One item of ``GET /v5/market/tickers`` (spot and linear share most fields)."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    last_price: str = Field(alias="lastPrice")
    bid1_price: str = Field(default="", alias="bid1Price")
    ask1_price: str = Field(default="", alias="ask1Price")
    high_price_24h: str = Field(default="", alias="highPrice24h")
    low_price_24h: str = Field(default="", alias="lowPrice24h")
    price_24h_pcnt: str = Field(default="", alias="price24hPcnt")
    volume_24h: str = Field(default="", alias="volume24h")
    turnover_24h: str = Field(default="", alias="turnover24h")
    mark_price: str = Field(default="", alias="markPrice")
    index_price: str = Field(default="", alias="indexPrice")
    funding_rate: str = Field(default="", alias="fundingRate")
    next_funding_time: str = Field(default="", alias="nextFundingTime")


class TickersResultDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: str = ""
    items: list[TickerDTO] = Field(default_factory=list, alias="list")


def parse_envelope(payload: object) -> Envelope:
    # The error is raised outside ``except``: a pydantic error quotes the input, which for private
    # endpoints may contain key material (``query-api`` returns ``apiKey`` and ``secret``), and it
    # must not survive as ``__cause__`` or ``__context__`` of what we raise.
    envelope = _validated(Envelope, payload)
    if envelope is None:
        raise unavailable("unexpected response format")
    return envelope


def _validated[M: BaseModel](model: type[M], data: object) -> M | None:
    """``model.model_validate(data)`` or ``None``; the ``ValidationError`` (which quotes the input)
    is dropped here so callers can raise their own error without any link to it."""
    try:
        return model.model_validate(data)
    except ValidationError:
        return None


def ticker_from_result(
    result: dict[str, Any], *, symbol: str, category: Category, time_ms: int | None
) -> Ticker:
    """Map the ``result`` of a successful tickers call to a domain ``Ticker``."""
    try:
        parsed = TickersResultDTO.model_validate(result)
    except ValidationError as exc:
        raise unavailable("unexpected tickers format") from exc
    item = next((i for i in parsed.items if i.symbol == symbol), None)
    if item is None:
        raise NotFound(
            f"no {category.value} instrument {symbol}",
            next_step="Check the symbol and category; spot and linear list different instruments.",
            details={"symbol": symbol, "category": category.value},
        )
    try:
        return _ticker(item, category=category, time_ms=time_ms)
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("unparsable number in tickers response") from exc


def _ticker(item: TickerDTO, *, category: Category, time_ms: int | None) -> Ticker:
    last_price = _dec(item.last_price)
    if last_price is None:
        raise ExchangeUnavailable(
            f"the exchange returned no last price for {item.symbol}",
            next_step="Retry later; the instrument may not be trading right now.",
        )
    observed_at = _ts(time_ms)
    if observed_at is None:
        raise unavailable("response without a server timestamp")
    linear = category is Category.LINEAR
    change = _dec(item.price_24h_pcnt)
    funding = _dec(item.funding_rate) if linear else None
    return Ticker(
        symbol=item.symbol,
        category=category,
        last_price=last_price,
        bid_price=_dec(item.bid1_price),
        ask_price=_dec(item.ask1_price),
        high_24h=_dec(item.high_price_24h),
        low_24h=_dec(item.low_price_24h),
        change_24h_pct=None if change is None else change * _HUNDRED,
        volume_24h_base=_dec(item.volume_24h),
        turnover_24h_quote=_dec(item.turnover_24h),
        observed_at=observed_at,
        mark_price=_dec(item.mark_price) if linear else None,
        index_price=_dec(item.index_price) if linear else None,
        funding_rate_pct=None if funding is None else funding * _HUNDRED,
        next_funding_at=_ts(_int(item.next_funding_time)) if linear else None,
    )


def _dec(raw: str) -> Decimal | None:
    """Bybit sends numbers as strings and ``""`` for "not applicable"; never go through float."""
    raw = raw.strip()
    if not raw:
        return None
    value = Decimal(raw)
    if not value.is_finite():
        raise ValueError(f"non-finite number: {raw!r}")
    return value


def _required_dec(raw: str) -> Decimal:
    value = _dec(raw)
    if value is None:
        raise ValueError("missing number")
    return value


def _int(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw else None


def _ts(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return _EPOCH + timedelta(milliseconds=ms)


# --- order book ------------------------------------------------------------------------------


class OrderbookResultDTO(BaseModel):
    """``result`` of ``GET /v5/market/orderbook``: levels are ``[price, size]`` string pairs,
    bids highest first, asks lowest first; ``ts`` is when the book snapshot was generated (ms)."""

    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(alias="s")
    bids: list[tuple[str, str]] = Field(alias="b")
    asks: list[tuple[str, str]] = Field(alias="a")
    ts: int | None = None


def orderbook_from_result(result: dict[str, Any], *, symbol: str, category: Category) -> Orderbook:
    try:
        parsed = OrderbookResultDTO.model_validate(result)
    except ValidationError as exc:
        raise unavailable("unexpected orderbook format") from exc
    if parsed.symbol != symbol:
        raise unavailable("orderbook for a different symbol")
    observed_at = _ts(parsed.ts)
    if observed_at is None:
        raise unavailable("orderbook without a timestamp")
    try:
        return Orderbook(
            symbol=symbol,
            category=category,
            bids=tuple(_level(price, qty) for price, qty in parsed.bids),
            asks=tuple(_level(price, qty) for price, qty in parsed.asks),
            observed_at=observed_at,
        )
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("inconsistent orderbook levels") from exc


def _level(price: str, qty: str) -> OrderbookLevel:
    return OrderbookLevel(price=_required_dec(price), qty=_required_dec(qty))


# --- candles ---------------------------------------------------------------------------------

KLINE_INTERVALS: Final[dict[CandleInterval, str]] = {
    CandleInterval.M1: "1",
    CandleInterval.M3: "3",
    CandleInterval.M5: "5",
    CandleInterval.M15: "15",
    CandleInterval.M30: "30",
    CandleInterval.H1: "60",
    CandleInterval.H2: "120",
    CandleInterval.H4: "240",
    CandleInterval.H6: "360",
    CandleInterval.H12: "720",
    CandleInterval.D1: "D",
    CandleInterval.W1: "W",
    CandleInterval.MO1: "M",
}
"""Domain interval -> Bybit ``interval`` parameter (https://bybit-exchange.github.io/docs/v5/enum)."""

KlineRow = tuple[str, str, str, str, str, str, str]


class KlineResultDTO(BaseModel):
    """``result`` of ``GET /v5/market/kline``: newest first, each row
    ``[startTime, open, high, low, close, volume, turnover]`` as strings."""

    model_config = ConfigDict(extra="ignore")

    symbol: str = ""
    rows: list[KlineRow] = Field(default_factory=list, alias="list")


def candles_from_result(
    result: dict[str, Any], *, symbol: str, category: Category, interval: CandleInterval
) -> Candles:
    try:
        parsed = KlineResultDTO.model_validate(result)
    except ValidationError as exc:
        raise unavailable("unexpected kline format") from exc
    if parsed.symbol != symbol:
        raise unavailable("candles for a different symbol")
    try:
        candles = tuple(_candle(row) for row in reversed(parsed.rows))  # Bybit: newest first
        return Candles(symbol=symbol, category=category, interval=interval, candles=candles)
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("inconsistent candles") from exc


def _candle(row: KlineRow) -> Candle:
    start_ms, open_, high, low, close, volume, turnover = row
    start_at = _ts(_int(start_ms))
    if start_at is None:
        raise ValueError("candle without a start time")
    return Candle(
        start_at=start_at,
        open=_required_dec(open_),
        high=_required_dec(high),
        low=_required_dec(low),
        close=_required_dec(close),
        volume_base=_required_dec(volume),
        turnover_quote=_required_dec(turnover),
    )


# --- instrument rules ------------------------------------------------------------------------

_COIN_RE: Final = re.compile(r"^[A-Z0-9]{1,20}$")
_LINEAR_PERPETUAL: Final = "LinearPerpetual"
_STATUSES: Final[dict[str, InstrumentStatus]] = {
    "PreLaunch": InstrumentStatus.PRE_LAUNCH,
    "PendingOpen": InstrumentStatus.PENDING_OPEN,
    "Trading": InstrumentStatus.TRADING,
    "Delivering": InstrumentStatus.SETTLING,
    "Closed": InstrumentStatus.CLOSED,
}
"""Bybit instrument ``status`` enum (https://bybit-exchange.github.io/docs/v5/enum); anything else
maps to ``UNKNOWN`` and therefore to "not trading"."""


class _PriceFilterDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tick_size: str = Field(alias="tickSize")


class _LotSizeFilterDTO(BaseModel):
    """Linear: ``qtyStep``, ``minOrderQty``, ``minNotionalValue``. Spot: ``basePrecision`` is the
    quantity step, ``minOrderAmt`` the minimum order value; spot ``minOrderQty`` is deprecated."""

    model_config = ConfigDict(extra="ignore")

    qty_step: str = Field(default="", alias="qtyStep")
    min_order_qty: str = Field(default="", alias="minOrderQty")
    min_notional_value: str = Field(default="", alias="minNotionalValue")
    base_precision: str = Field(default="", alias="basePrecision")
    min_order_amt: str = Field(default="", alias="minOrderAmt")


class _LeverageFilterDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_leverage: str = Field(alias="maxLeverage")


class InstrumentInfoDTO(BaseModel):
    """One item of ``GET /v5/market/instruments-info``; spot has no ``leverageFilter``/``contractType``."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    status: str
    base_coin: str = Field(alias="baseCoin")
    quote_coin: str = Field(alias="quoteCoin")
    contract_type: str = Field(default="", alias="contractType")
    price_filter: _PriceFilterDTO = Field(alias="priceFilter")
    lot_size_filter: _LotSizeFilterDTO = Field(alias="lotSizeFilter")
    leverage_filter: _LeverageFilterDTO | None = Field(default=None, alias="leverageFilter")


class InstrumentsResultDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[InstrumentInfoDTO] = Field(default_factory=list, alias="list")


def rules_from_result(result: dict[str, Any], *, symbol: str, category: Category) -> InstrumentRules:
    try:
        parsed = InstrumentsResultDTO.model_validate(result)
    except ValidationError as exc:
        raise unavailable("unexpected instruments-info format") from exc
    item = next((i for i in parsed.items if i.symbol == symbol), None)
    if item is None:
        raise NotFound(
            f"no {category.value} instrument {symbol}",
            next_step="Check the symbol and category; spot and linear list different instruments.",
            details={"symbol": symbol, "category": category.value},
        )
    if category is Category.LINEAR and item.contract_type != _LINEAR_PERPETUAL:
        # Domain `linear` means a perpetual: dated futures expire, which v1 does not model.
        raise InvalidParams(
            f"{symbol} is not a perpetual contract; only perpetuals are supported in linear",
            next_step="Use a USDT perpetual symbol, e.g. BTCUSDT.",
            details={"field": "symbol", "reason": "not_perpetual"},
        )
    if not (_COIN_RE.fullmatch(item.base_coin) and _COIN_RE.fullmatch(item.quote_coin)):
        # Coin codes end up in our own messages (e.g. "quoted in BTC"): only plain codes pass.
        raise unavailable("unexpected coin code in instruments-info")
    try:
        return _rules(item, category=category)
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("unparsable instrument rules") from exc


def _rules(item: InstrumentInfoDTO, *, category: Category) -> InstrumentRules:
    lot = item.lot_size_filter
    max_leverage: Decimal | None
    if category is Category.LINEAR:
        if item.leverage_filter is None:
            raise ValueError("linear instrument without leverageFilter")
        qty_step = _required_dec(lot.qty_step)
        min_qty = _required_dec(lot.min_order_qty)
        min_notional = _dec(lot.min_notional_value)
        max_leverage = _required_dec(item.leverage_filter.max_leverage)
    else:
        qty_step = _required_dec(lot.base_precision)
        # Spot `minOrderQty` is deprecated (docs v5/market/instrument): the real floor is the order
        # value `minOrderAmt`, quantity only has to be one step.
        min_qty = qty_step
        min_notional = _dec(lot.min_order_amt)
        max_leverage = None  # v1 spot is cash only
    return InstrumentRules(
        instrument=Instrument(
            symbol=item.symbol, category=category, base=item.base_coin, quote=item.quote_coin
        ),
        status=_STATUSES.get(item.status, InstrumentStatus.UNKNOWN),
        tick_size=_required_dec(item.price_filter.tick_size),
        qty_step=qty_step,
        min_qty=min_qty,
        min_notional=min_notional,
        max_leverage=max_leverage,
    )


# --- API key permissions (query-api) ----------------------------------------------------------

_PERMISSION_PART_RE: Final = re.compile(r"^[A-Za-z0-9_]{1,64}$")
UNRECOGNISED_PERMISSION: Final = "<unrecognised>"
_NO_IP_RESTRICTION: Final = frozenset({"*", ""})


class QueryApiDTO(BaseModel):
    """The fields of ``GET /v5/user/query-api`` this server needs; nothing else is ever parsed.

    The response also carries ``apiKey`` and ``secret`` (spike 004): ``extra="ignore"`` drops them
    and nothing here copies them anywhere. https://bybit-exchange.github.io/docs/v5/user/apikey-info
    """

    model_config = ConfigDict(extra="ignore")

    read_only: int = Field(alias="readOnly")
    permissions: dict[str, list[str]]
    ips: list[str] = Field(default_factory=list)
    expired_at: str = Field(default="", alias="expiredAt")


def key_permissions_from_result(result: dict[str, Any]) -> KeyPermissions:
    """``readOnly`` 0/1, permission groups (empty group = no permission), IP whitelist, expiry.

    A permission or group name with unexpected characters becomes ``<unrecognised>``: it is
    never on the allowlist, so the key is refused rather than "cannot be checked".
    ``expiredAt`` of ``1970-01-01T00:00:00Z`` (the documented value for keys bound to an IP) or
    empty means the key does not expire. ``ips`` empty or ``["*"]`` means no IP whitelist.
    """
    parsed = _validated(QueryApiDTO, result)  # never chained: the input holds the secret
    if parsed is None:
        raise unavailable("unexpected query-api format")
    if parsed.read_only not in (0, 1):
        raise unavailable("unexpected readOnly value in query-api")
    names = frozenset(
        _permission(group, name) for group, members in parsed.permissions.items() for name in members
    )
    return KeyPermissions(
        read_only=parsed.read_only == 1,
        permissions=names,
        expires_at=_expiry(parsed.expired_at),
        ip_restricted=not set(parsed.ips) <= _NO_IP_RESTRICTION,
    )


def _permission(group: str, name: str) -> str:
    if _PERMISSION_PART_RE.fullmatch(group) and _PERMISSION_PART_RE.fullmatch(name):
        return f"{group}.{name}"
    return UNRECOGNISED_PERMISSION


def _expiry(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    value = _iso_time(raw)
    if value is None:
        raise unavailable("unparsable expiredAt in query-api")
    if value.tzinfo is None:
        raise unavailable("expiredAt without a time zone in query-api")
    return None if value <= _EPOCH else value.astimezone(UTC)


def _iso_time(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# --- account: wallet balance, positions, open orders (private, signed) -----------------------

UNIFIED_ACCOUNT: Final = "UNIFIED"
USDT_SETTLE: Final = "USDT"
CLIENT_ORDER_ID_PREFIX: Final = "gx-"
"""Prefix of every ``orderLinkId`` this system sends (D-26: the format belongs to the adapter).

A fast filter only: anyone with the account can choose the same prefix. Confirmation against our
own records of sent orders arrives with trading (tickets 011/012)."""

_MAX_CLIENT_ORDER_ID: Final = 64
_ORDER_ID_RE: Final = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SYMBOL_RE: Final = re.compile(r"^[A-Z0-9]{2,30}$")
_ONE_WAY: Final = 0


class _CoinDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    coin: str
    wallet_balance: str = Field(alias="walletBalance")
    equity: str = ""
    locked: str = ""
    unrealised_pnl: str = Field(default="", alias="unrealisedPnl")
    usd_value: str = Field(default="", alias="usdValue")


class _WalletDTO(BaseModel):
    """One account of ``GET /v5/account/wallet-balance``; totals are in USD."""

    model_config = ConfigDict(extra="ignore")

    account_type: str = Field(alias="accountType")
    total_equity: str = Field(default="", alias="totalEquity")
    total_wallet_balance: str = Field(default="", alias="totalWalletBalance")
    total_available_balance: str = Field(default="", alias="totalAvailableBalance")
    coins: list[_CoinDTO] = Field(default_factory=list, alias="coin")


class _WalletResultDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    accounts: list[_WalletDTO] = Field(alias="list")


def balances_from_result(result: dict[str, Any], *, time_ms: int | None) -> AccountBalances:
    """https://bybit-exchange.github.io/docs/v5/account/wallet-balance (checked 2026-10-28).

    Per-coin ``availableToWithdraw``/``free``/``availableToBorrow`` are deprecated for unified
    accounts and not read; what can still be used for new margin is the account-level
    ``totalAvailableBalance`` (USD).
    """
    parsed = _validated(_WalletResultDTO, result)
    if parsed is None:
        raise unavailable("unexpected wallet-balance format")
    account = next((a for a in parsed.accounts if a.account_type == UNIFIED_ACCOUNT), None)
    if account is None:
        raise unavailable("wallet-balance without a unified account")
    observed_at = _ts(time_ms)
    if observed_at is None:
        raise unavailable("wallet-balance without a server timestamp")
    if not all(_COIN_RE.fullmatch(c.coin) for c in account.coins):
        # Coin codes are shown as plain text, so an unexpected one is not passed on (FR-41). The
        # code is not quoted here either. Retrying returns the same holding: not "unavailable".
        raise ExchangeRejected(
            "the account holds an asset whose code this server cannot display",
            next_step="Do not retry. Tell the user the balances could not be shown and to check them "
            "on the exchange.",
            details={"reason": "unsupported_coin_code"},
        )
    try:
        return AccountBalances(
            total_equity_usd=_dec(account.total_equity),
            total_wallet_balance_usd=_dec(account.total_wallet_balance),
            total_available_usd=_dec(account.total_available_balance),
            coins=tuple(
                CoinBalance(
                    coin=c.coin,
                    wallet_balance=_required_dec(c.wallet_balance),
                    equity=_dec(c.equity),
                    locked=_dec(c.locked),
                    unrealised_pnl=_dec(c.unrealised_pnl),
                    usd_value=_dec(c.usd_value),
                )
                for c in account.coins
            ),
            observed_at=observed_at,
        )
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("unparsable number in wallet-balance") from exc


class _PositionDTO(BaseModel):
    """One item of ``GET /v5/position/list`` (linear). ``side`` ``""`` and ``size`` 0: no position."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    position_idx: int = Field(alias="positionIdx")
    side: str
    size: str
    avg_price: str = Field(default="", alias="avgPrice")
    mark_price: str = Field(default="", alias="markPrice")
    liq_price: str = Field(default="", alias="liqPrice")
    unrealised_pnl: str = Field(default="", alias="unrealisedPnl")
    position_value: str = Field(default="", alias="positionValue")
    leverage: str = ""
    take_profit: str = Field(default="", alias="takeProfit")
    stop_loss: str = Field(default="", alias="stopLoss")
    updated_time: str = Field(default="", alias="updatedTime")


class _PositionsResultDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[_PositionDTO] = Field(alias="list")


_POSITION_SIDES: Final = {"Buy": PositionSide.LONG, "Sell": PositionSide.SHORT}


def positions_from_result(result: dict[str, Any], *, symbol: str | None, limit: int) -> Page[Position]:
    """Open positions of a ``limit + 1`` request: the extra row only tells whether there are more.

    https://bybit-exchange.github.io/docs/v5/position (checked 2026-10-28). A position in hedge
    mode (``positionIdx`` 1 or 2) refuses the whole read (D-26): showing half of a hedged book
    as one-way positions would mislead.
    """
    parsed = _validated(_PositionsResultDTO, result)
    if parsed is None:
        raise unavailable("unexpected position/list format")
    if any(p.position_idx != _ONE_WAY for p in parsed.items):
        raise UnsupportedPositionMode()
    if symbol and any(p.symbol != symbol for p in parsed.items):
        raise unavailable("position/list returned another symbol")
    try:
        open_rows = [p for p in parsed.items if p.side and _required_dec(p.size) != 0]
        supported, omitted = _split_supported(open_rows, limit)
        positions = tuple(_position(p) for p in supported)
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("inconsistent position/list") from exc
    return Page(items=positions, has_more=len(parsed.items) > limit, omitted=omitted)


def _split_supported[R: (_PositionDTO, _OrderDTO)](rows: list[R], limit: int) -> tuple[list[R], int]:
    """The page (the first ``limit`` rows): rows whose symbol v1 can represent, and how many of the
    page's rows were left out. Rows past the page only feed ``has_more``, never ``omitted``.

    A symbol outside ``^[A-Z0-9]{2,30}$`` (e.g. a dated future ``BTCUSDT-26DEC25``) is out of scope
    for v1 and its text is not shown to the agent (FR-41); it is counted instead of dropped silently.
    """
    page = rows[:limit]
    supported = [row for row in page if _SYMBOL_RE.fullmatch(row.symbol)]
    return supported, len(page) - len(supported)


def _position(item: _PositionDTO) -> Position:
    side = _POSITION_SIDES.get(item.side)
    if side is None:
        raise ValueError("unknown position side")
    return Position(
        symbol=item.symbol,
        side=side,
        size=_required_dec(item.size),
        entry_price=_required_dec(item.avg_price),
        mark_price=_dec(item.mark_price),
        liquidation_price=_positive_or_none(item.liq_price),
        unrealised_pnl=_dec(item.unrealised_pnl),
        position_value=_dec(item.position_value),
        leverage=_positive_or_none(item.leverage),
        take_profit=_positive_or_none(item.take_profit),
        stop_loss=_positive_or_none(item.stop_loss),
        updated_at=_ts(_int(item.updated_time)),
    )


def _positive_or_none(raw: str) -> Decimal | None:
    """Prices Bybit reports as ``""`` or ``"0"`` when not set (TP/SL, trigger, market order price)."""
    value = _dec(raw)
    return value if value is not None and value > 0 else None


class _OrderDTO(BaseModel):
    """One item of ``GET /v5/order/realtime``; spot lacks some linear-only fields."""

    model_config = ConfigDict(extra="ignore")

    order_id: str = Field(alias="orderId")
    order_link_id: str = Field(default="", alias="orderLinkId")
    symbol: str
    side: str
    order_type: str = Field(alias="orderType")
    order_status: str = Field(alias="orderStatus")
    qty: str
    cum_exec_qty: str = Field(default="", alias="cumExecQty")
    price: str = ""
    avg_price: str = Field(default="", alias="avgPrice")
    time_in_force: str = Field(default="", alias="timeInForce")
    reduce_only: bool = Field(default=False, alias="reduceOnly")
    stop_order_type: str = Field(default="", alias="stopOrderType")
    trigger_price: str = Field(default="", alias="triggerPrice")
    take_profit: str = Field(default="", alias="takeProfit")
    stop_loss: str = Field(default="", alias="stopLoss")
    position_idx: int = Field(default=_ONE_WAY, alias="positionIdx")
    market_unit: str = Field(default="", alias="marketUnit")
    reject_reason: str = Field(default="", alias="rejectReason")
    created_time: str = Field(alias="createdTime")
    updated_time: str = Field(default="", alias="updatedTime")


class _OrdersResultDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[_OrderDTO] = Field(alias="list")
    next_page_cursor: str = Field(default="", alias="nextPageCursor")


OPEN_ORDERS_PAGE_MAX: Final = 50
"""Bybit's maximum ``limit`` for ``order/realtime``."""

_ORDER_SIDES: Final = {"Buy": OrderSide.BUY, "Sell": OrderSide.SELL}
_ORDER_TYPES: Final = {"Limit": OrderType.LIMIT, "Market": OrderType.MARKET}
_ORDER_STATUSES: Final = {
    "New": OrderStatus.NEW,
    "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
    "Untriggered": OrderStatus.UNTRIGGERED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "PartiallyFilledCanceled": OrderStatus.PARTIALLY_FILLED_CANCELLED,
    "Rejected": OrderStatus.REJECTED,
    "Triggered": OrderStatus.TRIGGERED,
    "Deactivated": OrderStatus.DEACTIVATED,
}
"""https://bybit-exchange.github.io/docs/v5/enum#orderstatus (checked 2026-11-03)."""
_TIME_IN_FORCE: Final = {
    "GTC": TimeInForce.GTC,
    "IOC": TimeInForce.IOC,
    "FOK": TimeInForce.FOK,
    "PostOnly": TimeInForce.POST_ONLY,
}
_CONDITIONAL: Final = {
    "TakeProfit": ConditionalKind.TAKE_PROFIT,
    "PartialTakeProfit": ConditionalKind.TAKE_PROFIT,
    "StopLoss": ConditionalKind.STOP_LOSS,
    "PartialStopLoss": ConditionalKind.STOP_LOSS,
    "TrailingStop": ConditionalKind.TRAILING_STOP,
    "Stop": ConditionalKind.STOP,
}
_NOT_CONDITIONAL: Final = frozenset({"", "UNKNOWN"})


def open_orders_from_result(
    result: dict[str, Any], *, category: Category, symbol: str | None, limit: int
) -> Page[Order]:
    """Orders of a ``min(limit + 1, 50)`` request, newest first (the exchange's order).

    https://bybit-exchange.github.io/docs/v5/order/open-order (checked 2026-10-28). Enum values this
    version does not know become ``unknown``/``other`` instead of passing exchange text through.
    """
    parsed = _validated(_OrdersResultDTO, result)
    if parsed is None:
        raise unavailable("unexpected order/realtime format")
    if category is Category.LINEAR and any(o.position_idx != _ONE_WAY for o in parsed.items):
        raise UnsupportedPositionMode()
    if symbol and any(o.symbol != symbol for o in parsed.items):
        raise unavailable("order/realtime returned another symbol")
    supported, omitted = _split_supported(parsed.items, limit)
    if not all(_ORDER_ID_RE.fullmatch(o.order_id) for o in supported):
        raise unavailable("unexpected order id in order/realtime")  # assigned by the exchange
    try:
        orders = tuple(_order(o, category) for o in supported)
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("inconsistent order/realtime") from exc
    rows = len(parsed.items)
    # A request at the exchange's maximum has no spare row: a full page with a cursor may continue.
    full_last_page = limit >= OPEN_ORDERS_PAGE_MAX and rows >= limit and bool(parsed.next_page_cursor)
    return Page(items=orders, has_more=rows > limit or full_last_page, omitted=omitted)


# --- order history and executions (ticket 010) ----------------------------------------------

ORDER_HISTORY_PAGE_MAX: Final = 50
"""Bybit's maximum ``limit`` for ``order/history`` (``execution/list`` allows 100; we ask for at most 50)."""

_NO_ERROR: Final = "EC_NoError"
_EXCHANGE_CURSOR_RE: Final = re.compile(r"^(?:[A-Za-z0-9_.~-]|%[0-9A-Fa-f]{2}){1,512}$")
_UNESCAPED_IN_CURSOR: Final = "%-._~"


def wire_exchange_cursor(token: str) -> str:
    """The exchange's page cursor as it goes into the query string: sent verbatim (Bybit returns
    it percent-encoded and its SDK sends it back unencoded), so only unreserved characters and
    ``%XX`` escapes pass. ``&``, ``=``, ``#`` and the like cannot reach the query: no parameter
    can be smuggled in through a cursor."""
    if not _EXCHANGE_CURSOR_RE.fullmatch(token):
        raise InvalidParams(
            "cursor is not a cursor this tool returned",
            next_step="Pass next_cursor exactly as returned, or call again without cursor.",
            details={"field": "cursor", "reason": "invalid_cursor"},
        )
    return token


def _next_exchange_cursor(raw: str, *, rows: int, limit: int) -> str | None:
    """The next page's cursor, ``None`` on the last page. Bybit sends a cursor on the last page
    too, so a page shorter than ``limit`` is the last one whatever the cursor says. Characters
    outside the cursor alphabet are percent-encoded (the exchange decodes the query once, so the
    value it gets back is the same)."""
    raw = raw.strip()
    if not raw or rows < limit:
        return None
    token = quote(raw, safe=_UNESCAPED_IN_CURSOR)
    if not _EXCHANGE_CURSOR_RE.fullmatch(token):
        raise unavailable("unexpected page cursor")
    return token


def order_history_from_result(
    result: dict[str, Any], *, category: Category, symbol: str | None, limit: int
) -> HistoryPage[Order]:
    """One page of ``GET /v5/order/history``, newest first.

    https://bybit-exchange.github.io/docs/v5/order/order-list (checked 2026-11-03). Rows v1 cannot
    represent - a symbol outside the plain pattern (dated futures) or a hedge-mode order - are
    counted in ``omitted``: past hedge-mode orders do not make the whole history unreadable.
    """
    parsed = _validated(_OrdersResultDTO, result)
    if parsed is None:
        raise unavailable("unexpected order/history format")
    if symbol and any(o.symbol != symbol for o in parsed.items):
        raise unavailable("order/history returned another symbol")
    if len(parsed.items) > limit:
        raise unavailable("order/history returned more rows than asked")
    supported = [
        o
        for o in parsed.items
        if _SYMBOL_RE.fullmatch(o.symbol) and (category is Category.SPOT or o.position_idx == _ONE_WAY)
    ]
    if not all(_ORDER_ID_RE.fullmatch(o.order_id) for o in supported):
        raise unavailable("unexpected order id in order/history")
    try:
        orders = tuple(_order(o, category) for o in supported)
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("inconsistent order/history") from exc
    rows = len(parsed.items)
    return HistoryPage(
        items=orders,
        next_exchange_cursor=_next_exchange_cursor(parsed.next_page_cursor, rows=rows, limit=limit),
        omitted=rows - len(supported),
    )


class _ExecutionDTO(BaseModel):
    """One item of ``GET /v5/execution/list``."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    order_id: str = Field(alias="orderId")
    order_link_id: str = Field(default="", alias="orderLinkId")
    side: str
    exec_id: str = Field(alias="execId")
    exec_price: str = Field(alias="execPrice")
    exec_qty: str = Field(alias="execQty")
    exec_value: str = Field(default="", alias="execValue")
    exec_fee: str = Field(default="", alias="execFee")
    fee_currency: str = Field(default="", alias="feeCurrency")
    fee_rate: str = Field(default="", alias="feeRate")
    exec_type: str = Field(default="", alias="execType")
    is_maker: bool | None = Field(default=None, alias="isMaker")
    closed_size: str = Field(default="", alias="closedSize")
    exec_time: str = Field(alias="execTime")


class _ExecutionsResultDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[_ExecutionDTO] = Field(alias="list")
    next_page_cursor: str = Field(default="", alias="nextPageCursor")


_EXECUTION_KINDS: Final = {
    "Trade": ExecutionKind.TRADE,
    "BustTrade": ExecutionKind.LIQUIDATION,
    "AdlTrade": ExecutionKind.AUTO_DELEVERAGE,
    "Funding": ExecutionKind.FUNDING,
    "Delivery": ExecutionKind.SETTLEMENT,
    "Settle": ExecutionKind.SETTLEMENT,
    "BlockTrade": ExecutionKind.BLOCK_TRADE,
}
"""https://bybit-exchange.github.io/docs/v5/enum#exectype; any other value is ``other``."""


def trades_from_result(
    result: dict[str, Any], *, category: Category, symbol: str | None, limit: int
) -> HistoryPage[Trade]:
    """One page of ``GET /v5/execution/list``, newest first.

    https://bybit-exchange.github.io/docs/v5/order/execution (checked 2026-11-03). A row with an
    unsupported symbol or an unknown side is counted in ``omitted``, its text is not shown.
    """
    parsed = _validated(_ExecutionsResultDTO, result)
    if parsed is None:
        raise unavailable("unexpected execution/list format")
    if symbol and any(e.symbol != symbol for e in parsed.items):
        raise unavailable("execution/list returned another symbol")
    if len(parsed.items) > limit:
        raise unavailable("execution/list returned more rows than asked")
    supported = [e for e in parsed.items if _SYMBOL_RE.fullmatch(e.symbol) and e.side in _ORDER_SIDES]
    if not all(_ORDER_ID_RE.fullmatch(e.order_id) and _ORDER_ID_RE.fullmatch(e.exec_id) for e in supported):
        raise unavailable("unexpected id in execution/list")
    try:
        trades = tuple(_trade(e, category) for e in supported)
    except (InvalidOperation, ValueError) as exc:
        raise unavailable("inconsistent execution/list") from exc
    rows = len(parsed.items)
    return HistoryPage(
        items=trades,
        next_exchange_cursor=_next_exchange_cursor(parsed.next_page_cursor, rows=rows, limit=limit),
        omitted=rows - len(supported),
    )


def _trade(item: _ExecutionDTO, category: Category) -> Trade:
    executed_at = _ts(_int(item.exec_time))
    if executed_at is None:
        raise ValueError("execution without a time")
    fee_currency = item.fee_currency.strip()
    return Trade(
        trade_id=item.exec_id,
        order_id=item.order_id,
        symbol=item.symbol,
        category=category,
        side=_ORDER_SIDES[item.side],
        kind=_EXECUTION_KINDS.get(item.exec_type.strip(), ExecutionKind.OTHER),
        price=_required_dec(item.exec_price),
        qty=_required_dec(item.exec_qty),
        value=_dec(item.exec_value),
        fee=_dec(item.exec_fee),
        fee_currency=fee_currency if _COIN_RE.fullmatch(fee_currency) else None,
        fee_rate=_dec(item.fee_rate),
        is_maker=item.is_maker,
        closed_size=_positive_or_none(item.closed_size) if category is Category.LINEAR else None,
        executed_at=executed_at,
        origin=order_origin(item.order_link_id),
        client_order_id=_untrusted(item.order_link_id),
    )


def _order(item: _OrderDTO, category: Category) -> Order:
    side = _ORDER_SIDES.get(item.side)
    created_at = _ts(_int(item.created_time))
    if side is None or created_at is None:
        raise ValueError("order without a known side or creation time")
    stop_type = item.stop_order_type.strip()
    link_id = item.order_link_id.strip()  # for display only; the origin is judged on the raw id
    conditional = (
        None if stop_type in _NOT_CONDITIONAL else _CONDITIONAL.get(stop_type, ConditionalKind.OTHER)
    )
    return Order(
        order_id=item.order_id,
        symbol=item.symbol,
        category=category,
        side=side,
        order_type=_ORDER_TYPES.get(item.order_type, OrderType.UNKNOWN),
        status=_ORDER_STATUSES.get(item.order_status, OrderStatus.UNKNOWN),
        qty=_required_dec(item.qty),
        qty_unit=_qty_unit(item, category),
        filled_qty=_dec(item.cum_exec_qty) or Decimal(0),
        price=_positive_or_none(item.price),
        avg_fill_price=_positive_or_none(item.avg_price),
        time_in_force=_TIME_IN_FORCE.get(item.time_in_force, TimeInForce.UNKNOWN),
        reduce_only=item.reduce_only,
        conditional=conditional,
        trigger_price=_positive_or_none(item.trigger_price),
        take_profit=_positive_or_none(item.take_profit),
        stop_loss=_positive_or_none(item.stop_loss),
        created_at=created_at,
        updated_at=_ts(_int(item.updated_time)),
        origin=order_origin(item.order_link_id),
        client_order_id=_untrusted(link_id),
        reject_reason=_untrusted("" if item.reject_reason.strip() == _NO_ERROR else item.reject_reason),
    )


def _untrusted(raw: str) -> UntrustedText | None:
    text = raw.strip()
    return UntrustedText(text[:_MAX_CLIENT_ORDER_ID]) if text else None


_MARKET_UNITS: Final = {"baseCoin": QtyUnit.BASE, "quoteCoin": QtyUnit.QUOTE}


def _qty_unit(item: _OrderDTO, category: Category) -> QtyUnit:
    """Linear quantities and spot limit orders are in the base asset. A spot market order says its
    unit in ``marketUnit`` (a market buy defaults to the quote coin when placed); anything else -
    empty or a value we do not know - is ``UNKNOWN``, never assumed to be base."""
    if category is Category.LINEAR or item.order_type == "Limit":
        return QtyUnit.BASE
    return _MARKET_UNITS.get(item.market_unit.strip(), QtyUnit.UNKNOWN)


def order_origin(order_link_id: str) -> OrderOrigin:
    """``system`` for an ``orderLinkId`` in this system's format, else ``external`` (FR-2)."""
    return OrderOrigin.SYSTEM if order_link_id.startswith(CLIENT_ORDER_ID_PREFIX) else OrderOrigin.EXTERNAL
