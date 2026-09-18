"""Fault primitives: kill, stop, pause, partition, netem, fill_disk, switchover, restart_service.

Phase 2 ships ``kill`` and ``start``; the rest come with the scenario engine. Every fault
returns when Docker confirms it, and reports the monotonic time just before it was requested
and just after it was confirmed, so a metric can pick the reading it needs.
"""

from __future__ import annotations

import subprocess

from pydantic import BaseModel

from leaderfall.workload import Clock


class FaultTiming(BaseModel):
    action: str
    target: str
    requested_at: float
    done_at: float


def _docker(*args: str, timeout: float = 30.0) -> str:
    proc = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=True
    )
    return proc.stdout


def kill(clock: Clock, container: str, signal: str = "SIGKILL") -> FaultTiming:
    """``docker kill -s <signal>``. With SIGKILL the process is gone when this returns."""
    requested = clock.now()
    _docker("kill", "-s", signal, container)
    return FaultTiming(
        action=f"kill:{signal}", target=container, requested_at=requested, done_at=clock.now()
    )


def start(clock: Clock, container: str) -> FaultTiming:
    """``docker start``. Returns when the container process has been started."""
    requested = clock.now()
    _docker("start", container)
    return FaultTiming(
        action="start", target=container, requested_at=requested, done_at=clock.now()
    )


def container_logs(container: str, tail: int = 2000) -> str:
    """Recent stdout+stderr of a container."""
    proc = subprocess.run(
        ["docker", "logs", "--tail", str(tail), container],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.stdout + proc.stderr


def container_image(container: str) -> str | None:
    try:
        return _docker("inspect", "--format", "{{.Config.Image}}", container).strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def docker_server_version() -> str | None:
    try:
        return _docker("version", "--format", "{{.Server.Version}}").strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
