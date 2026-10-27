from pathlib import Path

from typer.testing import CliRunner

from guarded_exchange.interface.cli.main import Transport, build_cli

runner = CliRunner()


class ServeSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[Transport, Path | None]] = []

    def __call__(self, transport: Transport, env_file: Path | None) -> None:
        self.calls.append((transport, env_file))


def test_serve_defaults_to_stdio_without_env_file() -> None:
    spy = ServeSpy()

    result = runner.invoke(build_cli(spy), ["serve"])

    assert result.exit_code == 0, result.output
    assert spy.calls == [("stdio", None)]


def test_serve_passes_explicit_env_file() -> None:
    spy = ServeSpy()

    result = runner.invoke(build_cli(spy), ["serve", "--env-file", "conf/gx.env"])

    assert result.exit_code == 0, result.output
    assert spy.calls == [("stdio", Path("conf/gx.env"))]


def test_serve_rejects_unsupported_transport() -> None:
    spy = ServeSpy()

    result = runner.invoke(build_cli(spy), ["serve", "--transport", "streamable-http"])

    assert result.exit_code != 0
    assert spy.calls == []
