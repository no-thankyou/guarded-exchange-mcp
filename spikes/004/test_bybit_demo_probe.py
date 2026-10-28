"""Offline checks for the spike 004 probe: safety rails and recorded fixtures (no network).

The probe itself talks to Bybit Demo and is run by hand; these tests prove that it signs what it
sends, cannot leave the demo/testnet allowlist, and never writes key material. Run:
``uv run pytest spikes/004 -v``.
"""

import hashlib
import hmac
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from bybit_demo_probe import (
    FIXTURE_DIR,
    REDACT_KEYS,
    REDACTED,
    BybitProbeClient,
    Credentials,
    ForbiddenHostError,
    Probe,
    Recorder,
    base_url,
    check_host,
    credentials_for,
    d24_extra_permissions,
    find_secret_leaks,
    key_file_problem,
    parse_env_file,
    permission_names,
    redacted_placeholder,
    scrub,
    sign,
    to_step,
    verify_key,
)

KEY = "k3yK3yK3yK3yK3yK3y"
SECRET = "s3cr3t-s3cr3t-s3cr3t-s3cr3t-s3cr3t-x"  # noqa: S105 - fake value for offline tests


def _client(seen: list[httpx2.Request], body: dict[str, Any] | None = None) -> BybitProbeClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=body or {"retCode": 0, "retMsg": "OK", "result": {}})

    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return BybitProbeClient("demo", Credentials(KEY, SECRET), http)


def _expected_sign(request: httpx2.Request, payload: str) -> str:
    ts, rw = request.headers["X-BAPI-TIMESTAMP"], request.headers["X-BAPI-RECV-WINDOW"]
    return hmac.new(SECRET.encode(), f"{ts}{KEY}{rw}{payload}".encode(), hashlib.sha256).hexdigest()


def test_sign_is_hmac_sha256_over_timestamp_key_window_payload() -> None:
    expected = hmac.new(b"sec", b"1700000000000key5000a=1", hashlib.sha256).hexdigest()
    assert sign("sec", "1700000000000", "key", "5000", "a=1") == expected


def test_get_signature_covers_the_query_string_actually_sent() -> None:
    seen: list[httpx2.Request] = []
    _client(seen).call("GET", "/v5/order/realtime", {"category": "linear", "orderLinkId": "gx-1"})
    request = seen[0]
    assert request.url.host == "api-demo.bybit.com"
    query = request.url.query.decode() if isinstance(request.url.query, bytes) else request.url.query
    assert query == "category=linear&orderLinkId=gx-1"
    assert request.headers["X-BAPI-SIGN"] == _expected_sign(request, query)


def test_post_signature_covers_the_body_actually_sent() -> None:
    seen: list[httpx2.Request] = []
    _client(seen).call("POST", "/v5/order/create", {"category": "linear", "qty": "0.001"})
    request = seen[0]
    body = request.content.decode()
    assert json.loads(body) == {"category": "linear", "qty": "0.001"}
    assert request.headers["X-BAPI-SIGN"] == _expected_sign(request, body)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.bybit.com/v5/order/create",
        "https://api.bytick.com/x",
        "https://api-demo.bybit.com.evil.io/",
    ],
)
def test_hosts_outside_the_allowlist_are_refused(url: str) -> None:
    with pytest.raises(ForbiddenHostError):
        check_host(url)


def test_only_demo_and_testnet_environments_exist() -> None:
    assert base_url("demo") == "https://api-demo.bybit.com"
    assert base_url("testnet") == "https://api-testnet.bybit.com"
    with pytest.raises(ForbiddenHostError):
        base_url("mainnet")


def test_env_file_parsing_and_masked_credentials() -> None:
    values = parse_env_file(
        "# comment\nexport BYBIT_DEMO_API_KEY='abc'\nBYBIT_DEMO_API_SECRET=\"def\"\n\nBROKEN LINE\n"
    )
    creds = credentials_for("demo", values)
    assert creds == Credentials("abc", "def")
    assert "abc" not in repr(creds) and "def" not in repr(creds)
    assert credentials_for("testnet", values) is None


