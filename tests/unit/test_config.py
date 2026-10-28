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
        "GX_HTTP_HOST",
        "GX_HTTP_PORT",
        "GX_HTTP_ALLOWED_HOSTS",
        "GX_HTTP_ALLOWED_ORIGINS",
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


# --- remote mode (ticket 026) --------------------------------------------------------------


def test_http_defaults_listen_on_loopback_and_accept_only_loopback_hosts() -> None:
    settings = load_settings(environ={})

    assert (settings.http_host, settings.http_port) == ("127.0.0.1", 8000)
    assert settings.http_allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert settings.http_allowed_origins == ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def test_http_lists_are_read_as_json(tmp_path: Path) -> None:
    env = tmp_path / "gx.env"
    env.write_text(
        'GX_HTTP_ALLOWED_HOSTS=["mcp.example.com"]\nGX_HTTP_ALLOWED_ORIGINS=[]\nGX_HTTP_HOST=::1\n'
    )

    settings = load_settings(env, environ={})

    assert settings.http_allowed_hosts == ["mcp.example.com"]
    assert settings.http_allowed_origins == []  # no browser origin at all is a valid choice
    assert settings.http_host == "::1"


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ("GX_HTTP_ALLOWED_HOSTS=[]", "http_allowed_hosts"),  # would refuse every request: say so now
        ("GX_HTTP_HOST=http://0.0.0.0", "http_host"),
        ("GX_HTTP_HOST=0.0.0.0:8000", "http_host"),
        ("GX_HTTP_HOST=my host", "http_host"),
        ("GX_HTTP_HOST=-bad.example", "http_host"),
        ("GX_HTTP_PORT=0", "http_port"),
    ],
)
def test_bad_http_settings_are_configuration_errors(tmp_path: Path, line: str, field: str) -> None:
    env = tmp_path / "gx.env"
    env.write_text(line + "\n")

    with pytest.raises(ConfigError, match=field):
        load_settings(env, environ={})


@pytest.mark.parametrize("host", ["0.0.0.0", "10.1.2.3", "mcp-1.internal", "localhost"])  # noqa: S104
def test_http_host_accepts_addresses_and_host_names(tmp_path: Path, host: str) -> None:
    env = tmp_path / "gx.env"
    env.write_text(f"GX_HTTP_HOST={host}\n")

    assert load_settings(env, environ={}).http_host == host
