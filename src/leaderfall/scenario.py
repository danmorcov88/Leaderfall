"""Scenario runner. Phase 2 hard-codes ``primary-sigkill``; the YAML engine comes in Phase 3.

A run has a fixed shape:

1. make sure the cluster is up with the requested profile and start from empty tables
2. start the poller, the writer and the reader, and let them warm up
3. inject the fault and wait, on real conditions, for the cluster to recover
4. bring the failed node back and wait for it to rejoin
5. stop the workload, wait for the cluster to be healthy and in sync, compute the metrics
"""

from __future__ import annotations

import os
import platform
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from pydantic import BaseModel, Field

from leaderfall import __version__, faults, measure
from leaderfall.cluster import (
    ETCD_CONTAINERS,
    HAPROXY_CONTAINER,
    HAPROXY_WRITE_PORT,
    NODES,
    NODES_BY_NAME,
    Cluster,
    ClusterConfig,
    ClusterUnreachableError,
    PatroniClient,
    WaitTimeoutError,
    conninfo,
    find_repo_root,
    wait_until,
)
from leaderfall.workload import (
    Clock,
    Ledger,
    NodePoller,
    Reader,
    Writer,
    ledger_ids_present,
    ledger_row_count,
    prepare_schema,
)


class WorkloadSpec(BaseModel):
    writers: int = 1  # only 1 is supported in this phase
    rate_per_sec: float = 50.0
    warmup_sec: float = 15.0
    post_recovery_sec: float = 10.0  # keep writing this long after the first ack after the fault
    reader_rate_per_sec: float = 10.0
    poll_rate_per_sec: float = 2.0


class Timeouts(BaseModel):
    new_leader_sec: float = 90.0
    first_write_sec: float = 120.0
    rejoin_sec: float = 180.0
    final_healthy_sec: float = 120.0
    rows_in_sync_sec: float = 60.0


class Event(BaseModel):
    t: float
    name: str
    detail: str | None = None


class RunResult(BaseModel):
    scenario: str
    description: str
    leaderfall_version: str = __version__
    started_at: str  # wall clock, ISO 8601 UTC, for humans
    finished_at: str
    duration_sec: float
    host: dict[str, Any]
    versions: dict[str, Any]
    cluster: dict[str, Any]
    workload: WorkloadSpec
    fault: faults.FaultTiming | None
    events: list[Event]
    metrics: measure.Metrics
    final: measure.FinalTopology
    rewind: dict[str, Any] = Field(default_factory=dict)
    report_dir: str

    @property
    def ok(self) -> bool:
        return self.final.healthy and not self.metrics.split_brain.detected


class ScenarioError(RuntimeError):
    pass


