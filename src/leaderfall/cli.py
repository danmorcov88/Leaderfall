"""Command-line entry point.

Commands are added phase by phase:

- Phase 1: ``up``, ``status``, ``down``
- Phase 2/3: ``run``
- Phase 5: ``report``
"""

import json
import subprocess
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from leaderfall import __version__
from leaderfall.cluster import (
    HAPROXY_READ_PORT,
    HAPROXY_STATS_PORT,
    HAPROXY_WRITE_PORT,
    Cluster,
    ClusterConfig,
    ClusterState,
    ClusterUnreachableError,
    PatroniClient,
    PatroniProfile,
    SyncMode,
    WaitTimeoutError,
    expected_sync_standbys,
)

app = typer.Typer(
    name="leaderfall",
    help="Break a PostgreSQL HA cluster on purpose, and prove with numbers that it survives.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)


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


def _log(message: str) -> None:
    err.print(f"[dim]>[/dim] {message}")


def _fail(message: str, code: int = 1) -> None:
    err.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code)


def _topology_table(state: ClusterState) -> Table:
    table = Table(title=f"cluster '{state.scope}'", title_justify="left")
    table.add_column("node")
    table.add_column("role")
    table.add_column("state")
    table.add_column("timeline", justify="right")
    table.add_column("lag (bytes)", justify="right")
    for m in sorted(state.members, key=lambda m: m.name):
        role_style = "bold green" if m.is_leader else ""
        state_style = "" if m.state in {"running", "streaming"} else "yellow"
        table.add_row(
            m.name,
            f"[{role_style}]{m.role}[/]" if role_style else m.role,
            f"[{state_style}]{m.state}[/]" if state_style else m.state,
            "" if m.timeline is None else str(m.timeline),
            "" if m.is_leader else str(m.lag if m.lag is not None else "?"),
        )
    return table


@app.command()
def up(
    profile: Annotated[
        PatroniProfile,
        typer.Option(help="Patroni timing profile: default = ttl 30/10/10, fast = 15/5/5."),
    ] = PatroniProfile.DEFAULT,
    sync: Annotated[
        SyncMode,
        typer.Option(help="Synchronous replication: off, on, or strict."),
    ] = SyncMode.OFF,
    timeout: Annotated[float, typer.Option(help="Seconds to wait for a healthy cluster.")] = 180.0,
    build: Annotated[bool, typer.Option(help="Build the Patroni image first.")] = True,
) -> None:
    """Start the cluster and wait until it has one leader and two streaming replicas."""
    config = ClusterConfig(profile=profile, sync=sync)
    cluster = Cluster(log=_log)
    try:
        result = cluster.up(config, timeout=timeout, build=build)
    except FileNotFoundError as exc:
        _fail(str(exc))
    except subprocess.CalledProcessError as exc:
        _fail(f"docker compose failed with exit code {exc.returncode}")
    except WaitTimeoutError as exc:
        _fail(str(exc))
    else:
        console.print(_topology_table(result.state))
        console.print(
            f"cluster healthy in {result.seconds_to_healthy:.1f}s "
            f"(total {result.seconds_total:.1f}s), profile={config.profile}, sync={config.sync}"
        )
        if result.config_patched:
            console.print(f"dynamic config updated: {result.config_patched}")
        console.print(
            f"writes: localhost:{HAPROXY_WRITE_PORT}  reads: localhost:{HAPROXY_READ_PORT}  "
            f"stats: http://localhost:{HAPROXY_STATS_PORT}/"
        )


@app.command()
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON.")] = False,
) -> None:
    """Show the cluster topology: node, role, state, timeline, lag."""
    patroni = PatroniClient()
    try:
        state = patroni.cluster()
        config = patroni.config()
    except ClusterUnreachableError as exc:
        _fail(str(exc), code=2)
    else:
        if as_json:
            console.print_json(json.dumps({"cluster": state.model_dump(), "config": config}))
            return
        console.print(_topology_table(state))
        sync_standbys = expected_sync_standbys(config)
        console.print(
            f"ttl={config.get('ttl')} loop_wait={config.get('loop_wait')} "
            f"retry_timeout={config.get('retry_timeout')} "
            f"synchronous_mode={config.get('synchronous_mode')} "
            f"strict={config.get('synchronous_mode_strict')}"
        )
        problems = state.health_problems(sync_standbys=sync_standbys)
        if problems:
            for p in problems:
                console.print(f"[yellow]![/yellow] {p}")
            raise typer.Exit(3)
        sync_note = f", {sync_standbys} sync standby" if sync_standbys else ""
        console.print(
            f"[green]healthy[/green]: 1 leader, 2 streaming replicas{sync_note}, same timeline"
        )


@app.command()
def down(
    volumes: Annotated[
        bool, typer.Option("--volumes", help="Also delete the data volumes.")
    ] = False,
) -> None:
    """Stop the cluster. Data volumes are kept unless --volumes is given."""
    try:
        Cluster(log=_log).down(volumes=volumes)
    except FileNotFoundError as exc:
        _fail(str(exc))
    except subprocess.CalledProcessError as exc:
        _fail(f"docker compose failed with exit code {exc.returncode}")


if __name__ == "__main__":
    app()
