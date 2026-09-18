"""Scenario YAML model and runner.

A scenario is a YAML file (see ``scenarios/``). The runner:

1. makes sure the cluster is up with the requested profile and starts from empty tables
2. starts the poller, the writer and the reader, and lets them warm up
3. executes the steps in order: faults, actions, waits on real conditions, holds
4. stops the workload, waits for the cluster to be healthy and in sync, computes the
   metrics and checks them against the SLO limits

If a step fails, the runner does its best to bring the failed node back before it gives
up, so the next scenario starts from a healthy cluster.
"""

from __future__ import annotations

import os
import platform
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psycopg
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from leaderfall import __version__, faults, measure, slo
from leaderfall.cluster import (
    ETCD_CONTAINERS,
    HAPROXY_CONTAINER,
    HAPROXY_WRITE_PORT,
    NODES,
    NODES_BY_NAME,
    Cluster,
    ClusterConfig,
    ClusterState,
    ClusterUnreachableError,
    PatroniClient,
    PatroniProfile,
    SyncMode,
    WaitTimeoutError,
    conninfo,
    find_repo_root,
    wait_until,
)
from leaderfall.faults import FaultError, FaultTiming, Target, TargetKind, resolve_target
from leaderfall.workload import (
    Clock,
    Ledger,
    NodePoller,
    PollRound,
    Reader,
    Writer,
    ledger_ids_present,
    ledger_row_count,
    prepare_schema,
)

# --------------------------------------------------------------------------------------
# YAML model
# --------------------------------------------------------------------------------------

FAULTS = frozenset({"kill", "stop", "pause", "partition", "switchover", "restart_service"})
ACTIONS = frozenset({"start", "unpause", "heal", "kill", "stop", "pause", "partition"})
WAITS = frozenset({"new_leader", "writes_ok", "cluster_healthy", "node_streaming", "node_fenced"})
KEEPS_NODE_UP = frozenset({"partition", "pause"})  # faults after which the node still runs


class ClusterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patroni_profile: PatroniProfile = PatroniProfile.DEFAULT
    synchronous_mode: SyncMode = SyncMode.OFF

    @field_validator("synchronous_mode", mode="before")
    @classmethod
    def _sync_from_bool(cls, value: object) -> object:
        if value is True:
            return SyncMode.ON
        if value is False:
            return SyncMode.OFF
        return value

    def config(self) -> ClusterConfig:
        return ClusterConfig(profile=self.patroni_profile, sync=self.synchronous_mode)


class WorkloadSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    writers: int = Field(default=1, ge=1, le=1)  # one writer for now
    rate_per_sec: float = Field(default=50.0, gt=0)
    warmup_sec: float = Field(default=15.0, ge=1)
    reader_rate_per_sec: float = Field(default=10.0, gt=0)
    poll_rate_per_sec: float = Field(default=2.0, gt=0)


class Step(BaseModel):
    """Exactly one of ``fault``, ``action``, ``wait_for`` or ``hold_sec``."""

    model_config = ConfigDict(extra="forbid")

    fault: str | None = None
    action: str | None = None
    wait_for: str | None = None
    hold_sec: float | None = Field(default=None, gt=0)

    target: str | None = None
    signal: str = "SIGKILL"
    candidate: str | None = None  # switchover only
    grace_sec: int | None = None  # stop / restart_service only
    timeout_sec: float = Field(default=120.0, gt=0)
    max_lag_bytes: int = 0  # node_streaming only

    @model_validator(mode="after")
    def _one_kind(self) -> Step:
        kinds = [
            k for k in ("fault", "action", "wait_for", "hold_sec") if getattr(self, k) is not None
        ]
        if len(kinds) != 1:
            raise ValueError(
                f"a step needs exactly one of fault/action/wait_for/hold_sec, got {kinds}"
            )
        if self.fault is not None and self.fault not in FAULTS:
            raise ValueError(f"unknown fault {self.fault!r}; known: {sorted(FAULTS)}")
        if self.action is not None and self.action not in ACTIONS:
            raise ValueError(f"unknown action {self.action!r}; known: {sorted(ACTIONS)}")
        if self.wait_for is not None and self.wait_for not in WAITS:
            raise ValueError(f"unknown wait_for {self.wait_for!r}; known: {sorted(WAITS)}")
        if (self.fault or self.action) and self.target is None:
            raise ValueError(f"{self.fault or self.action} needs a target")
        return self

    @property
    def kind(self) -> Literal["fault", "action", "wait_for", "hold"]:
        if self.fault is not None:
            return "fault"
        if self.action is not None:
            return "action"
        if self.wait_for is not None:
            return "wait_for"
        return "hold"

    @property
    def label(self) -> str:
        return str(self.fault or self.action or self.wait_for or f"hold {self.hold_sec:g}s")


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str
    tags: list[str] = []
    cluster: ClusterSpec = ClusterSpec()
    workload: WorkloadSpec = WorkloadSpec()
    steps: list[Step] = Field(min_length=1)
    expect: slo.Limits = slo.Limits()

    @staticmethod
    def load(path: Path) -> ScenarioSpec:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a mapping at the top level")
        spec = ScenarioSpec.model_validate(data)
        if spec.name != path.stem:
            raise ValueError(f"{path}: name {spec.name!r} does not match the file name")
        return spec


