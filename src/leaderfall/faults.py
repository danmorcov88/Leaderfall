"""Fault primitives: kill, stop, pause, partition, netem, fill_disk, switchover, restart_service.

Every primitive returns when Docker (or Patroni) has confirmed it, and reports the monotonic
time just before it was requested and just after it was confirmed, so a metric can pick the
reading it needs.

Targets are resolved at run time from Patroni's view of the cluster: ``primary``,
``sync_replica``, ``any_replica``, ``failed_node`` (the target of the first fault),
``failed_node_2`` (the second node hit), ``old_primary`` (the leader before the first
fault), ``haproxy``, ``etcd-1``..``etcd-3``, or a node name such as ``pg-2``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import psycopg
from pydantic import BaseModel

from leaderfall.cluster import (
    COMPOSE_PROJECT,
    ETCD_CONTAINERS,
    HAPROXY_CONTAINER,
    HAPROXY_WRITE_PORT,
    NODES_BY_NAME,
    ClusterState,
    PatroniClient,
    conninfo,
)
from leaderfall.workload import Clock

NETWORK = COMPOSE_PROJECT  # the Compose network is named after the project
PG_IMAGE = "leaderfall/postgres-patroni:17.11-4.1.5"  # has iproute2; used for the tc helper


class FaultTiming(BaseModel):
    action: str
    target: str  # container name
    node: str | None = None  # pg node name when the target is a PostgreSQL node
    requested_at: float
    done_at: float
    detail: str | None = None


class FaultError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------------------


class TargetKind(StrEnum):
    PG = "pg"
    HAPROXY = "haproxy"
    ETCD = "etcd"


@dataclass(frozen=True)
class Target:
    container: str
    kind: TargetKind
    node: str | None = None  # pg node name for PG targets

    @property
    def label(self) -> str:
        return self.node or self.container


@dataclass
class TargetContext:
    """What earlier steps established, for the relative target names."""

    failed: Target | None = None  # target of the first fault
    failed_2: Target | None = None  # second distinct node hit by a fault or action
    old_primary: str | None = None  # leader before the first fault


def resolve_target(
    spec: str, state: ClusterState, ctx: TargetContext | Target | None = None
) -> Target:
    """Turn a scenario target such as ``primary`` into a concrete container."""
    if isinstance(ctx, Target):  # backwards-compatible: just the failed node
        ctx = TargetContext(failed=ctx)
    ctx = ctx or TargetContext()
    if spec == "primary":
        if state.leader is None:
            raise FaultError("no primary to target: Patroni reports no leader")
        return _pg(state.leader.name)
    if spec == "sync_replica":
        sync = [m for m in state.members if m.role == "sync_standby"]
        if not sync:
            raise FaultError("no sync_standby to target: is synchronous mode on?")
        return _pg(sorted(m.name for m in sync)[0])
    if spec == "any_replica":
        # The lowest-named replica that this scenario has not hit yet: Patroni keeps
        # listing a paused or freshly killed member for a while.
        hit = {t.node for t in (ctx.failed, ctx.failed_2) if t is not None}
        replicas = sorted(m.name for m in state.replicas if m.name not in hit)
        if not replicas:
            raise FaultError("no replica left to target")
        return _pg(replicas[0])
    if spec == "failed_node":
        if ctx.failed is None:
            raise FaultError("failed_node used before any fault was injected")
        return ctx.failed
    if spec == "failed_node_2":
        if ctx.failed_2 is None:
            raise FaultError("failed_node_2 used before a second node was hit")
        return ctx.failed_2
    if spec == "old_primary":
        if ctx.old_primary is None:
            raise FaultError("old_primary used before any fault was injected")
        return _pg(ctx.old_primary)
    if spec == "haproxy":
        return Target(HAPROXY_CONTAINER, TargetKind.HAPROXY)
    if spec.startswith("etcd-"):
        container = f"{COMPOSE_PROJECT}-{spec}"
        if container not in ETCD_CONTAINERS:
            raise FaultError(f"unknown etcd member {spec!r}")
        return Target(container, TargetKind.ETCD)
    if spec in NODES_BY_NAME:
        return _pg(spec)
    raise FaultError(f"unknown target {spec!r}")


def _pg(name: str) -> Target:
    return Target(NODES_BY_NAME[name].container, TargetKind.PG, node=name)


# --------------------------------------------------------------------------------------
# Docker helpers
# --------------------------------------------------------------------------------------


def _docker(*args: str, timeout: float = 60.0) -> str:
    try:
        proc = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=True
        )
    except subprocess.CalledProcessError as exc:
        raise FaultError(f"docker {' '.join(args)}: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FaultError(f"docker {' '.join(args)}: no answer after {timeout:.0f}s") from exc
    return proc.stdout


def _timed(
    clock: Clock, action: str, target: Target, detail: str | None, *args: str
) -> FaultTiming:
    requested = clock.now()
    _docker(*args)
    return FaultTiming(
        action=action,
        target=target.container,
        node=target.node,
        requested_at=requested,
        done_at=clock.now(),
        detail=detail,
    )


# --------------------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------------------


def kill(clock: Clock, target: Target, signal: str = "SIGKILL") -> FaultTiming:
    """``docker kill -s <signal>``. With SIGKILL the process is gone when this returns."""
    return _timed(clock, f"kill:{signal}", target, None, "kill", "-s", signal, target.container)


def stop(clock: Clock, target: Target, grace_sec: int = 60) -> FaultTiming:
    """``docker stop``: SIGTERM, then SIGKILL after ``grace_sec``. Patroni shuts down cleanly."""
    return _timed(clock, "stop", target, None, "stop", "-t", str(grace_sec), target.container)


def start(clock: Clock, target: Target) -> FaultTiming:
    return _timed(clock, "start", target, None, "start", target.container)


def pause(clock: Clock, target: Target) -> FaultTiming:
    """``docker pause``: every process in the container is frozen, TCP stays open."""
    return _timed(clock, "pause", target, None, "pause", target.container)


def unpause(clock: Clock, target: Target) -> FaultTiming:
    return _timed(clock, "unpause", target, None, "unpause", target.container)


def partition(clock: Clock, target: Target, network: str = NETWORK) -> FaultTiming:
    """Cut the container off the cluster network. It keeps running, nobody can reach it."""
    return _timed(
        clock, "partition", target, network, "network", "disconnect", network, target.container
    )


def heal(clock: Clock, target: Target, network: str = NETWORK) -> FaultTiming:
    return _timed(clock, "heal", target, network, "network", "connect", network, target.container)


def restart(clock: Clock, target: Target, grace_sec: int = 10) -> FaultTiming:
    """``docker restart``: for HAProxy or an etcd member."""
    return _timed(clock, "restart", target, None, "restart", "-t", str(grace_sec), target.container)


def switchover(
    clock: Clock, patroni: PatroniClient, target: Target, candidate: str | None = None
) -> FaultTiming:
    """Patroni ``POST /switchover``: a planned, clean leader change."""
    if target.node is None:
        raise FaultError("switchover target must be a PostgreSQL node")
    body: dict[str, str] = {"leader": target.node}
    if candidate:
        body["candidate"] = candidate
    requested = clock.now()
    reply = patroni.post("/switchover", body)
    return FaultTiming(
        action="switchover",
        target=target.container,
        node=target.node,
        requested_at=requested,
        done_at=clock.now(),
        detail=f"candidate={candidate or 'any'}: {reply}",
    )


def netem_args(spec: str) -> list[str]:
    """``"delay 500ms loss 10%"`` -> the ``tc qdisc`` arguments, validated."""
    words = spec.split()
    allowed = {"delay", "loss", "corrupt", "duplicate", "reorder", "rate", "jitter"}
    if not words or words[0] not in allowed:
        raise FaultError(f"netem spec must start with one of {sorted(allowed)}: {spec!r}")
    for w in words:
        if not all(c.isalnum() or c in ".%" for c in w):
            raise FaultError(f"bad netem token {w!r}")
    return ["qdisc", "add", "dev", "eth0", "root", "netem", *words]


def _tc(target: Target, *args: str) -> str:
    """Run ``tc`` in a throwaway container that shares the target's network namespace.

    The helper gets NET_ADMIN; the PostgreSQL containers themselves never do.
    """
    return _docker(
        "run", "--rm", "--net", f"container:{target.container}", "--cap-add", "NET_ADMIN",
        "--entrypoint", "tc", PG_IMAGE, *args,
    )  # fmt: skip


def netem(clock: Clock, target: Target, spec: str) -> FaultTiming:
    """Add network impairment (delay, loss, ...) on the target's interface.

    Needs the ``sch_netem`` kernel module on the Docker host. Docker Desktop's WSL2 kernel
    does not ship it; standard Linux kernels and GitHub runners do.
    """
    requested = clock.now()
    try:
        _tc(target, *netem_args(spec))
    except FaultError as exc:
        if "qdisc kind is unknown" in str(exc):
            raise FaultError(
                "tc netem is not available: the Docker host kernel has no sch_netem "
                "(Docker Desktop / WSL2). Run this scenario on a Linux host."
            ) from exc
        raise
    return FaultTiming(
        action="netem",
        target=target.container,
        node=target.node,
        requested_at=requested,
        done_at=clock.now(),
        detail=spec,
    )


def netem_clear(clock: Clock, target: Target) -> FaultTiming:
    requested = clock.now()
    try:
        _tc(target, "qdisc", "del", "dev", "eth0", "root")
    except FaultError as exc:
        if "handle of zero" not in str(exc) and "No such file" not in str(exc):
            raise  # nothing to delete is fine
    return FaultTiming(
        action="netem_clear",
        target=target.container,
        node=target.node,
        requested_at=requested,
        done_at=clock.now(),
    )


def netem_available() -> bool:
    """Whether the Docker host kernel can do ``tc netem`` (checked in a throwaway container)."""
    try:
        subprocess.run(
            ["docker", "run", "--rm", "--cap-add", "NET_ADMIN", "--entrypoint", "tc", PG_IMAGE,
             "qdisc", "add", "dev", "lo", "root", "netem", "delay", "1ms"],
            capture_output=True, text=True, timeout=60, check=True,
        )  # fmt: skip
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return True


PG_WAL_DIR = "/var/lib/postgresql/data/pgdata/pg_wal"
FILL_FILE = f"{PG_WAL_DIR}/leaderfall-fill"
BOUNDED_FS_MAX_BYTES = 4 << 30  # a pg_wal filesystem bigger than 4 GB is the shared host disk


def wal_filesystem(target: Target) -> tuple[int, int]:
    """``(size, available)`` in bytes of the filesystem holding the node's ``pg_wal``."""
    out = _docker(
        "exec", target.container, "df", "-B1", "--output=size,avail", PG_WAL_DIR
    ).splitlines()
    size, avail = out[-1].split()
    return int(size), int(avail)


