from pathlib import Path

import pytest

from guarded_exchange.config import ConfigError, load_settings, resolve_env_file


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GX_ENV_FILE", "GX_LOG_LEVEL", "GX_BYBIT_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_dotenv_in_current_directory_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("GX_BYBIT_BASE_URL=http://evil.example\nGX_LOG_LEVEL=DEBUG\n")
    monkeypatch.chdir(tmp_path)

    settings = load_settings(environ={})

    assert str(settings.bybit_base_url) == "https://api-demo.bybit.com/"
    assert settings.log_level == "INFO"


def test_explicit_env_file_is_read(tmp_path: Path) -> None:
    env = tmp_path / "gx.env"
    env.write_text("GX_LOG_LEVEL=DEBUG\n")

    assert load_settings(env, environ={}).log_level == "DEBUG"


def test_env_file_from_gx_env_file_variable(tmp_path: Path) -> None:
    env = tmp_path / "gx.env"
    env.write_text("GX_LOG_LEVEL=WARNING\n")

    assert load_settings(environ={"GX_ENV_FILE": str(env)}).log_level == "WARNING"


def test_cli_option_wins_over_variable(tmp_path: Path) -> None:
    cli_file = tmp_path / "cli.env"
    cli_file.write_text("")
    var_file = tmp_path / "var.env"
    var_file.write_text("")

    assert resolve_env_file(cli_file, {"GX_ENV_FILE": str(var_file)}) == cli_file


def test_missing_explicit_env_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.env", environ={})
    with pytest.raises(ConfigError):
        load_settings(environ={"GX_ENV_FILE": str(tmp_path / "nope.env")})