def scenarios_dir() -> Path:
    return find_repo_root() / "scenarios"


def load_all(directory: Path | None = None, tag: str | None = None) -> list[ScenarioSpec]:
    specs = [ScenarioSpec.load(p) for p in sorted((directory or scenarios_dir()).glob("*.yml"))]
    if tag and tag != "all":
        specs = [s for s in specs if tag in s.tags]
    return specs


def find_scenario(name: str, directory: Path | None = None) -> ScenarioSpec:
    base = directory or scenarios_dir()
    path = base / f"{name}.yml"
    if not path.is_file():
        available = ", ".join(p.stem for p in sorted(base.glob("*.yml")))
        raise FileNotFoundError(f"no scenario {name!r}; available: {available}")
    return ScenarioSpec.load(path)


# --------------------------------------------------------------------------------------
# Result model
# --------------------------------------------------------------------------------------


class Timeouts(BaseModel):
    warmup_extra_sec: float = 30.0
    final_healthy_sec: float = 180.0
    rows_in_sync_sec: float = 60.0


class Event(BaseModel):
    t: float
    name: str
    detail: str | None = None


class RunResult(BaseModel):
    scenario: str
    description: str
    tags: list[str]
    leaderfall_version: str = __version__
    started_at: str  # wall clock, ISO 8601 UTC, for humans
    finished_at: str
    duration_sec: float
    host: dict[str, Any]
    versions: dict[str, Any]
    cluster: dict[str, Any]
    workload: WorkloadSpec
    faults: list[FaultTiming]
    events: list[Event]
    metrics: measure.Metrics
    final: measure.FinalTopology
    rewind: dict[str, Any] = Field(default_factory=dict)
    limits: slo.Limits
    checks: list[slo.Check]
    passed: bool
    error: str | None = None  # set when the run aborted; metrics are then partial
    report_dir: str


class ScenarioError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------


