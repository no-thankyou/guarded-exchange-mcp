"""Structured JSON logging to stderr (NFR-3).

On the stdio transport stdout carries the MCP protocol only, so every log line - ours and
the standard-library loggers of the MCP SDK, httpx2, etc. - is rendered as one JSON object
on stderr. Context variables (``request_id``, ``tool``) bound by the interface layer are
merged into every event. Secrets are masked by key name and by type before rendering.
"""

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, Final, TextIO

import structlog
from pydantic import SecretBytes, SecretStr
from structlog.typing import EventDict, Processor, WrappedLogger

from guarded_exchange.domain.credentials import ApiKeyPair

MASK: Final = "***"
_SECRET_KEY_RE: Final = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|authorization|signature|master[_-]?key|cookie)",
    re.IGNORECASE,
)


def mask_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Replace values whose key looks secret, and any ``SecretStr``/``SecretBytes``/``ApiKeyPair`` value."""
    return _mask_mapping(event_dict)


def _mask_mapping(data: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key, value in list(data.items()):
        data[key] = MASK if _SECRET_KEY_RE.search(str(key)) else _mask_value(value)
    return data


def _mask_value(value: Any) -> Any:
    if isinstance(value, SecretStr | SecretBytes | ApiKeyPair):
        return MASK
    if isinstance(value, Mapping):
        return _mask_mapping(dict(value))
    if isinstance(value, list | tuple):
        return [_mask_value(v) for v in value]
    return value


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Route structlog and stdlib logging to ``stream`` (stderr by default) as JSON lines.

    Must run before the MCP server is constructed: ``MCPServer`` calls
    ``logging.basicConfig``, which is a no-op once the root logger has a handler.
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        mask_secrets,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,  # reconfigurable in tests; negligible cost
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[structlog.stdlib.ExtraAdder(), *shared],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
