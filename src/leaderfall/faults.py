"""Fault primitives: kill, stop, pause, partition, netem, fill_disk, switchover, restart_service.

Every primitive returns when Docker (or Patroni) has confirmed it, and reports the monotonic
time just before it was requested and just after it was confirmed, so a metric can pick the
reading it needs. ``netem`` and ``fill_disk`` arrive with the advanced scenarios.

Targets are resolved at run time from Patroni's view of the cluster: ``primary``,
``sync_replica``, ``any_replica``, ``failed_node`` (the target of the first fault),
``haproxy``, ``etcd-1``..``etcd-3``, or a node name such as ``pg-2``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from leaderfall.cluster import (
    COMPOSE_PROJECT,
    ETCD_CONTAINERS,
    HAPROXY_CONTAINER,
    NODES_BY_NAME,
    ClusterState,
    PatroniClient,
)
from leaderfall.workload import Clock

NETWORK = COMPOSE_PROJECT  # the Compose network is named after the project


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


def resolve_target(spec: str, state: ClusterState, failed: Target | None = None) -> Target:
    """Turn a scenario target such as ``primary`` into a concrete container."""
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
        replicas = sorted(m.name for m in state.replicas)
        if not replicas:
            raise FaultError("no replica to target")
        return _pg(replicas[0])
    if spec == "failed_node":
        if failed is None:
            raise FaultError("failed_node used before any fault was injected")
        return failed
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