class ScenarioRunner:
    def __init__(
        self,
        config: ClusterConfig,
        workload: WorkloadSpec | None = None,
        timeouts: Timeouts | None = None,
        reports_root: Path | None = None,
        log: Callable[[str], None] = lambda _: None,
        ensure_up: bool = True,
    ) -> None:
        self.config = config
        self.workload = workload or WorkloadSpec()
        self.timeouts = timeouts or Timeouts()
        self.reports_root = reports_root or find_repo_root() / "reports"
        self.log = log
        self.ensure_up = ensure_up
        self.patroni = PatroniClient()
        self.cluster = Cluster(patroni=self.patroni, log=log)
        self.clock = Clock()
        self.events: list[Event] = []

    # ---- helpers ------------------------------------------------------------------

    def event(self, name: str, detail: str | None = None) -> Event:
        e = Event(t=self.clock.now(), name=name, detail=detail)
        self.events.append(e)
        self.log(f"[{e.t:7.2f}s] {name}" + (f" - {detail}" if detail else ""))
        return e

    def _wait(
        self, check: Callable[[], tuple[bool, Any, str | None]], what: str, timeout: float
    ) -> tuple[Any, float]:
        try:
            return wait_until(check, what=what, timeout=timeout, interval=0.2)
        except WaitTimeoutError as exc:
            self.event("timeout", str(exc))
            raise ScenarioError(str(exc)) from exc

    def _new_report_dir(self, scenario: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.reports_root / f"{stamp}-{scenario}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    # ---- the scenario --------------------------------------------------------------

    def run_primary_sigkill(self) -> RunResult:
        scenario = "primary-sigkill"
        description = "Kill the primary process with SIGKILL during steady writes."
        report_dir = self._new_report_dir(scenario)
        started_wall = datetime.now(UTC)
        self.event("run_start", f"report dir {report_dir}")

        if self.ensure_up:
            up = self.cluster.up(self.config, build=False)
            self.event(
                "cluster_ready", f"leader {up.state.leader.name if up.state.leader else '?'}"
            )
        state, _ = self.cluster.wait_healthy(
            self.timeouts.final_healthy_sec, sync_standbys=self.config.expected_sync_standbys
        )
        if state.leader is None:
            raise ScenarioError("no leader before the fault")
        old_leader = state.leader.name
        old_node = NODES_BY_NAME[old_leader]

        prepare_schema()
        self.event("schema_ready", "ledger tables created and emptied")

        ledger = Ledger(report_dir / "ledger.jsonl")
        poller = NodePoller(self.clock, self.patroni, rate_per_sec=self.workload.poll_rate_per_sec)
        writer = Writer(self.clock, ledger, rate_per_sec=self.workload.rate_per_sec)
        reader = Reader(self.clock, rate_per_sec=self.workload.reader_rate_per_sec)
        threads = (poller, writer, reader)
        for t in threads:
            t.start()
        self.event(
            "workload_started", f"{self.workload.rate_per_sec:g} writes/s, old leader {old_leader}"
        )

        fault: faults.FaultTiming | None = None
        try:
            # Warm-up: the workload must be steady before the fault, and the ledger needs
            # acked writes before it so the outage window has a left edge.
            self._wait(
                lambda: (
                    self.clock.now() >= self.workload.warmup_sec
                    and any(e.result == "acked" for e in ledger.snapshot()),
                    None,
                    f"{len(ledger.snapshot())} attempts so far",
                ),
                "warm-up with acked writes",
                self.workload.warmup_sec + 30,
            )
            acked_before = sum(e.result == "acked" for e in ledger.snapshot())
            self.event("warmup_done", f"{acked_before} acked writes before the fault")

            # SIGKILL lands within tens of milliseconds of the request; the docker CLI then
            # takes a good half second to return. The request time is the fault time.
            fault = faults.kill(self.clock, old_node.container, "SIGKILL")
            fault_at = fault.requested_at
            self.event("fault_injected", f"SIGKILL {old_node.container}")

            (detection, new_leader), _ = self._wait(
                lambda: _detection_check(poller.snapshot(), fault_at, old_leader),
                "a new leader",
                self.timeouts.new_leader_sec,
            )
            self.event("new_leader", f"{new_leader} after {detection:.1f}s")

            outage, _ = self._wait(
                lambda: _first_write_check(ledger.snapshot(), fault_at),
                "the first acked write after the fault",
                self.timeouts.first_write_sec,
            )
            self.event("first_write_ok", f"RTO write {outage.rto_write_sec:.1f}s")

            # Keep the workload going so the new primary is exercised before the old one
            # comes back. This is part of the workload, not a wait for a condition.
            time.sleep(self.workload.post_recovery_sec)

            restart = faults.start(self.clock, old_node.container)
            self.event("node_started", old_node.container)
            rejoin, _ = self._wait(
                lambda: _rejoin_check(poller.snapshot(), restart.done_at, old_leader),
                f"{old_leader} to rejoin as a streaming replica",
                self.timeouts.rejoin_sec,
            )
            self.event("node_rejoined", f"{old_leader} streaming after {rejoin:.1f}s")
        finally:
            for t in threads:
                t.stop()
            ledger.close()
            self.event("workload_stopped")
            for t in threads:
                if t.exception is not None:
                    self.event("thread_error", f"{t.name}: {t.exception!r}")

        # ---- settle and measure ----
        final_state, _ = self._wait(
            lambda: _healthy_check(self.cluster, self.config.expected_sync_standbys),
            "a healthy cluster after the run",
            self.timeouts.final_healthy_sec,
        )
        counts, _ = self._wait(
            _row_counts_check, "row counts to match on all nodes", self.timeouts.rows_in_sync_sec
        )
        self.event("rows_in_sync", str(counts))

        assert fault is not None
        entries = ledger.snapshot()
        rounds = poller.snapshot()
        reads = reader.snapshot()
        leader_now = final_state.leader.name if final_state.leader else None
        leader_port = NODES_BY_NAME[leader_now].pg_port if leader_now else HAPROXY_WRITE_PORT
        candidate_ids = [e.id for e in entries if e.result in ("acked", "unknown")]
        present = ledger_ids_present(leader_port, candidate_ids)
        rpo = measure.rpo(entries, present)
        timelines = {n.name: node_timeline(n.pg_port) for n in NODES}

        metrics = measure.Metrics(
            old_leader=old_leader,
            new_leader=new_leader,
            detection_sec=detection,
            write_outage=measure.write_outage(entries, fault.requested_at),
            rto_read_sec=measure.rto_read(reads, run_end=self.clock.now()),
            rpo=rpo,
            split_brain=measure.split_brain(rounds),
            rejoin_sec=rejoin,
            diverged_rows_max=measure.diverged_rows_max(rpo),
            timeline_before=measure.timeline_at(rounds, fault.requested_at, old_leader),
            timeline_after=timelines.get(leader_now or ""),
            ledger=measure.summarize_ledger(entries),
        )
        final = measure.final_topology(
            leader=leader_now,
            replicas=[m.name for m in final_state.replicas],
            timelines=timelines,
            row_counts=counts,
            problems=final_state.health_problems(sync_standbys=self.config.expected_sync_standbys),
        )
        rewind = rewind_info(faults.container_logs(old_node.container))
        self.event("run_end")

        finished_wall = datetime.now(UTC)
        result = RunResult(
            scenario=scenario,
            description=description,
            started_at=started_wall.isoformat(timespec="seconds"),
            finished_at=finished_wall.isoformat(timespec="seconds"),
            duration_sec=self.clock.now(),
            host=host_info(),
            versions=versions_info(self.patroni),
            cluster={
                "profile": self.config.profile,
                "sync": self.config.sync,
                "dcs": self.patroni.config(),
            },
            workload=self.workload,
            fault=fault,
            events=self.events,
            metrics=metrics,
            final=final,
            rewind=rewind,
            report_dir=str(report_dir),
        )
        _dump_samples(report_dir, rounds, reads)
        return result


# --------------------------------------------------------------------------------------
# Wait conditions (each returns (done, value, detail))
# --------------------------------------------------------------------------------------


def _detection_check(
    rounds: list[Any], fault_at: float, old_leader: str
) -> tuple[bool, tuple[float | None, str | None], str | None]:
    detection, leader = measure.detection_time(rounds, fault_at, old_leader)
    last = rounds[-1].leader if rounds else None
    return detection is not None, (detection, leader), f"Patroni leader is {last}"


def _first_write_check(entries: list[Any], fault_at: float) -> tuple[bool, Any, str | None]:
    outage = measure.write_outage(entries, fault_at)
    return (
        outage.first_ack_after_fault is not None,
        outage,
        f"{outage.failed_during_outage} failed, {outage.unknown_during_outage} unknown so far",
    )


def _rejoin_check(
    rounds: list[Any], started_at: float, node: str
) -> tuple[bool, float | None, str | None]:
    rejoin = measure.rejoin_time(rounds, started_at, node)
    last = rounds[-1].members.get(node) if rounds else None
    return rejoin is not None, rejoin, f"{node} is {last}"


def _healthy_check(cluster: Cluster, sync_standbys: int) -> tuple[bool, Any, str | None]:
    try:
        state = cluster.status()
    except ClusterUnreachableError as exc:
        return False, None, str(exc)
    problems = state.health_problems(sync_standbys=sync_standbys)
    return not problems, state, "; ".join(problems) or None


def _row_counts_check() -> tuple[bool, dict[str, int | None], str | None]:
    counts = {n.name: ledger_row_count(n.pg_port) for n in NODES}
    distinct = {c for c in counts.values()}
    return len(distinct) == 1 and None not in distinct, counts, str(counts)


# --------------------------------------------------------------------------------------
# Environment facts recorded with every result
# --------------------------------------------------------------------------------------


def node_timeline(port: int) -> int | None:
    try:
        with psycopg.connect(conninfo(port, timeout=5)) as conn:
            row = conn.execute("select (pg_control_checkpoint()).timeline_id").fetchone()
    except psycopg.Error:
        return None
    return int(row[0]) if row else None


def host_info() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
        "docker": faults.docker_server_version(),
    }


