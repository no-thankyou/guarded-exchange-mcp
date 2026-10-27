"""Process configuration from environment variables (prefix ``GX_``).

A dotenv file is read only from an explicit path (``--env-file`` or ``GX_ENV_FILE``), never
implicitly from the current directory: an MCP client may start the server in any working
directory, and a stray ``.env`` there must not silently change where the server sends requests.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
ENV_FILE_VAR: Final = "GX_ENV_FILE"


class ConfigError(Exception):
    """The configuration cannot be loaded; the server must not start (fail closed)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GX_", env_file=None, extra="ignore")

    log_level: LogLevel = "INFO"

    # Bybit V5 REST. Default is Demo Trading (PLAN ADR-001); tests point it at a local stub.
    bybit_base_url: HttpUrl = HttpUrl("https://api-demo.bybit.com")
    bybit_connect_timeout_s: float = Field(default=3.0, gt=0, le=30)
    bybit_read_timeout_s: float = Field(default=5.0, gt=0, le=60)
    bybit_get_max_attempts: int = Field(default=3, ge=1, le=5)
    bybit_get_backoff_base_s: float = Field(default=0.2, ge=0, le=5)


def resolve_env_file(env_file: Path | None, environ: Mapping[str, str]) -> Path | None:
    """``--env-file`` wins over ``GX_ENV_FILE``; no implicit ``.env``. A missing file is an error."""
    if env_file is None:
        raw = environ.get(ENV_FILE_VAR, "").strip()
        env_file = Path(raw) if raw else None
    if env_file is not None and not env_file.is_file():
        raise ConfigError(f"env file not found: {env_file}")
    return env_file


def load_settings(env_file: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    path = resolve_env_file(env_file, os.environ if environ is None else environ)
    return Settings(_env_file=path)
