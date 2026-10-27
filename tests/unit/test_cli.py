from typer.testing import CliRunner

from guarded_exchange.interface.cli.main import Transport, build_cli

runner = CliRunner()


def test_serve_defaults_to_stdio() -> None:
    calls: list[Transport] = []

    result = runner.invoke(build_cli(calls.append), ["serve"])

    assert result.exit_code == 0, result.output
    assert calls == ["stdio"]


def test_serve_rejects_unsupported_transport() -> None:
    calls: list[Transport] = []

    result = runner.invoke(build_cli(calls.append), ["serve", "--transport", "streamable-http"])

    assert result.exit_code != 0
    assert calls == []