class ScenarioRunner:
    def __init__(
        self,
        spec: ScenarioSpec,
        *,
        slo_config: slo.SloConfig,
        reports_root: Path | None = None,
        timeouts: Timeouts | None = None,
        log: Callable[[str], None] = lambda _: None,
        ensure_up: bool = True,
    ) -> None:
        self.spec = spec
        self.config = spec.cluster.config()
        self.slo_config = slo_config
        self.timeouts = timeouts or Timeouts()
        self.reports_root = reports_root or find_repo_root() / "reports"
        self.log = log
        self.ensure_up = ensure_up
        self.patroni = PatroniClient()
        self.cluster = Cluster(patroni=self.patroni, log=log)
        self.clock = Clock()
        self.events: list[Event] = []
        self.fault_log: list[FaultTiming] = []
        # Set by the steps:
        self.first_fault: FaultTiming | None = None
        self.failed: Target | None = None
        self.old_leader: str | None = None
        self.recovery: FaultTiming | None = None

    # ---- helpers ------------------------------------------------------------------

    def event(self, name: str, detail: str | None = None) -> None:
        e = Event(t=self.clock.now(), name=name, detail=detail)
        self.events.append(e)
        self.log(f"[{e.t:7.2f}s] {name}" + (f" - {detail}" if detail else ""))

    def _wait(
        self, check: Callable[[], tuple[bool, Any, str | None]], what: str, timeout: float
    ) -> tuple[Any, float]:
        try:
            return wait_until(check, what=what, timeout=timeout, interval=0.2)
        except WaitTimeoutError as exc:
            raise ScenarioError(str(exc)) from exc

    def _state(self) -> ClusterState:
        try:
            return self.patroni.cluster()
        except ClusterUnreachableError as exc:
            raise ScenarioError(f"cannot resolve target: {exc}") from exc

    def _new_report_dir(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.reports_root / f"{stamp}-{self.spec.name}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    @property
    def fault_at(self) -> float | None:
        return self.first_fault.requested_at if self.first_fault else None

    # ---- steps ---------------------------------------------------------------------

    def _do_fault_or_action(self, step: Step) -> None:
        name = step.fault or step.action
        assert name is not None
        assert step.target is not None
        state = self._state()
        target = resolve_target(step.target, state, self.failed)
        is_first_fault = step.fault is not None and self.first_fault is None
        if is_first_fault:
            self.old_leader = state.leader.name if state.leader else None

        if name == "kill":
            timing = faults.kill(self.clock, target, step.signal)
        elif name == "stop":
            timing = faults.stop(self.clock, target, step.grace_sec or 60)
        elif name == "start":
            timing = faults.start(self.clock, target)
        elif name == "pause":
            timing = faults.pause(self.clock, target)
        elif name == "unpause":
            timing = faults.unpause(self.clock, target)
        elif name == "partition":
            timing = faults.partition(self.clock, target)
        elif name == "heal":
            timing = faults.heal(self.clock, target)
        elif name == "restart_service":
            timing = faults.restart(self.clock, target, step.grace_sec or 10)
        elif name == "switchover":
            timing = faults.switchover(self.clock, self.patroni, target, step.candidate)
        else:  # pragma: no cover - guarded by the model validator
            raise ScenarioError(f"unknown step {name!r}")

        self.fault_log.append(timing)
        if is_first_fault:
            self.first_fault, self.failed = timing, target
        if (
            step.action in {"start", "unpause", "heal"}
            and self.failed is not None
            and target == self.failed
            and self.recovery is None
        ):
            self.recovery = timing
        took = timing.done_at - timing.requested_at
        self.event(step.kind, f"{timing.action} {target.label} ({took:.2f}s)")

    def _do_wait(self, step: Step, poller: NodePoller, ledger: Ledger) -> None:
        what = step.wait_for
        assert what is not None
        target: Target | None = None
        if what in {"node_streaming", "node_fenced"}:
            target = resolve_target(step.target or "failed_node", self._state(), self.failed)

        def check() -> tuple[bool, Any, str | None]:
            rounds = poller.snapshot()
            last: PollRound | None = rounds[-1] if rounds else None
            if what == "new_leader":
                if self.fault_at is None:
                    raise ScenarioError("wait_for new_leader before any fault")
                detection, leader = measure.detection_time(rounds, self.fault_at, self.old_leader)
                seen = last.leader if last else "?"
                return detection is not None, (detection, leader), f"leader is {seen}"
            if what == "writes_ok":
                since = self.fault_at if self.fault_at is not None else 0.0
                entries = ledger.snapshot()
                tail = entries[-1] if entries else None
                detail = f"last write: {tail.result} {tail.error or ''}" if tail else "no writes"
                return measure.writes_ok(entries, since), None, detail
            if what == "cluster_healthy":
                try:
                    state = self.patroni.cluster()
                except ClusterUnreachableError as exc:
                    return False, None, str(exc)
                problems = state.health_problems(sync_standbys=self.config.expected_sync_standbys)
                return not problems, state, "; ".join(problems) or None
            assert target is not None
            assert target.node is not None
            if what == "node_streaming":
                m = last.members.get(target.node) if last else None
                lag = m.get("lag") if m else None
                ok = (
                    m is not None
                    and m.get("state") == "streaming"
                    and isinstance(lag, int)
                    and lag <= step.max_lag_bytes
                )
                return ok, None, f"{target.node} is {m}"
            # node_fenced: the node has stopped accepting probe writes
            sample = next((n for n in last.nodes if n.node == target.node), None) if last else None
            return measure.node_fenced(rounds, target.node), None, f"{sample}"

        what_label = what + (f" ({target.label})" if target else "")
        value, waited = self._wait(check, what_label, step.timeout_sec)
        detail = f"after {waited:.1f}s"
        if what == "new_leader":
            detection, leader = value
            detail = f"{leader} after {detection:.1f}s"
        self.event(f"wait:{what}", detail)

    def _do_hold(self, step: Step) -> None:
        assert step.hold_sec is not None
        # Part of the workload definition, not a wait for a condition: keep writing for
        # this long (for example to exercise the new primary before the old one returns).
        time.sleep(step.hold_sec)
        self.event("hold", f"{step.hold_sec:g}s of steady workload")

    # ---- best-effort recovery when a step fails --------------------------------------

    def _recover(self) -> None:
        if self.failed is None:
            return
        status = faults.container_status(self.failed.container)
        actions = {f.action for f in self.fault_log}
        try:
            if status == "paused":
                self.fault_log.append(faults.unpause(self.clock, self.failed))
                self.event("recover", f"unpaused {self.failed.label}")
            elif status in {"exited", "created", "dead"}:
                self.fault_log.append(faults.start(self.clock, self.failed))
                self.event("recover", f"started {self.failed.label}")
            elif status == "running" and "partition" in actions and "heal" not in actions:
                self.fault_log.append(faults.heal(self.clock, self.failed))
                self.event("recover", f"reconnected {self.failed.label}")
        except FaultError as exc:
            self.event("recover_failed", str(exc))

    # ---- the run ---------------------------------------------------------------------

    def run(self) -> RunResult:
        spec = self.spec
        report_dir = self._new_report_dir()
        started_wall = datetime.now(UTC)
        self.event("run_start", f"{spec.name}: {report_dir}")

        if self.ensure_up:
            self.cluster.up(self.config, build=False)
        state, _ = self.cluster.wait_healthy(
            self.timeouts.final_healthy_sec, sync_standbys=self.config.expected_sync_standbys
        )
        self.event("cluster_ready", f"leader {state.leader.name if state.leader else '?'}")

        prepare_schema()
        ledger = Ledger(report_dir / "ledger.jsonl")
        poller = NodePoller(self.clock, self.patroni, rate_per_sec=spec.workload.poll_rate_per_sec)
        writer = Writer(self.clock, ledger, rate_per_sec=spec.workload.rate_per_sec)
        reader = Reader(self.clock, rate_per_sec=spec.workload.reader_rate_per_sec)
        threads = (poller, writer, reader)
        for t in threads:
            t.start()
        self.event("workload_started", f"{spec.workload.rate_per_sec:g} writes/s")

        error: str | None = None
        try:
            self._wait(
                lambda: (
                    self.clock.now() >= spec.workload.warmup_sec
                    and any(e.result == "acked" for e in ledger.snapshot()),
                    None,
                    f"{len(ledger.snapshot())} attempts so far",
                ),
                "warm-up with acked writes",
                spec.workload.warmup_sec + self.timeouts.warmup_extra_sec,
            )
            acked = sum(e.result == "acked" for e in ledger.snapshot())
            self.event("warmup_done", f"{acked} acked writes")

            for i, step in enumerate(spec.steps, 1):
                self.log(f"step {i}/{len(spec.steps)}: {step.kind} {step.label}")
                if step.kind in {"fault", "action"}:
                    self._do_fault_or_action(step)
                elif step.kind == "wait_for":
                    self._do_wait(step, poller, ledger)
                else:
                    self._do_hold(step)
        except (ScenarioError, FaultError, ClusterUnreachableError) as exc:
            error = str(exc)
            self.event("step_failed", error)
            self._recover()
        finally:
            for t in threads:
                t.stop()
            ledger.close()
            self.event("workload_stopped")
            for t in threads:
                if t.exception is not None:
                    self.event("thread_error", f"{t.name}: {t.exception!r}")

        # ---- settle ----
        final_state: ClusterState | None = None
        final_problems = ["cluster never became healthy after the run"]
        counts: dict[str, int | None]
        try:
            final_state, _ = self._wait(
                lambda: _healthy_check(self.cluster, self.config.expected_sync_standbys),
                "a healthy cluster after the run",
                self.timeouts.final_healthy_sec,
            )
            final_problems = []
            counts, _ = self._wait(
                _row_counts_check,
                "row counts to match on all nodes",
                self.timeouts.rows_in_sync_sec,
            )
            self.event("rows_in_sync", str(counts))
        except ScenarioError as exc:
            self.event("settle_failed", str(exc))
            error = error or str(exc)
            counts = {n.name: ledger_row_count(n.pg_port) for n in NODES}

        # ---- measure ----
        entries = ledger.snapshot()
        rounds = poller.snapshot()
        reads = reader.snapshot()
        now = self.clock.now()
        leader_now = final_state.leader.name if final_state and final_state.leader else None
        leader_port = NODES_BY_NAME[leader_now].pg_port if leader_now else HAPROXY_WRITE_PORT
        candidate_ids = [e.id for e in entries if e.result in ("acked", "unknown")]
        try:
            present = ledger_ids_present(leader_port, candidate_ids)
        except psycopg.Error as exc:
            self.event("rpo_check_failed", str(exc))
            present = set()
        rpo = measure.rpo(entries, present)
        timelines = {n.name: node_timeline(n.pg_port) for n in NODES}

        fault_at = self.fault_at
        failed_node = (
            self.failed.node if self.failed and self.failed.kind is TargetKind.PG else None
        )
        detection, new_leader = (None, None)
        demotion = last_write = timeline_before = None
        # Demotion is only a thing when the old primary stayed up but lost its lease.
        demotion_expected = (
            self.first_fault is not None
            and self.first_fault.action in KEEPS_NODE_UP
            and failed_node is not None
            and failed_node == self.old_leader
        )
        if fault_at is not None:
            detection, new_leader = measure.detection_time(rounds, fault_at, self.old_leader)
            if demotion_expected:
                until = self.recovery.requested_at if self.recovery else None
                demotion, last_write = measure.demotion_time(rounds, fault_at, failed_node, until)
            timeline_before = measure.timeline_at(rounds, fault_at, self.old_leader)
        rejoin = (
            measure.rejoin_time(rounds, self.recovery.done_at, failed_node)
            if self.recovery and failed_node
            else None
        )
        metrics = measure.Metrics(
            failed_node=failed_node,
            recovery_action=self.recovery.action if self.recovery else None,
            old_leader=self.old_leader,
            new_leader=new_leader,
            detection_sec=detection,
            demotion_expected=demotion_expected,
            demotion_sec=demotion,
            old_primary_last_write_sec=last_write,
            write_outage=measure.write_outage(entries, fault_at if fault_at is not None else now),
            rto_read_sec=measure.rto_read(reads, run_end=now),
            rpo=rpo,
            split_brain=measure.split_brain(rounds),
            rejoin_sec=rejoin,
            diverged_rows_max=measure.diverged_rows_max(rpo),
            timeline_before=timeline_before,
            timeline_after=timelines.get(leader_now or ""),
            ledger=measure.summarize_ledger(entries),
        )
        final = measure.final_topology(
            leader=leader_now,
            replicas=[m.name for m in final_state.replicas] if final_state else [],
            timelines=timelines,
            row_counts=counts,
            problems=(
                final_state.health_problems(sync_standbys=self.config.expected_sync_standbys)
                if final_state
                else final_problems
            ),
        )
        rewind = (
            rewind_info(faults.container_logs(self.failed.container, since=started_wall))
            if self.failed is not None and failed_node
            else {}
        )
        limits = self.slo_config.limits_for(self.config.profile, self.config.sync, spec.expect)
        checks = slo.evaluate(metrics, final, limits)
        passed = slo.passed(checks) and error is None
        self.event("run_end", "passed" if passed else "FAILED")
        _dump_samples(report_dir, rounds, reads)

        return RunResult(
            scenario=spec.name,
            description=spec.description,
            tags=spec.tags,
            started_at=started_wall.isoformat(timespec="seconds"),
            finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
            duration_sec=self.clock.now(),
            host=host_info(),
            versions=versions_info(self.patroni),
            cluster={
                "profile": self.config.profile,
                "sync": self.config.sync,
                "dcs": _safe_config(self.patroni),
            },
            workload=spec.workload,
            faults=self.fault_log,
            events=self.events,
            metrics=metrics,
            final=final,
            rewind=rewind,
            limits=limits,
            checks=checks,
            passed=passed,
            error=error,
            report_dir=str(report_dir),
        )


# --------------------------------------------------------------------------------------
# Wait conditions and environment facts
# --------------------------------------------------------------------------------------


def _healthy_check(cluster: Cluster, sync_standbys: int) -> tuple[bool, Any, str | None]:
    try:
        state = cluster.status()
    except ClusterUnreachableError as exc:
        return False, None, str(exc)
    problems = state.health_problems(sync_standbys=sync_standbys)
    return not problems, state, "; ".join(problems) or None


def _row_counts_check() -> tuple[bool, dict[str, int | None], str | None]:
    counts = {n.name: ledger_row_count(n.pg_port) for n in NODES}
    distinct = set(counts.values())
    return len(distinct) == 1 and None not in distinct, counts, str(counts)


def _safe_config(patroni: PatroniClient) -> dict[str, Any] | None:
    try:
        return patroni.config()
    except ClusterUnreachableError:
        return None


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