def versions_info(patroni: PatroniClient) -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        with psycopg.connect(conninfo(HAPROXY_WRITE_PORT, timeout=5)) as conn:
            row = conn.execute("show server_version").fetchone()
        info["postgresql"] = row[0] if row else None
    except psycopg.Error:
        info["postgresql"] = None
    status = patroni.node_status(NODES[0]) or {}
    info["patroni"] = (status.get("patroni") or {}).get("version")
    info["etcd_image"] = faults.container_image(ETCD_CONTAINERS[0])
    info["haproxy_image"] = faults.container_image(HAPROXY_CONTAINER)
    info["postgres_image"] = faults.container_image(NODES[0].container)
    return info


_REWIND_DIVERGED = re.compile(r"servers diverged at WAL location (\S+) on timeline (\d+)")


def rewind_info(logs: str) -> dict[str, Any]:
    """What ``pg_rewind`` said in the restarted node's log, if it ran."""
    ran = "pg_rewind" in logs
    matches = list(_REWIND_DIVERGED.finditer(logs))
    match = matches[-1] if matches else None  # the most recent rewind
    return {
        "ran": ran,
        "done": ran and "pg_rewind: Done!" in logs,
        "diverged_at_lsn": match.group(1) if match else None,
        "diverged_on_timeline": int(match.group(2)) if match else None,
    }


def _dump_samples(report_dir: Path, rounds: list[Any], reads: list[Any]) -> None:
    with (report_dir / "rounds.jsonl").open("w", encoding="utf-8") as f:
        for r in rounds:
            f.write(r.model_dump_json() + "\n")
    with (report_dir / "reads.jsonl").open("w", encoding="utf-8") as f:
        for r in reads:
            f.write(r.model_dump_json() + "\n")
