"""The real server process over stdio: ``python -m guarded_exchange serve``.

The exchange is replaced through configuration (``GX_BYBIT_BASE_URL``) by a loopback HTTP
stub that replays recorded Bybit responses, so the real adapter runs and nothing leaves the
machine. ``tee`` copies the server's raw stdout to a file to prove it carries only JSON-RPC.
"""

import json
import shutil
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import jsonrpc_message_adapter

from tests.conftest import FIXTURES

pytestmark = [
    pytest.mark.req("FR-1", "NFR-2", "NFR-3"),
    pytest.mark.skipif(shutil.which("sh") is None or shutil.which("tee") is None, reason="needs sh and tee"),
]


class _BybitStub(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/v5/market/tickers" and query == {"category": "linear", "symbol": "BTCUSDT"}:
            body = (FIXTURES / "bybit/tickers_linear_btcusdt.json").read_bytes()
        else:
            body = (FIXTURES / "bybit/error_10001_not_supported_symbol.json").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep pytest output clean."""


@pytest.fixture
def bybit_stub_url() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _BybitStub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


async def test_stdio_server_answers_and_keeps_stdout_protocol_only(
    tmp_path: Path, bybit_stub_url: str
) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    params = StdioServerParameters(
        command="sh",
        args=["-c", '"$GX_TEST_PY" -m guarded_exchange serve 2>"$GX_TEST_ERR" | tee "$GX_TEST_OUT"'],
        env={
            "GX_TEST_PY": sys.executable,
            "GX_TEST_ERR": str(stderr_log),
            "GX_TEST_OUT": str(stdout_log),
            "GX_BYBIT_BASE_URL": bybit_stub_url,
            "GX_BYBIT_GET_MAX_ATTEMPTS": "1",
            "GX_LOG_LEVEL": "INFO",
            "PYTHONPATH": "",
        },
        cwd=tmp_path,  # no stray .env is picked up
    )

    async with Client(params, read_timeout_seconds=20) as client:
        tools = await client.list_tools()
        ok = await client.call_tool("get_ticker", {"symbol": "BTCUSDT", "category": "linear"})
        err = await client.call_tool("get_ticker", {"symbol": "NOPEUSDT", "category": "spot"})

    assert [t.name for t in tools.tools] == ["get_ticker"]
    assert ok.is_error is False
    assert ok.structured_content is not None
    assert ok.structured_content["last_price"] == "80571.1"
    assert err.is_error is True
    assert err.structured_content is not None
    assert err.structured_content["code"] == "invalid_params"

    # stdout: every line is a JSON-RPC message, nothing else
    stdout_lines = [line for line in stdout_log.read_text().splitlines() if line.strip()]
    assert stdout_lines, "server wrote nothing to stdout"
    for line in stdout_lines:
        jsonrpc_message_adapter.validate_json(line)

    # stderr: JSON log lines with a request id for each tool call
    events = [json.loads(line) for line in stderr_log.read_text().splitlines() if line.strip()]
    names = [e["event"] for e in events]
    assert "server_starting" in names
    tool_events = [e for e in events if e["event"] in {"tool_ok", "tool_error"}]
    assert len(tool_events) == 2
    assert all(e["request_id"] and e["tool"] == "get_ticker" for e in tool_events)
