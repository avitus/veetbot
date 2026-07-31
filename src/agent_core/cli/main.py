"""Top-level CLI command group."""

import typer

from agent_core import __version__

app = typer.Typer(
    name="agent",
    help="Modular general-purpose agent platform.",
    no_args_is_help=True,
)


def version_callback(value: bool) -> None:
    """Print the package version and stop command processing."""

    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show the installed version.",
    ),
) -> None:
    """Provide the shared command group; runtime commands arrive in Milestone 1."""

    del version
