"""Command-line entrypoint. Commands receive their behaviour from the composition root."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

import typer

Transport = Literal["stdio"]
Serve = Callable[[Transport, Path | None], None]


def build_cli(serve: Serve) -> typer.Typer:
    app = typer.Typer(name="guarded-exchange", no_args_is_help=True, add_completion=False)

    @app.callback()
    def main() -> None:
        """Guarded MCP server for a crypto exchange."""

    @app.command("serve")
    def serve_command(
        transport: Annotated[
            str, typer.Option(help="MCP transport. Only stdio is available in this version.")
        ] = "stdio",
        env_file: Annotated[
            Path | None,
            typer.Option(
                help="dotenv file with GX_* settings (else $GX_ENV_FILE; no implicit ./.env).",
                dir_okay=False,
            ),
        ] = None,
    ) -> None:
        """Run the MCP server (stdout = protocol, stderr = JSON logs)."""
        if transport != "stdio":
            raise typer.BadParameter("only 'stdio' is supported yet", param_hint="--transport")
        serve("stdio", env_file)

    return app
