"""Process configuration from environment variables (prefix ``GX_``) and an optional ``.env``."""

from typing import Literal

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GX_", env_file=".env", extra="ignore")

    log_level: LogLevel = "INFO"

    # Bybit V5 REST. Default is Demo Trading (PLAN ADR-001); tests point it at a local stub.
    bybit_base_url: HttpUrl = HttpUrl("https://api-demo.bybit.com")
    bybit_connect_timeout_s: float = Field(default=3.0, gt=0, le=30)
    bybit_read_timeout_s: float = Field(default=5.0, gt=0, le=60)
    bybit_get_max_attempts: int = Field(default=3, ge=1, le=5)
    bybit_get_backoff_base_s: float = Field(default=0.2, ge=0, le=5)