def fill_disk(clock: Clock, target: Target, keep_mb: int = 0) -> FaultTiming:
    """Fill the filesystem under ``pg_wal`` until PostgreSQL cannot write WAL any more.

    Refuses to run unless that filesystem is bounded (under 4 GB): on Docker Desktop and
    on CI runners ``pg_wal`` sits on the shared VM disk, and filling it would take the
    Docker host down with it. A bounded ``pg_wal`` needs a size-limited volume driver or
    a mount the lab's containers are not allowed to make. See the runbook.
    """
    size, avail = wal_filesystem(target)
    if size > BOUNDED_FS_MAX_BYTES:
        raise FaultError(
            f"refusing to fill pg_wal on {target.label}: its filesystem is {size >> 30} GB, "
            "which is the shared Docker host disk, not a bounded WAL volume"
        )
    requested = clock.now()
    # fallocate reserves the space at once; keep_mb leaves a little for PostgreSQL to hit
    # ENOSPC on the next segment instead of failing this very call.
    to_take = max(0, avail - keep_mb * (1 << 20))
    _docker("exec", target.container, "fallocate", "-l", str(to_take), FILL_FILE)
    return FaultTiming(
        action="fill_disk",
        target=target.container,
        node=target.node,
        requested_at=requested,
        done_at=clock.now(),
        detail=f"took {to_take >> 20} MB of {size >> 20} MB",
    )


