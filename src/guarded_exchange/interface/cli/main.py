"""Command-line entrypoint. Commands receive their behaviour from the composition root."""

from collections.abc import Callable
from typing import Annotated, Literal

import typer

Transport = Literal["stdio"]


def build_cli(serve: Callable[[Transport], None]) -> typer.Typer:
    app = typer.Typer(name="guarded-exchange", no_args_is_help=True, add_completion=False)

    @app.callback()
    def main() -> None:
        """Guarded MCP server for a crypto exchange."""

    @app.command("serve")
    def serve_command(
        transport: Annotated[
            str, typer.Option(help="MCP transport. Only stdio is available in this version.")
        ] = "stdio",
    ) -> None:
        """Run the MCP server (stdout = protocol, stderr = JSON logs)."""
        if transport != "stdio":
            raise typer.BadParameter("only 'stdio' is supported yet", param_hint="--transport")
        serve("stdio")

    return app
