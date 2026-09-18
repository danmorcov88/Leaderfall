"""Command-line entry point.

Commands are added phase by phase:

- Phase 1: ``up``, ``status``, ``down``
- Phase 2/3: ``run``
- Phase 5: ``report``
"""

import json
import subprocess
from pathlib import Path
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
from leaderfall.report import write_run
from leaderfall.scenario import RunResult, ScenarioError, ScenarioRunner, WorkloadSpec

SCENARIOS = {"primary-sigkill"}

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


def _fmt(value: float | None, unit: str = "s") -> str:
    return "-" if value is None else f"{value:.1f}{unit}"


def _result_table(r: RunResult) -> Table:
    m = r.metrics
    table = Table(title=f"{r.scenario}: {r.description}", title_justify="left")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_column("note")
    table.add_row("old leader -> new leader", f"{m.old_leader} -> {m.new_leader}", "")
    table.add_row("detection", _fmt(m.detection_sec), "fault -> Patroni shows a new leader")
    table.add_row("RTO write", _fmt(m.write_outage.rto_write_sec), "fault -> first acked write")
    table.add_row("write gap", _fmt(m.write_outage.gap_sec), "last ack before -> first ack after")
    table.add_row(
        "read-only window",
        _fmt(m.write_outage.readonly_window_sec),
        f"{m.write_outage.readonly_failures} writes hit the new primary before it was writable",
    )
    table.add_row("RTO read", _fmt(m.rto_read_sec), "longest gap between good reads")
    lost = m.rpo.lost_acked_commits
    table.add_row(
        "lost acked commits",
        f"[red]{lost}[/red]" if lost else "0",
        f"of {m.ledger.acked} acked" + (f": ids {m.rpo.lost_acked[:10]}" if lost else ""),
    )
    table.add_row(
        "unknown commits",
        str(m.ledger.unknown),
        f"{len(m.rpo.unknown_present)} present, {len(m.rpo.unknown_missing)} missing",
    )
    sb = m.split_brain
    table.add_row(
        "split brain",
        "[red]YES[/red]" if sb.detected else "no",
        f"{len(sb.rounds)} rounds with 2 writable primaries, "
        f"{len(sb.multi_primary_rounds)} with 2 primaries but 1 writable",
    )
    table.add_row("rejoin", _fmt(m.rejoin_sec), "node start -> streaming with lag 0")
    table.add_row(
        "timeline",
        f"{m.timeline_before} -> {m.timeline_after}",
        f"pg_rewind ran: {r.rewind.get('ran')}, diverged at {r.rewind.get('diverged_at_lsn')}",
    )
    table.add_row(
        "ledger",
        f"{m.ledger.attempts}",
        f"{m.ledger.acked} acked, {m.ledger.failed} failed, {m.ledger.unknown} unknown",
    )
    table.add_row(
        "final state",
        "[green]healthy[/green]" if r.final.healthy else "[red]unhealthy[/red]",
        f"leader {r.final.leader}, replicas {r.final.replicas}, rows {r.final.row_counts}",
    )
    return table


@app.command()
def run(
    scenario: Annotated[str, typer.Argument(help="Scenario name. Phase 2: primary-sigkill.")],
    profile: Annotated[PatroniProfile, typer.Option(help="Patroni timing profile.")] = (
        PatroniProfile.DEFAULT
    ),
    sync: Annotated[SyncMode, typer.Option(help="Synchronous replication mode.")] = SyncMode.OFF,
    rate: Annotated[float, typer.Option(help="Writes per second.")] = 50.0,
    warmup: Annotated[
        float, typer.Option(help="Seconds of steady writes before the fault.")
    ] = 15.0,
    post_recovery: Annotated[
        float, typer.Option(help="Seconds of writes after recovery before restarting the node.")
    ] = 10.0,
    reports_dir: Annotated[
        Path | None, typer.Option(help="Where to write reports (default: <repo>/reports).")
    ] = None,
    ensure_up: Annotated[
        bool, typer.Option(help="Run `up` first so the cluster matches --profile/--sync.")
    ] = True,
) -> None:
    """Run one scenario against the cluster and write its report."""
    if scenario not in SCENARIOS:
        _fail(f"unknown scenario {scenario!r}; available: {', '.join(sorted(SCENARIOS))}")
    runner = ScenarioRunner(
        ClusterConfig(profile=profile, sync=sync),
        WorkloadSpec(rate_per_sec=rate, warmup_sec=warmup, post_recovery_sec=post_recovery),
        reports_root=reports_dir,
        log=_log,
        ensure_up=ensure_up,
    )
    try:
        result = runner.run_primary_sigkill()
    except (ScenarioError, FileNotFoundError, WaitTimeoutError) as exc:
        _fail(str(exc))
    except subprocess.CalledProcessError as exc:
        _fail(f"docker failed with exit code {exc.returncode}: {exc.stderr}")
    else:
        report_dir = write_run(result)
        console.print(_result_table(result))
        console.print(f"report: {report_dir}")
        if not result.ok:
            raise typer.Exit(1)


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