def test_scrub_redacts_identity_fields_and_any_secret_occurrence() -> None:
    raw = {
        "result": {
            "apiKey": "visible-only-by-field-name",
            "ips": ["1.2.3.4"],
            "userID": 42,
            "parentUid": "42",
            "inner": {"ips": []},
            "nested": [{"echo": f"prefix-{KEY}-suffix", "fine": "BTCUSDT"}],
            "readOnly": 0,
        }
    }
    clean = scrub(raw, (KEY, SECRET))
    assert clean["result"]["apiKey"] == REDACTED
    assert clean["result"]["ips"] == [REDACTED]  # type kept, non-empty kept
    assert clean["result"]["inner"]["ips"] == []  # an unbound key stays visibly unbound
    assert clean["result"]["userID"] == 0
    assert clean["result"]["parentUid"] == 0
    assert clean["result"]["nested"] == [{"echo": REDACTED, "fine": "BTCUSDT"}]
    assert clean["result"]["readOnly"] == 0


@pytest.mark.parametrize(
    ("value", "step", "up", "expected"),
    [
        ("68796.66", "0.1", False, "68796.6"),
        ("68796.61", "0.1", True, "68796.7"),
        ("0.0001233", "0.000001", True, "0.000124"),
        ("5", "0.001", False, "5.000"),
    ],
)
def test_to_step(value: str, step: str, up: bool, expected: str) -> None:
    assert to_step(Decimal(value), Decimal(step), up=up) == Decimal(expected)


def test_recorder_never_writes_secrets_or_request_headers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx2.Request] = []
    client = _client(seen, {"retCode": 0, "result": {"apiKey": KEY, "secret": SECRET, "readOnly": 0}})
    rec = Recorder(tmp_path, (KEY, SECRET))
    reply = client.call("GET", "/v5/user/query-api")
    rec.record("q", client, "GET", "/v5/user/query-api", {}, reply=reply)
    rec.flush()
    assert find_secret_leaks(tmp_path, (KEY, SECRET)) == []
    assert KEY not in capsys.readouterr().out
    index = (tmp_path / "index.json").read_text().lower()
    assert "x-bapi-sign" not in index and "x-bapi-api-key" not in index and "x-bapi-timestamp" not in index


def test_find_secret_leaks_reports_file_names(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_text("{}")
    (tmp_path / "bad.json").write_text(f'{{"k": "{SECRET}"}}')
    assert find_secret_leaks(tmp_path, (KEY, SECRET)) == ["bad.json"]


# --- the recorded fixtures (tests/fixtures/bybit/spike004) -------------------------------------


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURE_DIR.glob("*.json") if p.name != "index.json")


def _identity_values(node: Any) -> list[Any]:
    if isinstance(node, dict):
        found = [v for k, v in node.items() if k in REDACT_KEYS and v != redacted_placeholder(k, v)]
        return found + [x for v in node.values() for x in _identity_values(v)]
    if isinstance(node, list):
        return [x for v in node for x in _identity_values(v)]
    return []


def test_recorded_fixtures_are_bybit_envelopes_without_identity_fields() -> None:
    files = _fixture_files()
    assert files, "run spikes/004/bybit_demo_probe.py to record fixtures"
    for path in files:
        body = json.loads(path.read_text())
        assert isinstance(body.get("retCode"), int), path.name
        assert _identity_values(body) == [], path.name


def test_query_api_fixture_keeps_json_types_of_redacted_fields() -> None:
    result = json.loads((FIXTURE_DIR / "demo_query_api.json").read_text())["result"]
    assert isinstance(result["ips"], list)
    assert result["userID"] == result["parentUid"] == 0
    assert result["apiKey"] == result["secret"] == REDACTED


def test_recorded_demo_key_fails_d24_with_exact_extra_permissions() -> None:
    result = json.loads((FIXTURE_DIR / "demo_query_api.json").read_text())["result"]
    assert d24_extra_permissions(result["permissions"]) == {
        "Derivatives": ["DerivativesTrade"],
        "Exchange": ["ExchangeHistory"],
        "Options": ["OptionsTrade"],
        "Wallet": ["AccountTransfer", "SubMemberTransferList"],
    }


def test_index_describes_every_fixture_and_only_allowlisted_hosts() -> None:
    document = json.loads((FIXTURE_DIR / "index.json").read_text())
    assert document["run_number"] >= 1 and document["history"]
    index = document["entries"]
    described = {entry["fixture"] for entry in index}
    assert {p.name for p in _fixture_files()} - described <= {"demo_apply_money.json"}
    assert {entry["host"] for entry in index} <= {"api-demo.bybit.com", "api-testnet.bybit.com"}
    for entry in index:
        assert set(entry["rate_limit_headers"]) <= {
            "x-bapi-limit",
            "x-bapi-limit-status",
            "x-bapi-limit-reset-timestamp",
        }


