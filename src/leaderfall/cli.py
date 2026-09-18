"""Command-line entry point.

Commands are added phase by phase:

- Phase 1: ``up``, ``status``, ``down``
- Phase 2/3: ``run``
- Phase 5: ``report``
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
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
    find_repo_root,
)
from leaderfall.report import SuiteResult, SuiteRow, write_run, write_suite
from leaderfall.scenario import (
    RunResult,
    ScenarioRunner,
    ScenarioSpec,
    find_scenario,
    load_all,
    scenarios_dir,
)
from leaderfall.slo import SloConfig

app = typer.Typer(
    name="leaderfall",
    help="Break a PostgreSQL HA cluster on purpose, and prove with numbers that it survives.",
    no_args_is_help=True,
    add_completion=False,
)
# Rich falls back to 80 columns when stdout is not a terminal (CI logs, pipes), which
# squashes the result tables. Give it room unless the environment says otherwise.
_WIDTH = None if sys.stdout.isatty() else int(os.environ.get("COLUMNS", "132"))
console = Console(width=_WIDTH)
err = Console(stderr=True, width=_WIDTH)


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


def _pf(passed: bool) -> str:
    return "[green]pass[/green]" if passed else "[red]FAIL[/red]"


def _result_table(r: RunResult) -> Table:
    m = r.metrics
    table = Table(title=f"{r.scenario}: {r.description}", title_justify="left")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_column("note")
    if m.new_leader:
        table.add_row("failover", f"{m.old_leader} -> {m.new_leader}", "")
    else:
        table.add_row("failover", "none", f"leader stayed {m.old_leader}")
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
    if m.demotion_expected:
        last = _fmt(m.old_primary_last_write_sec)
        table.add_row(
            "demotion",
            _fmt(m.demotion_sec),
            f"fault -> {m.failed_node} stops taking writes (last accepted at {last})",
        )
    if m.failed_node:
        table.add_row(
            "rejoin",
            _fmt(m.rejoin_sec),
            f"{m.recovery_action or 'no action'} -> streaming with lag 0",
        )
    rewind_note = (
        f"pg_rewind ran: {r.rewind.get('ran')}, diverged at {r.rewind.get('diverged_at_lsn')}"
        if r.rewind
        else ""
    )
    table.add_row("timeline", f"{m.timeline_before} -> {m.timeline_after}", rewind_note)
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


def _checks_table(r: RunResult) -> Table:
    table = Table(title="SLO checks", title_justify="left")
    table.add_column("check")
    table.add_column("limit", justify="right")
    table.add_column("actual", justify="right")
    table.add_column("result")
    table.add_column("note")
    for c in r.checks:
        table.add_row(c.name, str(c.limit), str(c.actual), _pf(c.passed), c.note or "")
    return table


SUITE_COLUMNS = (
    "scenario", "result", "detect", "RTO w", "ro win", "RTO r",
    "lost", "unk", "split", "demote", "rejoin", "failed checks",
)  # fmt: skip


def _suite_table(suite: SuiteResult) -> Table:
    table = Table(title=f"suite '{suite.tag}'", title_justify="left")
    for col in SUITE_COLUMNS:
        left = col in {"scenario", "failed checks"}
        table.add_column(col, justify="left" if left else "right")
    for row in suite.rows:
        table.add_row(
            row.scenario,
            _pf(row.passed),
            _fmt(row.detection_sec),
            _fmt(row.rto_write_sec),
            _fmt(row.readonly_window_sec),
            _fmt(row.rto_read_sec),
            str(row.lost_acked_commits),
            str(row.unknown_commits),
            "YES" if row.split_brain else "no",
            _fmt(row.demotion_sec),
            _fmt(row.rejoin_sec),
            ", ".join(row.failed_checks) + (f" ({row.error})" if row.error else ""),
        )
    return table


def _run_one(
    spec: ScenarioSpec, slo_config: SloConfig, reports_dir: Path | None, ensure_up: bool
) -> RunResult:
    runner = ScenarioRunner(
        spec, slo_config=slo_config, reports_root=reports_dir, log=_log, ensure_up=ensure_up
    )
    result = runner.run()
    write_run(result)
    return result


@app.command("list")
def list_scenarios() -> None:
    """List the scenarios in scenarios/."""
    table = Table(title=str(scenarios_dir()), title_justify="left")
    table.add_column("name")
    table.add_column("tags")
    table.add_column("profile")
    table.add_column("sync")
    table.add_column("description")
    for spec in load_all():
        table.add_row(
            spec.name,
            ", ".join(spec.tags),
            spec.cluster.patroni_profile,
            spec.cluster.synchronous_mode,
            spec.description,
        )
    console.print(table)


@app.command()
def run(
    scenario: Annotated[
        str | None, typer.Argument(help="Scenario name (a file in scenarios/).")
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="Run every scenario with the tag.")] = False,
    tag: Annotated[str, typer.Option(help="With --all: which tag to run (or 'all').")] = "core",
    reports_dir: Annotated[
        Path | None, typer.Option(help="Where to write reports (default: <repo>/reports).")
    ] = None,
    slo_file: Annotated[
        Path | None, typer.Option(help="SLO limits file (default: <repo>/slo.yml).")
    ] = None,
    ensure_up: Annotated[
        bool, typer.Option(help="Run `up` first so the cluster matches the scenario.")
    ] = True,
) -> None:
    """Run one scenario, or a whole suite with --all, and write the reports."""
    if (scenario is None) == (not all_):
        _fail("give a scenario name, or --all")
    try:
        slo_config = SloConfig.load(slo_file or find_repo_root() / "slo.yml")
        specs = load_all(tag=tag) if all_ else [find_scenario(scenario or "")]
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return
    if not specs:
        _fail(f"no scenarios with tag {tag!r}")

    results: list[RunResult] = []
    started = datetime.now(UTC).isoformat(timespec="seconds")
    for spec in specs:
        console.rule(spec.name)
        try:
            result = _run_one(spec, slo_config, reports_dir, ensure_up)
        except (FileNotFoundError, WaitTimeoutError) as exc:
            _fail(str(exc))
            return
        except subprocess.CalledProcessError as exc:
            _fail(f"docker compose failed with exit code {exc.returncode}")
            return
        results.append(result)
        console.print(_result_table(result))
        console.print(_checks_table(result))
        if result.error:
            console.print(f"[red]run aborted:[/red] {result.error}")
        console.print(f"{_pf(result.passed)}  report: {result.report_dir}")

    if all_:
        suite = SuiteResult(
            started_at=started,
            finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
            tag=tag,
            rows=[SuiteRow.from_result(r) for r in results],
            passed=all(r.passed for r in results),
            host=results[0].host,
            versions=results[0].versions,
        )
        path = write_suite(suite, reports_dir or find_repo_root() / "reports")
        console.rule("suite")
        console.print(_suite_table(suite))
        console.print(f"{_pf(suite.passed)}  suite report: {path}")
    if not all(r.passed for r in results):
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
