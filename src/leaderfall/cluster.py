"""Cluster lifecycle: up/down/status, wait-for-healthy, Patroni REST client.

The cluster is defined in ``docker-compose.yml``. This module starts it, waits on real
conditions (never fixed sleeps), and makes sure the running Patroni dynamic config matches
the requested profile and sync mode.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import psycopg
from pydantic import BaseModel, ConfigDict

# --------------------------------------------------------------------------------------
# Static topology. Must match docker-compose.yml.
# --------------------------------------------------------------------------------------

COMPOSE_PROJECT = "leaderfall"
COMPOSE_FILE = "docker-compose.yml"
SCOPE = "leaderfall"

PG_USER = "postgres"
PG_PASSWORD = os.environ.get("LEADERFALL_SUPERUSER_PASSWORD", "postgres")
PG_DATABASE = "postgres"


@dataclass(frozen=True)
class Node:
    """One PostgreSQL + Patroni node and the host ports it is published on."""

    name: str
    rest_port: int
    pg_port: int

    @property
    def container(self) -> str:
        return f"{COMPOSE_PROJECT}-{self.name}"


NODES: tuple[Node, ...] = (
    Node("pg-1", rest_port=8011, pg_port=5441),
    Node("pg-2", rest_port=8012, pg_port=5442),
    Node("pg-3", rest_port=8013, pg_port=5443),
)
NODES_BY_NAME: Mapping[str, Node] = {n.name: n for n in NODES}

ETCD_CONTAINERS: tuple[str, ...] = tuple(f"{COMPOSE_PROJECT}-etcd-{i}" for i in (1, 2, 3))
HAPROXY_CONTAINER = f"{COMPOSE_PROJECT}-haproxy"

HAPROXY_WRITE_PORT = 5000
HAPROXY_READ_PORT = 5001
HAPROXY_STATS_PORT = 7000


def docker_host() -> str:
    """Address where published container ports are reachable.

    An IP, not ``localhost``: with a hostname every failed connect is tried twice (IPv6
    then IPv4), which doubles the time it takes to notice a dead node.
    """
    return os.environ.get("LEADERFALL_HOST", "127.0.0.1")


def find_repo_root(start: Path | None = None) -> Path:
    """Directory that holds ``docker-compose.yml``.

    Order: ``LEADERFALL_ROOT`` env var, then the current directory and its parents, then the
    source tree this package was installed from (editable installs).
    """
    if env := os.environ.get("LEADERFALL_ROOT"):
        return Path(env)
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / COMPOSE_FILE).is_file():
            return candidate
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / COMPOSE_FILE).is_file():
        return source_root
    raise FileNotFoundError(
        f"{COMPOSE_FILE} not found. Run from the repository or set LEADERFALL_ROOT."
    )


# --------------------------------------------------------------------------------------
# Configuration profiles.
# --------------------------------------------------------------------------------------


class PatroniProfile(StrEnum):
    DEFAULT = "default"
    FAST = "fast"


class SyncMode(StrEnum):
    OFF = "off"
    ON = "on"
    STRICT = "strict"


@dataclass(frozen=True)
class Timing:
    ttl: int
    loop_wait: int
    retry_timeout: int


# Patroni refuses a ttl under 20 s: config.py silently raises anything lower to 20. The
# "fast" profile therefore uses 20, the real floor, so that the config says what runs.
PROFILES: Mapping[PatroniProfile, Timing] = {
    PatroniProfile.DEFAULT: Timing(ttl=30, loop_wait=10, retry_timeout=10),
    PatroniProfile.FAST: Timing(ttl=20, loop_wait=5, retry_timeout=5),
}

DEFAULT_MAX_LAG_ON_FAILOVER = 1_048_576  # bytes, Patroni's own default


@dataclass(frozen=True)
class ClusterConfig:
    """What the user asked for with ``leaderfall up``."""

    profile: PatroniProfile = PatroniProfile.DEFAULT
    sync: SyncMode = SyncMode.OFF
    maximum_lag_on_failover: int = DEFAULT_MAX_LAG_ON_FAILOVER

    @property
    def timing(self) -> Timing:
        return PROFILES[self.profile]

    def dcs_config(self) -> dict[str, Any]:
        """The part of Patroni's dynamic configuration this config controls."""
        return {
            "ttl": self.timing.ttl,
            "loop_wait": self.timing.loop_wait,
            "retry_timeout": self.timing.retry_timeout,
            "maximum_lag_on_failover": self.maximum_lag_on_failover,
            "synchronous_mode": self.sync is not SyncMode.OFF,
            "synchronous_mode_strict": self.sync is SyncMode.STRICT,
        }

    @property
    def expected_sync_standbys(self) -> int:
        """Patroni's ``synchronous_node_count`` is left at 1, so sync mode means one standby."""
        return 0 if self.sync is SyncMode.OFF else 1


