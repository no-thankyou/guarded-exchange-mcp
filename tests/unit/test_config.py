from pathlib import Path

import pytest

from guarded_exchange.config import ConfigError, load_settings, resolve_env_file


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "GX_ENV_FILE",
        "GX_LOG_LEVEL",
        "GX_BYBIT_BASE_URL",
        "GX_DB_PATH",
        "GX_MASTER_KEY_FILE",
        "GX_LOCAL_USER",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
    ):
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


def test_default_storage_paths_follow_xdg_and_keep_the_key_outside_the_db_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    settings = load_settings(environ={})

    assert settings.db_path == tmp_path / "data/guarded-exchange/guarded-exchange.db"
    assert settings.master_key_file == tmp_path / "config/guarded-exchange/master.key"
    assert settings.local_user == "local"


def test_master_key_inside_the_db_directory_is_refused(tmp_path: Path) -> None:
    env = tmp_path / "gx.env"
    env.write_text(f"GX_DB_PATH={tmp_path}/data/gx.db\nGX_MASTER_KEY_FILE={tmp_path}/data/keys/master.key\n")

    with pytest.raises(ConfigError, match="outside the database directory"):
        load_settings(env, environ={})


def test_relative_storage_paths_are_refused(tmp_path: Path) -> None:
    env = tmp_path / "gx.env"
    env.write_text("GX_DB_PATH=data/gx.db\n")

    with pytest.raises(ConfigError, match="absolute path"):
        load_settings(env, environ={})


def test_home_relative_paths_are_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    env = tmp_path / "gx.env"
    env.write_text("GX_DB_PATH=~/gx/gx.db\nGX_MASTER_KEY_FILE=~/keys/master.key\n")

    settings = load_settings(env, environ={})

    assert settings.db_path == tmp_path / "gx/gx.db"
    assert settings.master_key_file == tmp_path / "keys/master.key"


def test_invalid_values_become_config_errors(tmp_path: Path) -> None:
    env = tmp_path / "gx.env"
    env.write_text("GX_LOG_LEVEL=LOUD\n")

    with pytest.raises(ConfigError, match="log_level"):
        load_settings(env, environ={})
