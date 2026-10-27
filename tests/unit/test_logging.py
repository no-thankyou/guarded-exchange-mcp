import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog
from pydantic import SecretStr

from guarded_exchange.infrastructure.logging import MASK, configure_logging, mask_secrets

pytestmark = pytest.mark.req("NFR-3")


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    yield stream
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_mask_secrets_by_key_name_and_type() -> None:
    event = {
        "event": "x",
        "api_key": "AKIA",
        "apiSecret": "s3cr3t",
        "Authorization": "Bearer abc",
        "master_key": b"k",
        "nested": {"password": "p", "symbol": "BTCUSDT"},
        "value": SecretStr("hidden"),
        "items": [SecretStr("a"), "b"],
        "symbol": "BTCUSDT",
    }

    masked = mask_secrets(None, "info", event)

    assert masked == {
        "event": "x",
        "api_key": MASK,
        "apiSecret": MASK,
        "Authorization": MASK,
        "master_key": MASK,
        "nested": {"password": MASK, "symbol": "BTCUSDT"},
        "value": MASK,
        "items": [MASK, "b"],
        "symbol": "BTCUSDT",
    }


def test_structlog_events_are_json_with_bound_request_id_and_no_secrets(log_stream: io.StringIO) -> None:
    log = structlog.get_logger("test")
    with structlog.contextvars.bound_contextvars(request_id="req-1"):
        log.info("tool_ok", api_secret="s3cr3t", symbol="BTCUSDT")  # noqa: S106 - fake secret

    (line,) = _lines(log_stream)
    assert line["event"] == "tool_ok"
    assert line["request_id"] == "req-1"
    assert line["api_secret"] == MASK
    assert line["level"] == "info"
    assert "s3cr3t" not in log_stream.getvalue()


def test_stdlib_loggers_are_rendered_as_json_too(log_stream: io.StringIO) -> None:
    logging.getLogger("mcp.server").warning("sdk says %s", "hi")

    (line,) = _lines(log_stream)
    assert line["event"] == "sdk says hi"
    assert line["logger"] == "mcp.server"