def config_diff(desired: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Keys of ``desired`` whose value differs from ``current``.

    Patroni stores ``synchronous_mode`` as a bool when given ``on``/``off``, and as the
    string ``quorum`` otherwise; both are normalised before comparing.
    """

    def norm(value: Any) -> Any:
        if isinstance(value, str) and value.lower() in {"on", "true", "yes"}:
            return True
        if isinstance(value, str) and value.lower() in {"off", "false", "no"}:
            return False
        return value

    return {k: v for k, v in desired.items() if norm(current.get(k)) != norm(v)}


# --------------------------------------------------------------------------------------
# Cluster state as reported by Patroni's /cluster endpoint.
# --------------------------------------------------------------------------------------

LEADER_ROLES = frozenset({"leader"})
REPLICA_ROLES = frozenset({"replica", "sync_standby", "quorum_standby"})


class Member(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    role: str
    state: str
    timeline: int | None = None
    lag: int | str | None = None  # Patroni reports "unknown" when it cannot compute it
    host: str | None = None
    port: int | None = None

    @property
    def lag_bytes(self) -> int | None:
        return self.lag if isinstance(self.lag, int) else None

    @property
    def is_leader(self) -> bool:
        return self.role in LEADER_ROLES

    @property
    def is_streaming_replica(self) -> bool:
        return self.role in REPLICA_ROLES and self.state == "streaming"


class ClusterState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    members: list[Member]
    scope: str = SCOPE

    @property
    def leader(self) -> Member | None:
        return next((m for m in self.members if m.is_leader), None)

    @property
    def replicas(self) -> list[Member]:
        return [m for m in self.members if m.role in REPLICA_ROLES]

    def member(self, name: str) -> Member | None:
        return next((m for m in self.members if m.name == name), None)

    def health_problems(
        self,
        expected_members: int = 3,
        max_lag_bytes: int = 0,
        sync_standbys: int | None = None,
    ) -> list[str]:
        """Empty when the cluster is healthy; otherwise one line per problem.

        Healthy means: exactly one running leader, every other member a streaming replica
        with lag at or under ``max_lag_bytes``, and everyone on the same timeline. When
        ``sync_standbys`` is given, exactly that many members must have the
        ``sync_standby`` role (0 when sync mode is off).
        """
        problems: list[str] = []
        if sync_standbys is not None:
            found = sum(m.role == "sync_standby" for m in self.members)
            if found != sync_standbys:
                problems.append(f"expected {sync_standbys} sync standby, found {found}")
        leaders = [m for m in self.members if m.is_leader]
        if len(leaders) != 1:
            problems.append(f"expected 1 leader, found {len(leaders)}")
        elif leaders[0].state != "running":
            problems.append(f"leader {leaders[0].name} is {leaders[0].state}, not running")

        if len(self.members) != expected_members:
            problems.append(f"expected {expected_members} members, found {len(self.members)}")

        for m in self.members:
            if m.is_leader:
                continue
            if not m.is_streaming_replica:
                problems.append(f"{m.name} is {m.role}/{m.state}, not a streaming replica")
                continue
            if m.lag_bytes is None or m.lag_bytes > max_lag_bytes:
                problems.append(f"{m.name} lag is {m.lag}, limit {max_lag_bytes}")

        timelines = {m.timeline for m in self.members if m.timeline is not None}
        if len(timelines) > 1:
            problems.append(f"members are on different timelines: {sorted(timelines)}")
        if any(m.timeline is None for m in self.members):
            problems.append("some members have no timeline yet")
        return problems

    def is_healthy(
        self, expected_members: int = 3, max_lag_bytes: int = 0, sync_standbys: int | None = None
    ) -> bool:
        return not self.health_problems(expected_members, max_lag_bytes, sync_standbys)


def expected_sync_standbys(dynamic_config: Mapping[str, Any]) -> int:
    """How many ``sync_standby`` members a cluster with this dynamic config should have."""
    mode = dynamic_config.get("synchronous_mode", False)
    if isinstance(mode, str):
        mode = mode.lower() not in {"off", "false", "no"}
    if not mode:
        return 0
    count = dynamic_config.get("synchronous_node_count", 1)
    return int(count) if isinstance(count, int | str) else 1


# --------------------------------------------------------------------------------------
# Waiting on real conditions.
# --------------------------------------------------------------------------------------


class WaitTimeoutError(TimeoutError):
    def __init__(self, what: str, timeout: float, last: str | None) -> None:
        detail = f" (last: {last})" if last else ""
        super().__init__(f"timed out after {timeout:.0f}s waiting for {what}{detail}")
        self.what = what
        self.timeout = timeout
        self.last = last


def wait_until[T](
    check: Callable[[], tuple[bool, T, str | None]],
    *,
    what: str,
    timeout: float,
    interval: float = 0.5,
) -> tuple[T, float]:
    """Poll ``check`` until it reports success. Returns ``(value, seconds_waited)``.

    ``check`` returns ``(done, value, detail)``. ``detail`` is the reason it is not done yet
    and goes into the timeout error. All timing uses the monotonic clock.
    """
    start = time.monotonic()
    last: str | None = None
    while True:
        done, value, last = check()
        if done:
            return value, time.monotonic() - start
        if time.monotonic() - start >= timeout:
            raise WaitTimeoutError(what, timeout, last)
        time.sleep(interval)


# --------------------------------------------------------------------------------------
# Patroni REST client.
# --------------------------------------------------------------------------------------


class ClusterUnreachableError(RuntimeError):
    pass


class PatroniClient:
    """Talks to the Patroni REST API on the published host ports."""

    def __init__(self, nodes: tuple[Node, ...] = NODES, host: str | None = None) -> None:
        self.nodes = nodes
        self.host = host or docker_host()
        # Short: a healthy node answers in milliseconds, and on Docker Desktop the published
        # port of a dead container still accepts TCP, so only the read timeout ends a call.
        self._http = httpx.Client(timeout=httpx.Timeout(1.0, connect=0.5))
        self._preferred: Node | None = None  # last node that answered; asked first next time

    def url(self, node: Node, path: str) -> str:
        return f"http://{self.host}:{node.rest_port}{path}"

    def _candidates(self) -> list[Node]:
        first = self._preferred
        return [n for n in self.nodes if n is first] + [n for n in self.nodes if n is not first]

    def _first_answer(self, path: str) -> httpx.Response:
        errors: list[str] = []
        for node in self._candidates():
            try:
                response = self._http.get(self.url(node, path))
            except httpx.HTTPError as exc:
                errors.append(f"{node.name}: {exc.__class__.__name__}")
                continue
            if response.status_code == 200:
                self._preferred = node
                return response
            errors.append(f"{node.name}: HTTP {response.status_code}")
        raise ClusterUnreachableError(f"no node answered GET {path}: {', '.join(errors)}")

    def cluster(self) -> ClusterState:
        return ClusterState.model_validate(self._first_answer("/cluster").json())

    def config(self) -> dict[str, Any]:
        data: dict[str, Any] = self._first_answer("/config").json()
        return data

    def patch_config(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        for node in self._candidates():
            try:
                response = self._http.patch(self.url(node, "/config"), json=dict(patch))
            except httpx.HTTPError as exc:
                errors.append(f"{node.name}: {exc.__class__.__name__}")
                continue
            if response.status_code == 200:
                data: dict[str, Any] = response.json()
                return data
            errors.append(f"{node.name}: HTTP {response.status_code} {response.text[:80]}")
        raise ClusterUnreachableError(f"no node accepted PATCH /config: {', '.join(errors)}")

    def post(self, path: str, body: Mapping[str, Any], timeout: float = 90.0) -> str:
        """POST to the first node that accepts it. Returns the text reply.

        Long timeout: ``/switchover`` answers only when the switchover is complete.
        """
        errors: list[str] = []
        for node in self._candidates():
            try:
                response = self._http.post(self.url(node, path), json=dict(body), timeout=timeout)
            except httpx.HTTPError as exc:
                errors.append(f"{node.name}: {exc.__class__.__name__}")
                continue
            if response.status_code in (200, 202):
                self._preferred = node
                return response.text.strip()
            errors.append(f"{node.name}: HTTP {response.status_code} {response.text[:120]}")
        raise ClusterUnreachableError(f"no node accepted POST {path}: {', '.join(errors)}")

    def node_status(self, node: Node) -> dict[str, Any] | None:
        """``GET /patroni`` on one node, or ``None`` if it does not answer."""
        try:
            response = self._http.get(self.url(node, "/patroni"))
        except httpx.HTTPError:
            return None
        if response.status_code >= 500:
            return None
        data: dict[str, Any] = response.json()
        return data

    def close(self) -> None:
        self._http.close()


# --------------------------------------------------------------------------------------
# Docker Compose wrapper.
# --------------------------------------------------------------------------------------


class Compose:
    def __init__(self, root: Path | None = None, profiles: tuple[str, ...] = ()) -> None:
        self.root = root or find_repo_root()
        self.profiles = profiles

    def _run(self, *args: str, env: Mapping[str, str] | None = None) -> None:
        cmd = ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", COMPOSE_FILE]
        for profile in self.profiles:
            cmd += ["--profile", profile]
        cmd += list(args)
        subprocess.run(cmd, cwd=self.root, env={**os.environ, **(env or {})}, check=True)

    def build(self) -> None:
        # Without this, BuildKit attaches a fresh provenance attestation to every build, the
        # image ID changes even when fully cached, and `up` recreates all containers (which
        # is a failover). With it, an unchanged Dockerfile gives an unchanged image ID.
        self._run("build", "--quiet", env={"BUILDX_NO_DEFAULT_ATTESTATIONS": "1"})

    def up(self, *, build: bool = True) -> None:
        if build:
            self.build()
        self._run("--progress", "quiet", "up", "-d", "--remove-orphans")

    def restart(self, service: str) -> None:
        self._run("--progress", "quiet", "restart", service)

    def haproxy_config_is_stale(self) -> bool:
        """True when haproxy.cfg was edited after the HAProxy container last started."""
        cfg = self.root / "docker" / "haproxy" / "haproxy.cfg"
        try:
            started = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.StartedAt}}", HAPROXY_CONTAINER],
                capture_output=True, text=True, check=True, timeout=15,
            ).stdout.strip()  # fmt: skip
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
        started_at = datetime.fromisoformat(started[:26] + "+00:00").timestamp()
        return cfg.stat().st_mtime > started_at

    def down(self, *, volumes: bool = False) -> None:
        args = ["down", "--remove-orphans"]
        if volumes:
            args.append("--volumes")
        self._run(*args)


# --------------------------------------------------------------------------------------
# Direct PostgreSQL access.
# --------------------------------------------------------------------------------------


def conninfo(port: int, *, host: str | None = None, timeout: int = 2) -> str:
    return (
        f"host={host or docker_host()} port={port} user={PG_USER} password={PG_PASSWORD} "
        f"dbname={PG_DATABASE} connect_timeout={timeout} application_name=leaderfall"
    )


def in_recovery(port: int) -> bool | None:
    """``pg_is_in_recovery()`` over a fresh connection, or ``None`` if unreachable."""
    try:
        with psycopg.connect(conninfo(port)) as conn:
            row = conn.execute("select pg_is_in_recovery()").fetchone()
    except psycopg.Error:
        return None
    return bool(row[0]) if row else None


# --------------------------------------------------------------------------------------
# High-level operations used by the CLI.
# --------------------------------------------------------------------------------------


@dataclass
class UpResult:
    config: ClusterConfig
    state: ClusterState
    seconds_to_healthy: float
    config_patched: dict[str, Any]
    seconds_total: float


class Cluster:
    def __init__(
        self,
        compose: Compose | None = None,
        patroni: PatroniClient | None = None,
        log: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.compose = compose or Compose()
        self.patroni = patroni or PatroniClient()
        self.log = log

    def status(self) -> ClusterState:
        return self.patroni.cluster()

    def wait_healthy(
        self, timeout: float, max_lag_bytes: int = 0, sync_standbys: int | None = None
    ) -> tuple[ClusterState, float]:
        def check() -> tuple[bool, ClusterState | None, str | None]:
            try:
                state = self.patroni.cluster()
            except ClusterUnreachableError as exc:
                return False, None, str(exc)
            problems = state.health_problems(
                max_lag_bytes=max_lag_bytes, sync_standbys=sync_standbys
            )
            return not problems, state, "; ".join(problems) or None

        state, waited = wait_until(check, what="a healthy cluster", timeout=timeout)
        assert state is not None
        return state, waited

    def wait_proxy(self, timeout: float) -> float:
        """Until HAProxy routes :5000 to a primary and :5001 to a replica."""

        def check() -> tuple[bool, None, str | None]:
            write = in_recovery(HAPROXY_WRITE_PORT)
            read = in_recovery(HAPROXY_READ_PORT)
            ok = write is False and read is True
            return ok, None, f"write port in_recovery={write}, read port in_recovery={read}"

        _, waited = wait_until(check, what="HAProxy routing", timeout=timeout)
        return waited

    def ensure_config(self, config: ClusterConfig) -> dict[str, Any]:
        """PATCH the dynamic config if it differs from ``config``. Returns what was patched."""
        patch = config_diff(config.dcs_config(), self.patroni.config())
        if patch:
            self.log(f"patching dynamic config: {patch}")
            self.patroni.patch_config(patch)
        return patch

    def up(self, config: ClusterConfig, *, timeout: float = 180.0, build: bool = True) -> UpResult:
        start = time.monotonic()
        self.log("docker compose up")
        self.compose.up(build=build)
        if self.compose.haproxy_config_is_stale():
            self.log("haproxy.cfg changed since HAProxy started: restarting it")
            self.compose.restart("haproxy")

        self.log("waiting for one leader and two streaming replicas")
        state, to_healthy = self.wait_healthy(timeout)
        leader = state.leader.name if state.leader else "?"
        self.log(f"healthy after {to_healthy:.1f}s, leader {leader}")

        patched = self.ensure_config(config)
        if patched or state.health_problems(sync_standbys=config.expected_sync_standbys):
            # Patroni applies the new config on its next loop; sync mode also needs it to
            # pick (or release) a sync standby. Wait for the cluster to settle again.
            remaining = max(10.0, timeout - (time.monotonic() - start))
            state, settled = self.wait_healthy(
                remaining, sync_standbys=config.expected_sync_standbys
            )
            self.log(f"config applied after {settled:.1f}s")

        remaining = max(10.0, timeout - (time.monotonic() - start))
        waited = self.wait_proxy(remaining)
        self.log(f"HAProxy routing ok after {waited:.1f}s")

        return UpResult(
            config=config,
            state=state,
            seconds_to_healthy=to_healthy,
            config_patched=patched,
            seconds_total=time.monotonic() - start,
        )

    def down(self, *, volumes: bool = False) -> None:
        self.compose.down(volumes=volumes)
