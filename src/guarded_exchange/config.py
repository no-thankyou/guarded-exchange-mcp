"""Process configuration from environment variables (prefix ``GX_``).

A dotenv file is read only from an explicit path (``--env-file`` or ``GX_ENV_FILE``), never
implicitly from the current directory: an MCP client may start the server in any working
directory, and a stray ``.env`` there must not silently change where the server sends requests.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, HttpUrl, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
ENV_FILE_VAR: Final = "GX_ENV_FILE"
APP_DIR: Final = "guarded-exchange"


def _xdg_dir(variable: str, fallback: str) -> Path:
    raw = os.environ.get(variable, "").strip()
    base = Path(raw) if raw and Path(raw).is_absolute() else Path.home() / fallback
    return base / APP_DIR


def default_db_path() -> Path:
    return _xdg_dir("XDG_DATA_HOME", ".local/share") / "guarded-exchange.db"


def default_master_key_file() -> Path:
    # A different directory from the database on purpose (PLAN ADR-009): copying or sharing
    # the data directory must not hand over the key that decrypts it.
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / "master.key"


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
    # Instrument rules (tick size, qty step, minimums) are cached in memory for this long.
    instrument_rules_ttl_s: int = Field(default=600, ge=1, le=86_400)

    # Local storage (PLAN ADR-007): SQLite file, master key file outside its directory.
    db_path: Path = Field(default_factory=default_db_path)
    master_key_file: Path = Field(default_factory=default_master_key_file)
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    # Local mode has one principal: this user, created by `guarded-exchange init` (ADR-004).
    local_user: str = Field(default="local", min_length=1, max_length=64)

    # Remote mode (ADR-003/004): `serve --transport http`. Loopback by default; to expose it, bind
    # another address behind a TLS-terminating reverse proxy and list the public Host header.
    http_host: str = Field(default="127.0.0.1", min_length=1)
    http_port: int = Field(default=8000, ge=1, le=65_535)
    # Host/Origin headers accepted on /mcp (DNS rebinding protection is always on); `host:*` = any port.
    http_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1:*", "localhost:*", "[::1]:*"], min_length=1
    )
    http_allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    )

    @field_validator("db_path", "master_key_file")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        # MCP clients start the server from arbitrary directories: a relative path would
        # silently point at a different (empty) database depending on the working directory.
        value = value.expanduser()
        if not value.is_absolute():
            raise ValueError(f"must be an absolute path, got {str(value)!r}")
        return value

    @model_validator(mode="after")
    def _key_outside_db_dir(self) -> "Settings":
        db_dir = self.db_path.parent.resolve()
        if self.master_key_file.resolve().is_relative_to(db_dir):
            raise ValueError(
                f"GX_MASTER_KEY_FILE must be outside the database directory {db_dir} "
                "(a copy of the data directory must not contain its key)"
            )
        return self


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
    try:
        return Settings(_env_file=path)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, err['loc'])) or 'settings'}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(problems) from exc
