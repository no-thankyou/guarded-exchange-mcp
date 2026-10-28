"""Spike 004: probe Bybit V5 Demo Trading (and classic testnet, if a key exists) by hand.

Manual script, never run in CI (it needs a key and the network). It answers the ticket 004
questions and records scrubbed responses into ``tests/fixtures/bybit/spike004/``.

Safety rules baked in:

* hosts are a hard-coded allowlist (``api-demo.bybit.com``, ``api-testnet.bybit.com``); any other
  host, mainnet included, raises before a socket is opened;
* the key file is read by path (argument or ``GX_SPIKE_ENV_FILE``); key, secret and signature are
  never printed, request headers are never logged or stored, and every recorded/printed value goes
  through :func:`scrub` (key-material fields such as ``apiKey``/``ips``/``uid`` are replaced too);
* orders: minimal size, resting limits far from the market where the question allows it;
  the run refuses to trade if the probe symbols already have open orders or a position, and the
  ``finally`` block cancels everything on the probe symbols and flattens the linear position.

Run: ``uv run python spikes/004/bybit_demo_probe.py [--read-only | --verify-key] <env-file>``
(``--read-only``: preflight reads only, no orders, no settings changes, no demo top-up;
``--verify-key``: D-24 verdict for a key + one far limit order per category, cancelled at once;
prints permission names only and writes no fixtures). The key file must be ``chmod 600``.
"""

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx2

ENV_FILE_VAR = "GX_SPIKE_ENV_FILE"
ALLOWED_HOSTS: Mapping[str, str] = {"demo": "api-demo.bybit.com", "testnet": "api-testnet.bybit.com"}
KEY_VARS: Mapping[str, tuple[str, str]] = {
    "demo": ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"),
    "testnet": ("BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET"),
}
RECV_WINDOW = "5000"
REDACTED = "<redacted>"
# Fields that identify the key or the account; never written to fixtures or stdout. Placeholders keep
# the JSON type so fixtures still parse like real responses: strings -> "<redacted>", numeric ids -> 0,
# ``ips`` -> ``[]`` if it was empty, ``["<redacted>"]`` otherwise.
REDACT_STRINGS = frozenset({"apiKey", "secret", "id", "note"})
REDACT_NUMBERS = frozenset({"userID", "inviterID", "affiliateID", "parentUid"})
REDACT_LISTS = frozenset({"ips"})
REDACT_KEYS = REDACT_STRINGS | REDACT_NUMBERS | REDACT_LISTS
LIMIT_HEADERS = ("x-bapi-limit", "x-bapi-limit-status", "x-bapi-limit-reset-timestamp")
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "bybit" / "spike004"
SYMBOL = "BTCUSDT"


class ForbiddenHostError(RuntimeError):
    """The probe tried to reach a host outside the demo/testnet allowlist."""


@dataclass(frozen=True)
class Credentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"


# --- pure helpers (unit-tested in test_bybit_demo_probe.py) -----------------------------------


