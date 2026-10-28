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
    Recorder,
    base_url,
    check_host,
    credentials_for,
    find_secret_leaks,
    parse_env_file,
    scrub,
    sign,
    to_step,
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
            "nested": [{"echo": f"prefix-{KEY}-suffix", "fine": "BTCUSDT"}],
            "readOnly": 0,
        }
    }
    clean = scrub(raw, (KEY, SECRET))
    assert clean["result"]["apiKey"] == REDACTED
    assert clean["result"]["ips"] == REDACTED
    assert clean["result"]["userID"] == REDACTED
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
        found = [v for k, v in node.items() if k in REDACT_KEYS and v != REDACTED]
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


def test_index_describes_every_fixture_and_only_allowlisted_hosts() -> None:
    index = json.loads((FIXTURE_DIR / "index.json").read_text())
    described = {entry["fixture"] for entry in index}
    assert {p.name for p in _fixture_files()} - described <= {"demo_apply_money.json"}
    assert {entry["host"] for entry in index} <= {"api-demo.bybit.com", "api-testnet.bybit.com"}
    for entry in index:
        assert set(entry["rate_limit_headers"]) <= {
            "x-bapi-limit",
            "x-bapi-limit-status",
            "x-bapi-limit-reset-timestamp",
        }