# --- review round 1: key file, D-24 verdict, --verify-key, index runs ---------------------------


def test_key_file_must_not_be_readable_by_group_or_others(tmp_path: Path) -> None:
    path = tmp_path / "bybit-demo.env"
    path.write_text("BYBIT_DEMO_API_KEY=x\n")
    path.chmod(0o600)
    assert key_file_problem(path) is None
    path.chmod(0o640)
    problem = key_file_problem(path)
    assert problem is not None and "0o640" in problem


def test_d24_allowlist_ignores_empty_groups_and_flags_anything_else() -> None:
    ok = {"ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"], "NFT": [], "Wallet": []}
    assert d24_extra_permissions(ok) == {}
    assert permission_names(ok) == {"ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"]}
    assert d24_extra_permissions({"Spot": ["SpotTrade", "Unknown"], "Earn": ["Earn"]}) == {
        "Spot": ["Unknown"],
        "Earn": ["Earn"],
    }


def test_flush_numbers_runs_and_keeps_history(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, ())
    rec.flush()
    Recorder(tmp_path, ()).flush()
    document = json.loads((tmp_path / "index.json").read_text())
    assert document["run_number"] == 2
    assert len(document["history"]) == 2


class FakeDemo:
    """Just enough of Bybit demo for ``verify_key``: query-api, instruments, ticker, create/cancel/read."""

    def __init__(self, permissions: dict[str, list[str]]) -> None:
        self.permissions = permissions
        self.orders: dict[str, dict[str, Any]] = {}
        self.paths: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        self.paths.append(path)
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else {}
        result: dict[str, Any] = {}
        if path == "/v5/market/time":
            return httpx2.Response(200, json={"retCode": 0, "time": 0})
        if path == "/v5/user/query-api":
            result = {"apiKey": KEY, "readOnly": 0, "permissions": self.permissions, "ips": []}
        elif path == "/v5/market/instruments-info":
            lot = (
                {"qtyStep": "0.001", "minOrderQty": "0.001"}
                if params["category"] == "linear"
                else {"basePrecision": "0.000001", "minOrderQty": "0.000001", "minOrderAmt": "5"}
            )
            result = {"list": [{"priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": lot}]}
        elif path == "/v5/market/tickers":
            result = {"list": [{"bid1Price": "80000", "ask1Price": "80000.1"}]}
        elif path == "/v5/order/create":
            self.orders[body["orderLinkId"]] = {"orderLinkId": body["orderLinkId"], "orderStatus": "New"}
        elif path == "/v5/order/cancel":
            self.orders[body["orderLinkId"]]["orderStatus"] = "Cancelled"
        elif path in {"/v5/order/realtime", "/v5/order/history"}:
            found = self.orders.get(params["orderLinkId"])
            result = {"list": [found] if found else []}
        return httpx2.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


def _verify(fake: FakeDemo, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    client = BybitProbeClient(
        "demo", Credentials(KEY, SECRET), httpx2.Client(transport=httpx2.MockTransport(fake))
    )
    code = verify_key(Probe(client, Recorder.quiet((KEY, SECRET))))
    out = capsys.readouterr().out
    assert KEY not in out and SECRET not in out
    verdict = next(json.loads(line) for line in out.splitlines() if '"verify_key_verdict"' in line)
    return code, verdict


def test_verify_key_passes_a_d24_key_that_trades(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    fake = FakeDemo({"ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"], "Wallet": []})
    code, verdict = _verify(fake, capsys)
    assert code == 0
    assert verdict["passes_d24"] is True and verdict["trades"] is True
    assert all(o["orderStatus"] == "Cancelled" for o in fake.orders.values()) and len(fake.orders) == 2


def test_verify_key_rejects_a_key_with_transfer_rights(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    fake = FakeDemo({"ContractTrade": ["Order", "Position"], "Wallet": ["AccountTransfer"]})
    code, verdict = _verify(fake, capsys)
    assert code == 1
    assert verdict["passes_d24"] is False