def parse_env_file(text: str) -> dict[str, str]:
    """``KEY=VALUE`` lines; ``#`` comments, ``export`` prefix and surrounding quotes allowed."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        values[name.strip()] = value.strip().strip("'\"")
    return values


def credentials_for(env: str, values: Mapping[str, str]) -> Credentials | None:
    key_var, secret_var = KEY_VARS[env]
    key, secret = values.get(key_var, ""), values.get(secret_var, "")
    return Credentials(key, secret) if key and secret else None


def key_file_problem(path: Path) -> str | None:
    """Refuse key files readable or writable by group/others (``st_mode & 0o077``)."""
    mode = path.stat().st_mode
    if mode & 0o077:
        return f"key file permissions are {oct(mode & 0o777)}; run: chmod 600 {path}"
    return None


# D-24: the only permissions a key may carry (NFR-1 allowlist); empty groups mean "no permission".
D24_ALLOWED: Mapping[str, frozenset[str]] = {
    "ContractTrade": frozenset({"Order", "Position"}),
    "Spot": frozenset({"SpotTrade"}),
}


def permission_names(permissions: Mapping[str, Any]) -> dict[str, list[str]]:
    """Non-empty permission groups as ``{group: [names]}`` (names only, safe to print)."""
    return {g: sorted(str(n) for n in names) for g, names in permissions.items() if names}


def d24_extra_permissions(permissions: Mapping[str, Any]) -> dict[str, list[str]]:
    """Permissions outside the D-24 allowlist; empty dict = the key passes D-24."""
    extra: dict[str, list[str]] = {}
    for group, names in permission_names(permissions).items():
        outside = [n for n in names if n not in D24_ALLOWED.get(group, frozenset())]
        if outside:
            extra[group] = outside
    return extra


def base_url(env: str) -> str:
    host = ALLOWED_HOSTS.get(env)
    if host is None:
        raise ForbiddenHostError(f"environment {env!r} is not allowed")
    return f"https://{host}"


def check_host(url: str) -> None:
    host = urlsplit(url).hostname
    if host not in ALLOWED_HOSTS.values():
        raise ForbiddenHostError(f"host {host!r} is not in the demo/testnet allowlist")


def sign(secret: str, timestamp: str, key: str, recv_window: str, payload: str) -> str:
    """Bybit V5 HMAC_SHA256: ``timestamp + api_key + recv_window + (queryString | jsonBody)``."""
    message = f"{timestamp}{key}{recv_window}{payload}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def redacted_placeholder(field_name: str, value: Any) -> Any:
    """Type-preserving placeholder for a key-identifying field (see ``REDACT_*``)."""
    if field_name in REDACT_NUMBERS:
        return 0
    if field_name in REDACT_LISTS:
        return [REDACTED] if value not in ([], "", None) else []
    return REDACTED


def scrub(value: Any, secrets_: tuple[str, ...]) -> Any:
    """Copy of ``value`` with key-identifying fields and any occurrence of a secret redacted."""
    if isinstance(value, dict):
        return {
            k: (redacted_placeholder(k, v) if k in REDACT_KEYS else scrub(v, secrets_))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub(v, secrets_) for v in value]
    if isinstance(value, str):
        return REDACTED if any(s and s in value for s in secrets_) else value
    return value


def to_step(value: Decimal, step: Decimal, *, up: bool) -> Decimal:
    """Round ``value`` to a multiple of ``step``: up (ceiling) or down (floor)."""
    units = (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return (units * step).quantize(step)


def find_secret_leaks(directory: Path, secrets_: tuple[str, ...]) -> list[str]:
    """Files under ``directory`` that contain any of ``secrets_`` (returns names only)."""
    return sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() and any(s and s in path.read_text(errors="replace") for s in secrets_)
    )


def status_in(*statuses: str) -> Callable[[dict[str, Any] | None], bool]:
    """Poll predicate: the order exists and its ``orderStatus`` is one of ``statuses``."""
    return lambda order: order is not None and order.get("orderStatus") in statuses


# --- HTTP ---------------------------------------------------------------------------------------


@dataclass
class Reply:
    status: int
    body: dict[str, Any]
    limit_headers: dict[str, str]
    elapsed_ms: int

    @property
    def code(self) -> int | None:
        code = self.body.get("retCode")
        return code if isinstance(code, int) else None

    @property
    def result(self) -> dict[str, Any]:
        result = self.body.get("result")
        return result if isinstance(result, dict) else {}


class BybitProbeClient:
    def __init__(self, env: str, creds: Credentials, http: httpx2.Client) -> None:
        self.env = env
        self._base = base_url(env)
        self._creds = creds
        self._http = http
        self.clock_offset_ms = 0

    def sync_clock(self) -> int:
        reply = self.call("GET", "/v5/market/time", signed=False)
        server_ms = int(reply.body.get("time", 0))
        self.clock_offset_ms = server_ms - int(time.time() * 1000) if server_ms else 0
        return self.clock_offset_ms

    def call(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        signed: bool = True,
        timeout: httpx2.Timeout | None = None,
        timestamp_shift_ms: int = 0,
    ) -> Reply:
        params = dict(params or {})
        headers: dict[str, str] = {}
        if method == "GET":
            payload = urlencode(params)
            url = f"{self._base}{path}" + (f"?{payload}" if payload else "")
            content: bytes | None = None
        else:
            payload = json.dumps(params, separators=(",", ":"))
            url = f"{self._base}{path}"
            content = payload.encode()
            headers["Content-Type"] = "application/json"
        check_host(url)
        if signed:
            ts = str(int(time.time() * 1000) + self.clock_offset_ms + timestamp_shift_ms)
            headers |= {
                "X-BAPI-API-KEY": self._creds.key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": RECV_WINDOW,
                "X-BAPI-SIGN": sign(self._creds.secret, ts, self._creds.key, RECV_WINDOW, payload),
            }
        started = time.monotonic()
        kwargs: dict[str, Any] = {"content": content, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = self._http.request(method, url, **kwargs)
        elapsed = int((time.monotonic() - started) * 1000)
        try:
            body = response.json()
        except ValueError:
            body = {"non_json_body": response.text[:500]}
        limits = {h: response.headers[h] for h in LIMIT_HEADERS if h in response.headers}
        return Reply(
            response.status_code, body if isinstance(body, dict) else {"body": body}, limits, elapsed
        )


# --- recording ------------------------------------------------------------------------------------


class Recorder:
    """Writes scrubbed response bodies as fixtures and keeps an index with request metadata."""

    def __init__(self, directory: Path | None, secrets_: tuple[str, ...]) -> None:
        self.directory = directory
        self.secrets = secrets_
        self.index: list[dict[str, Any]] = []
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def quiet(cls, secrets_: tuple[str, ...]) -> "Recorder":
        """Prints scrubbed lines, writes nothing (``--verify-key``)."""
        return cls(None, secrets_)

    def record(
        self,
        name: str,
        client: BybitProbeClient,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        *,
        reply: Reply,
        **notes: Any,
    ) -> None:
        body = scrub(reply.body, self.secrets)
        if self.directory is None:
            self.say(name, retCode=reply.code, retMsg=reply.body.get("retMsg"))
            return
        (self.directory / f"{name}.json").write_text(json.dumps(body, indent=4, ensure_ascii=False) + "\n")
        entry = {
            "fixture": f"{name}.json",
            "env": client.env,
            "host": ALLOWED_HOSTS[client.env],
            "method": method,
            "path": path,
            "request": scrub(dict(params or {}), self.secrets),
            "http_status": reply.status,
            "rate_limit_headers": reply.limit_headers,
            "elapsed_ms": reply.elapsed_ms,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **scrub(notes, self.secrets),
        }
        self.index.append(entry)
        self.say(name, retCode=reply.code, retMsg=reply.body.get("retMsg"), http=reply.status, **notes)

    def say(self, step: str, **fields: Any) -> None:
        print(json.dumps({"step": step, **scrub(fields, self.secrets)}, ensure_ascii=False, default=str))

    def flush(self) -> None:
        """``index.json``: run number (previous + 1), history note and one entry per fixture."""
        if self.directory is None:
            return
        path = self.directory / "index.json"
        previous = json.loads(path.read_text()) if path.is_file() else {}
        run = int(previous.get("run_number", 0)) + 1 if isinstance(previous, dict) else 1
        history = list(previous.get("history", [])) if isinstance(previous, dict) else []
        history.append(f"run {run}: {datetime.now(UTC).isoformat(timespec='seconds')}, fixtures overwritten")
        index = {"run_number": run, "history": history, "entries": self.index}
        path.write_text(json.dumps(index, indent=4, ensure_ascii=False) + "\n")


# --- the probe ----------------------------------------------------------------------------------


@dataclass
class Rules:
    tick: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_amt: Decimal = Decimal(0)


class Probe:
    def __init__(self, client: BybitProbeClient, rec: Recorder) -> None:
        self.c = client
        self.rec = rec
        self.run_id = secrets.token_hex(3)
        self._seq = 0
        self.rules: dict[str, Rules] = {}
        self.order_sources: dict[str, str] = {}
        self.first_sources: dict[str, str] = {}
        self.traded = False
        self.restore_linear_modify: bool | None = None
        self.read_only = False

    # small wrappers ----------------------------------------------------------------------

    def link(self, tag: str) -> str:
        self._seq += 1
        return f"gx-s004-{self.run_id}-{tag}{self._seq}"

    def get(self, name: str | None, path: str, params: Mapping[str, Any], **notes: Any) -> Reply:
        reply = self.c.call("GET", path, params)
        if name:
            self.rec.record(name, self.c, "GET", path, params, reply=reply, **notes)
        time.sleep(0.15)
        return reply

    def post(
        self, name: str | None, path: str, body: Mapping[str, Any], *, pause: bool = True, **notes: Any
    ) -> Reply:
        """POST and record; ``pause=False`` lets the caller read state with no delay (Q5/Q7)."""
        reply = self.c.call("POST", path, body)
        if name:
            self.rec.record(name, self.c, "POST", path, body, reply=reply, **notes)
        else:
            self.rec.say(path, retCode=reply.code, retMsg=reply.body.get("retMsg"))
        if pause:
            time.sleep(0.15)
        return reply

    def ticker(self, category: str) -> tuple[Decimal, Decimal]:
        reply = self.c.call("GET", "/v5/market/tickers", {"category": category, "symbol": SYMBOL})
        row = reply.result["list"][0]
        return Decimal(row["bid1Price"]), Decimal(row["ask1Price"])

    def price_limit(self, category: str) -> tuple[Decimal, Decimal]:
        reply = self.c.call("GET", "/v5/market/price-limit", {"category": category, "symbol": SYMBOL})
        return Decimal(reply.result["buyLmt"]), Decimal(reply.result["sellLmt"])

    def order(self, category: str, link: str) -> dict[str, Any] | None:
        """Order by ``orderLinkId``: ``/v5/order/realtime`` first, then ``/v5/order/history``.

        ``self.order_sources[link]`` remembers which endpoint answered (Q4/Q5: where an order is
        visible right after create / after it is closed).
        """
        params = {"category": category, "symbol": SYMBOL, "orderLinkId": link}
        for path in ("/v5/order/realtime", "/v5/order/history"):
            rows = self.c.call("GET", path, params).result.get("list") or []
            if rows:
                self.order_sources[link] = path
                return dict(rows[0])
        self.order_sources[link] = "not found"
        return None

    def poll(
        self, category: str, link: str, done: Callable[[dict[str, Any] | None], bool], limit_s: float = 5.0
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int, int]:
        """(first snapshot, final snapshot, attempts, ms until ``done``; -1 if never)."""
        started = time.monotonic()
        first: dict[str, Any] | None = None
        attempts = 0
        while True:
            attempts += 1
            snapshot = self.order(category, link)
            if attempts == 1:
                first = snapshot
                self.first_sources[link] = self.order_sources[link]
            elapsed = int((time.monotonic() - started) * 1000)
            if done(snapshot):
                return first, snapshot, attempts, elapsed
            if elapsed > limit_s * 1000:
                return first, snapshot, attempts, -1
            time.sleep(0.1)

    def create(
        self,
        name: str | None,
        category: str,
        notes: Mapping[str, Any] | None = None,
        *,
        pause: bool = True,
        **fields: Any,
    ) -> Reply:
        body = {"category": category, "symbol": SYMBOL, "orderType": "Limit", **fields}
        if category == "linear":
            body.setdefault("positionIdx", 0)
        return self.post(name, "/v5/order/create", body, pause=pause, **dict(notes or {}))

    def cancel(self, category: str, link: str) -> Reply:
        return self.post(
            None, "/v5/order/cancel", {"category": category, "symbol": SYMBOL, "orderLinkId": link}
        )

    def far_buy_price(self, category: str) -> Decimal:
        bid, _ = self.ticker(category)
        return to_step(bid * Decimal("0.85"), self.rules[category].tick, up=False)

    def spot_qty(self, price: Decimal, factor: str = "2") -> Decimal:
        r = self.rules["spot"]
        need = to_step(r.min_amt * Decimal(factor) / price, r.qty_step, up=True)
        return max(need, r.min_qty)

    def qty(self, category: str, price: Decimal) -> Decimal:
        return self.rules["linear"].min_qty if category == "linear" else self.spot_qty(price)

    # steps ----------------------------------------------------------------------------------

    def preflight(self) -> bool:
        """Read-only questions; returns True if trading steps are safe to run."""
        self.rec.say("clock", offset_ms=self.c.sync_clock())
        self.get("demo_query_api", "/v5/user/query-api", {}, question="Q1 query-api on demo")
        self.get("demo_user_setting_config", "/v5/account/user-setting-config", {}, question="Q6 config")
        for category in ("linear", "spot"):
            reply = self.get(
                f"demo_instruments_info_{category}_btcusdt",
                "/v5/market/instruments-info",
                {"category": category, "symbol": SYMBOL},
                question="Q2 instruments-info on demo",
            )
            item = reply.result["list"][0]
            price_f, lot = item["priceFilter"], item["lotSizeFilter"]
            if category == "linear":
                self.rules[category] = Rules(
                    Decimal(price_f["tickSize"]), Decimal(lot["qtyStep"]), Decimal(lot["minOrderQty"])
                )
            else:
                self.rules[category] = Rules(
                    Decimal(price_f["tickSize"]),
                    Decimal(lot["basePrecision"]),
                    Decimal(lot.get("minOrderQty") or lot["basePrecision"]),
                    Decimal(lot["minOrderAmt"]),
                )
            self.get(
                f"demo_price_limit_{category}_btcusdt",
                "/v5/market/price-limit",
                {"category": category, "symbol": SYMBOL},
                question="Q6 order price limit",
            )
        self.get(
            "demo_instruments_info_linear_page",
            "/v5/market/instruments-info",
            {"category": "linear", "limit": "2"},
            question="Q2 pagination (nextPageCursor)",
        )
        wallet = self.get("demo_wallet_balance", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        usdt = Decimal(0)
        for account in wallet.result.get("list") or []:
            for coin in account.get("coin") or []:
                if coin.get("coin") == "USDT":
                    usdt = Decimal(coin.get("walletBalance") or 0)
        self.rec.say("usdt_balance_demo", usdt=str(usdt))
        if usdt < Decimal(1000) and not self.read_only:
            self.post(
                "demo_apply_money",
                "/v5/account/demo-apply-money",
                {"adjustType": 0, "utaDemoApplyMoney": [{"coin": "USDT", "amountStr": "5000"}]},
            )
        positions = self.get(
            "demo_position_list_before", "/v5/position/list", {"category": "linear", "symbol": SYMBOL}
        )
        busy = any(Decimal(p.get("size") or 0) != 0 for p in positions.result.get("list") or [])
        for category in ("linear", "spot"):
            orders = self.c.call(
                "GET", "/v5/order/realtime", {"category": category, "symbol": SYMBOL, "openOnly": 0}
            )
            busy = busy or any(
                o.get("orderStatus") in {"New", "PartiallyFilled", "Untriggered"}
                for o in orders.result.get("list") or []
            )
        if busy:
            self.rec.say("abort", reason="probe symbol already has open orders or a position; not trading")
        return not busy

    def link_uniqueness(self, category: str) -> None:
        """Q3: orderLinkId scope — active duplicate, reuse after cancel, after fill."""
        price = self.far_buy_price(category)
        qty = self.qty(category, price)
        common: dict[str, Any] = {"side": "Buy", "qty": str(qty), "price": str(price), "timeInForce": "GTC"}
        a = self.link("a")
        self.create(f"{category}_create_limit_gtc_ok", category, orderLinkId=a, **common)
        self.create(
            f"{category}_create_duplicate_active", category, orderLinkId=a, notes={"question": "Q3"}, **common
        )
        self.cancel(category, a)
        self.poll(category, a, status_in("Cancelled"))
        reuse = self.create(
            f"{category}_create_reuse_after_cancel",
            category,
            orderLinkId=a,
            notes={"question": "Q3"},
            **common,
        )
        if reuse.code == 0:
            self.cancel(category, a)
            self.rec.say(f"{category}_reuse_after_cancel_order", order=self.order(category, a))

        # Q5: Limit IOC that crosses — what create returns and when cumExecQty is visible.
        _, ask = self.ticker(category)
        ioc_price = to_step(ask * Decimal("1.002"), self.rules[category].tick, up=True)
        ioc_qty = self.qty(category, ioc_price)
        b = self.link("b")
        self.create(
            f"{category}_create_limit_ioc_crossing",
            category,
            side="Buy",
            qty=str(ioc_qty),
            price=str(ioc_price),
            timeInForce="IOC",
            orderLinkId=b,
            notes={"question": "Q5"},
            pause=False,
        )
        first, final, attempts, ms = self.poll(category, b, status_in("Filled", "Cancelled", "Rejected"))
        self._save_order(
            f"{category}_order_ioc_first_read", first, attempts=1, first_read_source=self.first_sources.get(b)
        )
        self._save_order(f"{category}_order_ioc_final_read", final, attempts=attempts, ms_until_terminal=ms)
        self.create(
            f"{category}_create_reuse_after_fill", category, notes={"question": "Q3"}, orderLinkId=b, **common
        )
        after = self.order(category, b)
        if after is not None and after["orderStatus"] == "New":
            self.cancel(category, b)
        self.flatten(category, f"{category}_close_ioc_reduce")

        # Q5: Limit IOC that does not cross — expected Cancelled with zero fill.
        bid, _ = self.ticker(category)
        d = self.link("d")
        no_cross = to_step(bid * Decimal("0.95"), self.rules[category].tick, up=False)
        self.create(
            f"{category}_create_limit_ioc_not_crossing",
            category,
            side="Buy",
            qty=str(self.qty(category, no_cross)),
            price=str(no_cross),
            timeInForce="IOC",
            orderLinkId=d,
        )
        first, final, attempts, ms = self.poll(category, d, status_in("Cancelled", "Rejected", "Filled"))
        self._save_order(f"{category}_order_ioc_not_crossing_final", final, attempts=attempts, ms=ms)

    def _save_order(self, name: str, order: dict[str, Any] | None, **notes: Any) -> None:
        body = {"retCode": 0, "retMsg": "OK", "result": {"list": [order] if order else []}}
        reply = Reply(200, body, {}, 0)
        link = order.get("orderLinkId", "") if order else ""
        path = self.order_sources.get(link, "/v5/order/realtime")
        self.rec.record(name, self.c, "GET", path, {"source": "poll snapshot"}, reply=reply, **notes)

    def flatten(self, category: str, name: str) -> None:
        """Undo what an IOC buy did: reduce-only IOC sell (linear) or sell the bought base (spot)."""
        if category == "linear":
            rows = self.c.call("GET", "/v5/position/list", {"category": "linear", "symbol": SYMBOL})
            for pos in rows.result.get("list") or []:
                size = Decimal(pos.get("size") or 0)
                if size == 0:
                    continue
                bid, ask = self.ticker("linear")
                side = "Sell" if pos["side"] == "Buy" else "Buy"
                price = (
                    to_step(bid * Decimal("0.998"), self.rules["linear"].tick, up=False)
                    if side == "Sell"
                    else to_step(ask * Decimal("1.002"), self.rules["linear"].tick, up=True)
                )
                link = self.link("c")
                self.create(
                    name,
                    "linear",
                    side=side,
                    qty=str(size),
                    price=str(price),
                    timeInForce="IOC",
                    reduceOnly=True,
                    orderLinkId=link,
                )
                self.poll("linear", link, status_in("Filled", "Cancelled"))
            return
        free = self.spot_free_btc()
        r = self.rules["spot"]
        sell = to_step(free - self.btc_baseline, r.qty_step, up=False)
        bid, _ = self.ticker("spot")
        if sell <= 0 or sell * bid < r.min_amt:
            self.rec.say("spot_flatten_skip", sell=str(sell), reason="below min amount (dust stays)")
            return
        link = self.link("s")
        price = to_step(bid * Decimal("0.998"), r.tick, up=False)
        self.create(
            name, "spot", side="Sell", qty=str(sell), price=str(price), timeInForce="IOC", orderLinkId=link
        )
        self.poll("spot", link, status_in("Filled", "Cancelled"))

    btc_baseline = Decimal(0)

    def spot_free_btc(self) -> Decimal:
        reply = self.c.call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "BTC"})
        for account in reply.result.get("list") or []:
            for coin in account.get("coin") or []:
                if coin.get("coin") == "BTC":
                    return Decimal(coin.get("walletBalance") or 0)
        return Decimal(0)

    def limit_price_behaviour(self) -> None:
        """Q6: what a crossing limit beyond the price band does; switch linear to reject; restore."""
        config = self.c.call("GET", "/v5/account/user-setting-config", {}).result
        for category in ("linear", "spot"):
            self._over_limit_buy(category, f"{category}_create_over_buy_limit_default")
        original_linear_modify = not bool(config.get("lpaPerp"))  # docs: lpaPerp=true means reject
        self.restore_linear_modify = original_linear_modify  # cleanup() restores it if a step fails
        self.post(
            "demo_set_limit_px_action_linear_reject",
            "/v5/account/set-limit-px-action",
            {"category": "linear", "modifyEnable": False},
        )
        self.get("demo_user_setting_config_after_reject", "/v5/account/user-setting-config", {})
        self._over_limit_buy("linear", "linear_create_over_buy_limit_reject_mode")
        self.post(
            "demo_set_limit_px_action_linear_restore",
            "/v5/account/set-limit-px-action",
            {"category": "linear", "modifyEnable": original_linear_modify},
        )
        self.restore_linear_modify = None
        self.get("demo_user_setting_config_restored", "/v5/account/user-setting-config", {})

    def _over_limit_buy(self, category: str, name: str) -> None:
        buy_lmt, sell_lmt = self.price_limit(category)
        price = to_step(buy_lmt * Decimal("1.01"), self.rules[category].tick, up=True)
        link = self.link("p")
        reply = self.create(
            name,
            category,
            side="Buy",
            qty=str(self.qty(category, price)),
            price=str(price),
            timeInForce="IOC",
            orderLinkId=link,
            notes={"buy_limit": str(buy_lmt), "sell_limit": str(sell_lmt), "sent_price": str(price)},
        )
        if reply.code == 0:
            _, final, attempts, ms = self.poll(category, link, status_in("Filled", "Cancelled", "Rejected"))
            self._save_order(f"{name}_order", final, attempts=attempts, ms=ms, sent_price=str(price))
            self.flatten(category, f"{name}_close")

    def amend_and_cancel_all(self) -> None:
        """Q7: is amend / cancel-all visible right after a successful response?"""
        category = "linear"
        price = self.far_buy_price(category)
        qty = str(self.qty(category, price))
        j = self.link("j")
        self.create(None, category, side="Buy", qty=qty, price=str(price), timeInForce="GTC", orderLinkId=j)
        self.poll(category, j, status_in("New"))
        new_price = price - self.rules[category].tick * 10
        self.post(
            "linear_amend_ok",
            "/v5/order/amend",
            {"category": category, "symbol": SYMBOL, "orderLinkId": j, "price": str(new_price)},
            pause=False,
        )
        first, _, attempts, ms = self.poll(
            category, j, lambda o: o is not None and Decimal(o["price"]) == new_price
        )
        self.rec.say(
            "linear_amend_visibility",
            first_read_price=first and first["price"],
            amended_price=str(new_price),
            attempts=attempts,
            ms_until_visible=ms,
        )
        links = [j, self.link("k")]
        self.create(
            None, category, side="Buy", qty=qty, price=str(price), timeInForce="GTC", orderLinkId=links[1]
        )
        self.poll(category, links[1], status_in("New"))
        self.post(
            "linear_cancel_all_ok",
            "/v5/order/cancel-all",
            {"category": category, "symbol": SYMBOL, "orderFilter": "Order"},
            pause=False,
        )
        for link in links:
            first, _, attempts, ms = self.poll(category, link, status_in("Cancelled"))
            self.rec.say(
                "linear_cancel_all_visibility",
                first_read_status=first and first["orderStatus"],
                attempts=attempts,
                ms_until_cancelled=ms,
            )

    def unknown_outcome(self) -> None:
        """Q4: provoke what we can — a client-side read timeout after send, and a stale timestamp."""
        category = "linear"
        price = self.far_buy_price(category)
        body: dict[str, Any] = {
            "category": category,
            "symbol": SYMBOL,
            "orderType": "Limit",
            "side": "Buy",
            "qty": str(self.qty(category, price)),
            "price": str(price),
            "timeInForce": "GTC",
            "positionIdx": 0,
        }
        stale = dict(body, orderLinkId=self.link("t"))
        reply = self.c.call("POST", "/v5/order/create", stale, timestamp_shift_ms=-60_000)
        self.rec.record(
            "linear_create_stale_timestamp", self.c, "POST", "/v5/order/create", stale, reply=reply
        )
        after_stale = self.order(category, stale["orderLinkId"])
        self._save_order(
            "linear_order_after_stale_timestamp",
            after_stale,
            source=self.order_sources.get(stale["orderLinkId"]),
            question="Q4: is an order created after 10002?",
        )

        m = dict(body, orderLinkId=self.link("m"))
        outcome = "response"
        try:
            self.c.call(
                "POST",
                "/v5/order/create",
                m,
                timeout=httpx2.Timeout(5.0, read=0.001),
            )
        except httpx2.TimeoutException as exc:
            outcome = type(exc).__name__
        _, final, attempts, ms = self.poll(category, m["orderLinkId"], lambda o: o is not None, limit_s=3)
        self._save_order(
            "linear_order_after_client_timeout",
            final,
            first_read_source=self.first_sources.get(m["orderLinkId"]),
            client_outcome=outcome,
            attempts=attempts,
            ms_until_visible=ms,
        )
        retry = self.c.call("POST", "/v5/order/create", m)
        self.rec.record(
            "linear_create_retry_after_timeout",
            self.c,
            "POST",
            "/v5/order/create",
            m,
            reply=retry,
            question="Q4",
        )
        self.cancel(category, m["orderLinkId"])

    def cleanup(self) -> dict[str, Any]:
        """Cancel everything on the probe symbols, flatten the linear position, report what is left."""
        if self.restore_linear_modify is not None:
            body = {"category": "linear", "modifyEnable": self.restore_linear_modify}
            restored = self.c.call("POST", "/v5/account/set-limit-px-action", body)
            self.rec.say(
                "cleanup_restore_limit_px_action", retCode=restored.code, modifyEnable=body["modifyEnable"]
            )
            self.restore_linear_modify = None
        for category in ("linear", "spot"):
            self.c.call("POST", "/v5/order/cancel-all", {"category": category, "symbol": SYMBOL})
            time.sleep(0.3)
        if "linear" in self.rules:
            self.flatten("linear", "cleanup_linear_close")
        time.sleep(1.0)
        left: dict[str, Any] = {}
        for category, params in (
            ("linear", {"category": "linear", "settleCoin": "USDT"}),
            ("spot", {"category": "spot"}),
        ):
            rows = (
                self.c.call("GET", "/v5/order/realtime", {**params, "openOnly": 0}).result.get("list") or []
            )
            left[f"{category}_open_orders"] = [
                o["orderLinkId"]
                for o in rows
                if o.get("orderStatus") in {"New", "PartiallyFilled", "Untriggered"}
            ]
        positions = self.c.call("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        left["linear_positions"] = [
            {"symbol": p["symbol"], "size": p["size"]}
            for p in positions.result.get("list") or []
            if Decimal(p.get("size") or 0) != 0
        ]
        left["spot_btc_delta_vs_start"] = str(self.spot_free_btc() - self.btc_baseline)
        return left


def testnet_check(creds: Credentials | None, http: httpx2.Client, rec: Recorder) -> None:
    if creds is None:
        rec.say("testnet", status="not checked: no BYBIT_TESTNET_API_KEY/SECRET in the env file")
        return
    client = BybitProbeClient("testnet", creds, http)
    client.sync_clock()
    for name, path, params in (
        ("testnet_query_api", "/v5/user/query-api", {}),
        (
            "testnet_instruments_info_linear_btcusdt",
            "/v5/market/instruments-info",
            {"category": "linear", "symbol": SYMBOL},
        ),
    ):
        rec.record(name, client, "GET", path, params, reply=client.call("GET", path, params))


OPEN_STATUSES = frozenset({"New", "PartiallyFilled", "Untriggered"})


def is_open(order: dict[str, Any] | None) -> bool:
    return order is not None and order.get("orderStatus") in OPEN_STATUSES


def place_and_cancel(probe: Probe, category: str) -> tuple[dict[str, Any], str | None]:
    """One far limit buy, always followed by a confirmed cancel by ``orderLinkId``.

    Returns (outcome, ``"<symbol>/<category>/<orderLinkId>"`` if the order may still be open).
    A create that raised (timeout = unknown outcome) is looked up before deciding; any doubt about
    the final state counts as "left open" so the caller fails loudly.
    """
    price = probe.far_buy_price(category)
    link = probe.link("v")
    outcome: dict[str, Any] = {"create": "not sent"}
    accepted: bool | None = None  # None = unknown outcome
    try:
        created = probe.create(
            None,
            category,
            side="Buy",
            qty=str(probe.qty(category, price)),
            price=str(price),
            timeInForce="GTC",
            orderLinkId=link,
        )
        outcome = {"create": created.code}
        if created.code == 0:
            accepted = True
        else:
            outcome["retMsg"] = created.body.get("retMsg")
            accepted = False
    except Exception as exc:  # unknown outcome: the order may exist
        outcome = {"create": f"unknown ({type(exc).__name__})"}
    finally:
        left_open = not cancel_confirmed(probe, category, link, accepted)
    outcome["cancelled"] = not left_open
    return outcome, f"{SYMBOL}/{category}/{link}" if left_open else None


def cancel_confirmed(probe: Probe, category: str, link: str, accepted: bool | None) -> bool:
    """True when the order with ``link`` is known not to be open (cancelled or never created)."""
    try:
        if accepted is False:
            return True
        if accepted is None:  # look it up first: a timed-out create may still have been placed
            _, found, _, _ = probe.poll(category, link, lambda o: o is not None, limit_s=3.0)
            if found is None:
                return True
            if not is_open(found):
                return True
        cancelled = probe.cancel(category, link)
        probe.rec.say("verify_key_cancel", category=category, retCode=cancelled.code)
        _, final, _, ms = probe.poll(category, link, lambda o: not is_open(o), limit_s=3.0)
        return ms >= 0 and not is_open(final)
    except Exception as exc:  # cannot prove the order is gone -> report it as left open
        probe.rec.say("verify_key_cancel_error", category=category, error=type(exc).__name__)
        return False


def verify_key(probe: Probe) -> int:
    """Check a (re)created demo key: D-24 permissions + one far limit order per category, cancelled.

    Prints permission *names* only, writes no fixtures. Exit code 0 = passes D-24 and trades.
    """
    say = probe.rec.say
    probe.c.sync_clock()
    info = probe.c.call("GET", "/v5/user/query-api")
    if info.code != 0:
        say("verify_key", query_api_retCode=info.code, retMsg=info.body.get("retMsg"))
        return 1
    result = info.result
    permissions = result.get("permissions") or {}
    extra = d24_extra_permissions(permissions)
    read_only = result.get("readOnly") == 1
    say(
        "verify_key_permissions",
        permissions=permission_names(permissions),
        readOnly=result.get("readOnly"),
        ips_bound=bool(result.get("ips")) and result.get("ips") != ["*"],
        expiredAt=result.get("expiredAt"),
        outside_d24=extra,
    )
    trades: dict[str, Any] = {}
    left_open: list[str] = []
    for category in ("linear", "spot"):
        instruments = probe.c.call(
            "GET", "/v5/market/instruments-info", {"category": category, "symbol": SYMBOL}
        ).result["list"][0]
        lot = instruments["lotSizeFilter"]
        probe.rules[category] = Rules(
            Decimal(instruments["priceFilter"]["tickSize"]),
            Decimal(lot.get("qtyStep") or lot["basePrecision"]),
            Decimal(lot.get("minOrderQty") or lot["basePrecision"]),
            Decimal(lot.get("minOrderAmt") or 0),
        )
        trades[category], leftover = place_and_cancel(probe, category)
        if leftover:
            left_open.append(leftover)
    for leftover in left_open:
        message = f"LEFT OPEN: limit order {leftover} is still open, cancel it manually"
        say("verify_key_left_open_order", message=message)
        print(message, file=sys.stderr)
    trades_ok = not left_open and all(t.get("create") == 0 and t.get("cancelled") for t in trades.values())
    say(
        "verify_key_verdict",
        passes_d24=not extra and not read_only,
        read_only_profile=read_only,
        trades=trades_ok,
        orders=trades,
    )
    if left_open:
        return 3
    return 0 if not extra and trades_ok else 1


def steps(probe: Probe) -> Iterator[tuple[str, Callable[[], None]]]:
    yield "linear_uniqueness", lambda: probe.link_uniqueness("linear")
    yield "spot_uniqueness", lambda: probe.link_uniqueness("spot")
    yield "limit_price_behaviour", probe.limit_price_behaviour
    yield "amend_and_cancel_all", probe.amend_and_cancel_all
    yield "unknown_outcome", probe.unknown_outcome


def main(argv: list[str]) -> int:
    read_only = "--read-only" in argv
    verify = "--verify-key" in argv
    paths = [a for a in argv[1:] if not a.startswith("--")]
    env_path = Path(paths[0] if paths else os.environ.get(ENV_FILE_VAR, "")).expanduser()
    if not env_path.is_file():
        usage = "usage: bybit_demo_probe.py [--read-only | --verify-key] <env-file>"
        print(f"{usage} (or {ENV_FILE_VAR}=<env-file>)", file=sys.stderr)
        return 2
    problem = key_file_problem(env_path)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    values = parse_env_file(env_path.read_text())
    demo = credentials_for("demo", values)
    testnet = credentials_for("testnet", values)
    if demo is None:
        print("BYBIT_DEMO_API_KEY / BYBIT_DEMO_API_SECRET missing in the env file", file=sys.stderr)
        return 2
    all_secrets = tuple(s for c in (demo, testnet) if c for s in (c.key, c.secret))
    if verify:
        with httpx2.Client(timeout=httpx2.Timeout(10.0)) as http:
            quiet = Recorder.quiet(all_secrets)
            return verify_key(Probe(BybitProbeClient("demo", demo, http), quiet))
    rec = Recorder(FIXTURE_DIR, all_secrets)
    rec.say("start", demo_key_present=True, testnet_key_present=testnet is not None)
    with httpx2.Client(timeout=httpx2.Timeout(10.0)) as http:
        probe = Probe(BybitProbeClient("demo", demo, http), rec)
        probe.read_only = read_only
        try:
            safe = probe.preflight()
            probe.btc_baseline = probe.spot_free_btc()
            if safe and not read_only:
                probe.traded = True
                for name, step in steps(probe):
                    try:
                        step()
                    except Exception as exc:  # a failed step must not skip the others or cleanup
                        rec.say(name, error=f"{type(exc).__name__}: {exc}")
            testnet_check(testnet, http, rec)
        finally:
            if probe.traded:
                rec.say("cleanup", left_on_account=probe.cleanup())
            rec.flush()
    leaks = find_secret_leaks(FIXTURE_DIR, all_secrets)
    lengths = sorted({len(s) for s in all_secrets})
    rec.say("secret_scan", files_with_secrets=leaks, checked_secret_lengths=lengths)
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
