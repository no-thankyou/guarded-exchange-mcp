"""Command-line entrypoint. Commands receive their behaviour from the composition root."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer

Transport = Literal["stdio"]
Serve = Callable[[Transport, Path | None], None]


@dataclass(frozen=True, slots=True)
class InitReport:
    """What ``init`` did; the composition root fills it, this layer only renders it."""

    db_path: Path
    schema_revision: str
    master_key_file: Path
    master_key_created: bool
    user_name: str
    user_id: str
    user_created: bool


Init = Callable[[Path | None], InitReport]

_ENV_FILE_HELP = "dotenv file with GX_* settings (else $GX_ENV_FILE; no implicit ./.env)."


def render_init_report(report: InitReport) -> str:
    key_state = "created - back it up" if report.master_key_created else "kept existing"
    user_state = "created" if report.user_created else "already existed"
    return "\n".join(
        [
            f"database:   {report.db_path} (schema {report.schema_revision})",
            f"master key: {report.master_key_file} ({key_state})",
            f"local user: {report.user_name} [{report.user_id}] ({user_state})",
        ]
    )


def build_cli(serve: Serve, init: Init) -> typer.Typer:
    app = typer.Typer(name="guarded-exchange", no_args_is_help=True, add_completion=False)

    @app.callback()
    def main() -> None:
        """Guarded MCP server for a crypto exchange."""

    @app.command("serve")
    def serve_command(
        transport: Annotated[
            str, typer.Option(help="MCP transport. Only stdio is available in this version.")
        ] = "stdio",
        env_file: Annotated[Path | None, typer.Option(help=_ENV_FILE_HELP, dir_okay=False)] = None,
    ) -> None:
        """Run the MCP server (stdout = protocol, stderr = JSON logs)."""
        if transport != "stdio":
            raise typer.BadParameter("only 'stdio' is supported yet", param_hint="--transport")
        serve("stdio", env_file)

    @app.command("init")
    def init_command(
        env_file: Annotated[Path | None, typer.Option(help=_ENV_FILE_HELP, dir_okay=False)] = None,
    ) -> None:
        """Create or migrate the database, the master key and the local user. Safe to re-run."""
        typer.echo(render_init_report(init(env_file)))

    return app
