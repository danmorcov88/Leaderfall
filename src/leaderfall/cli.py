"""Command-line entry point.

Commands are added phase by phase:

- Phase 1: ``up``, ``status``, ``down``
- Phase 2/3: ``run``
- Phase 5: ``report``
"""

from typing import Annotated

import typer
from rich.console import Console

from leaderfall import __version__

app = typer.Typer(
    name="leaderfall",
    help="Break a PostgreSQL HA cluster on purpose, and prove with numbers that it survives.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _print_version(value: bool) -> None:
    if value:
        console.print(f"leaderfall {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Print the version and exit.",
            callback=_print_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Break a PostgreSQL HA cluster on purpose, and prove with numbers that it survives."""


@app.command()
def version() -> None:
    """Print the leaderfall version."""
    _print_version(True)


if __name__ == "__main__":
    app()