def free_disk(clock: Clock, target: Target) -> FaultTiming:
    requested = clock.now()
    _docker("exec", target.container, "rm", "-f", FILL_FILE)
    return FaultTiming(
        action="free_disk",
        target=target.container,
        node=target.node,
        requested_at=requested,
        done_at=clock.now(),
    )


def write_wal(clock: Clock, mb: int, port: int = HAPROXY_WRITE_PORT) -> FaultTiming:
    """Generate roughly ``mb`` megabytes of WAL on the primary with one bulk insert.

    Used to put real replication lag behind a frozen replica: a paused container still
    receives WAL into its TCP buffer, so only more WAL than the buffer holds is lag.
    """
    requested = clock.now()
    rows = mb * 2048  # ~512 bytes of WAL per row
    with psycopg.connect(conninfo(port, timeout=5), autocommit=True) as conn:
        conn.execute("create table if not exists leaderfall_bulk (x int, pad text)")
        conn.execute(
            "insert into leaderfall_bulk select g, repeat('x', 480) from generate_series(1, %s) g",
            (rows,),
        )
    return FaultTiming(
        action="write_wal",
        target="primary",
        requested_at=requested,
        done_at=clock.now(),
        detail=f"{mb} MB, {rows} rows",
    )


# --------------------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------------------


def container_logs(container: str, since: datetime | None = None, tail: int = 5000) -> str:
    """stdout+stderr of a container, only from ``since`` on (container logs outlive runs)."""
    args = ["docker", "logs", "--tail", str(tail)]
    if since is not None:
        args += ["--since", since.isoformat(timespec="seconds")]
    proc = subprocess.run(
        [*args, container],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.stdout + proc.stderr


def container_image(container: str) -> str | None:
    try:
        return _docker("inspect", "--format", "{{.Config.Image}}", container).strip() or None
    except FaultError:
        return None


def container_status(container: str) -> str | None:
    """``running``, ``paused``, ``exited``, ... or ``None`` if unknown."""
    try:
        return _docker("inspect", "--format", "{{.State.Status}}", container).strip() or None
    except FaultError:
        return None


def docker_server_version() -> str | None:
    try:
        return _docker("version", "--format", "{{.Server.Version}}").strip() or None
    except FaultError:
        return None
