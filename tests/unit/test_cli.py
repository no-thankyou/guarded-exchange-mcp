from pathlib import Path

from typer.testing import CliRunner

from guarded_exchange.interface.cli.main import InitReport, Transport, build_cli

runner = CliRunner()
REPORT = InitReport(
    db_path=Path("/data/gx.db"),
    schema_revision="0002",
    master_key_file=Path("/conf/master.key"),
    master_key_created=True,
    user_name="local",
    user_id="u-1",
    user_created=True,
)


class ServeSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[Transport, Path | None]] = []

    def __call__(self, transport: Transport, env_file: Path | None) -> None:
        self.calls.append((transport, env_file))


class InitSpy:
    def __init__(self, report: InitReport = REPORT) -> None:
        self.calls: list[Path | None] = []
        self.report = report

    def __call__(self, env_file: Path | None) -> InitReport:
        self.calls.append(env_file)
        return self.report


def test_serve_defaults_to_stdio_without_env_file() -> None:
    spy = ServeSpy()

    result = runner.invoke(build_cli(spy, InitSpy()), ["serve"])

    assert result.exit_code == 0, result.output
    assert spy.calls == [("stdio", None)]


def test_serve_passes_explicit_env_file() -> None:
    spy = ServeSpy()

    result = runner.invoke(build_cli(spy, InitSpy()), ["serve", "--env-file", "conf/gx.env"])

    assert result.exit_code == 0, result.output
    assert spy.calls == [("stdio", Path("conf/gx.env"))]


def test_serve_rejects_unsupported_transport() -> None:
    spy = ServeSpy()

    result = runner.invoke(build_cli(spy, InitSpy()), ["serve", "--transport", "streamable-http"])

    assert result.exit_code != 0
    assert spy.calls == []


def test_init_passes_env_file_and_prints_report() -> None:
    init = InitSpy()

    result = runner.invoke(build_cli(ServeSpy(), init), ["init", "--env-file", "conf/gx.env"])

    assert result.exit_code == 0, result.output
    assert init.calls == [Path("conf/gx.env")]
    assert "/data/gx.db (schema 0002)" in result.output
    assert "/conf/master.key (created - back it up)" in result.output
    assert "local [u-1] (created)" in result.output


def test_init_report_for_a_rerun_says_nothing_was_created() -> None:
    rerun = InitReport(
        db_path=Path("/data/gx.db"),
        schema_revision="0002",
        master_key_file=Path("/conf/master.key"),
        master_key_created=False,
        user_name="local",
        user_id="u-1",
        user_created=False,
    )

    result = runner.invoke(build_cli(ServeSpy(), InitSpy(rerun)), ["init"])

    assert result.exit_code == 0, result.output
    assert "(kept existing)" in result.output
    assert "(already existed)" in result.output
