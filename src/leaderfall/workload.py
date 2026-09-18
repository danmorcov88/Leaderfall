"""Write and read workload against HAProxy, with a ledger of every confirmed commit.

Three background threads collect the raw data every metric is computed from:

- ``Writer``: one row per transaction through HAProxy :5000. Every attempt goes into the
  ``Ledger`` as ``acked``, ``failed`` or ``unknown``.
- ``Reader``: a small query through HAProxy :5001, recorded as ok/failed with the node hit.
- ``NodePoller``: asks every node directly (not through the proxy) whether it is a primary,
  probes it with a real write when it says so, and records who Patroni calls the leader.

All timestamps are seconds since the run started, from the host's monotonic clock.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from enum import StrEnum
from pathlib import Path
from typing import Any

import psycopg
from pydantic import BaseModel

from leaderfall.cluster import (
    HAPROXY_READ_PORT,
    HAPROXY_WRITE_PORT,
    NODES,
    ClusterUnreachableError,
    Node,
    PatroniClient,
    conninfo,
)

LEDGER_TABLE = "ledger_writes"
PROBE_TABLE = "leaderfall_probe"

SCHEMA_SQL = (
    f"create table if not exists {LEDGER_TABLE} ("
    " id bigint primary key, sent_at timestamptz not null, payload text not null)",
    f"create table if not exists {PROBE_TABLE} ("
    " node text not null, round bigint not null, at timestamptz not null default now())",
)

# Short client-side limits so a dead or frozen server is noticed quickly. Keepalives catch a
# vanished peer; statement_timeout catches a server that answers but does not finish.
CLIENT_OPTIONS = (
    "keepalives=1 keepalives_idle=1 keepalives_interval=1 keepalives_count=2"
    " options='-c statement_timeout=2000'"
)


class Clock:
    """Seconds since the run started, from ``time.monotonic()``."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.started_at_wall = time.time()

    def now(self) -> float:
        return time.monotonic() - self.t0


NODE_NAME_SQL = "select current_setting('leaderfall.node_name', true)"


def client_conninfo(port: int, connect_timeout: int = 2) -> str:
    return f"{conninfo(port, timeout=connect_timeout)} {CLIENT_OPTIONS}"


# --------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------


class WriteResult(StrEnum):
    ACKED = "acked"  # COMMIT confirmed by the server
    FAILED = "failed"  # error before COMMIT was sent; the row is certainly not committed
    UNKNOWN = "unknown"  # connection broke after COMMIT was sent, before the answer arrived


class LedgerEntry(BaseModel):
    id: int
    sent_at: float
    done_at: float
    result: WriteResult
    server: str | None = None  # node the connection was on (custom GUC leaderfall.node_name)
    error: str | None = None


class Ledger:
    """Append-only record of write attempts, kept in memory and streamed to a JSONL file."""

    def __init__(self, path: Path | None = None) -> None:
        self.entries: list[LedgerEntry] = []
        self._lock = threading.Lock()
        self._file = path.open("a", encoding="utf-8") if path else None

    def add(self, entry: LedgerEntry) -> None:
        with self._lock:
            self.entries.append(entry)
            if self._file:
                self._file.write(entry.model_dump_json() + "\n")
                self._file.flush()

    def snapshot(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self.entries)

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None

    @staticmethod
    def load(path: Path) -> list[LedgerEntry]:
        with path.open(encoding="utf-8") as f:
            return [LedgerEntry.model_validate_json(line) for line in f if line.strip()]


# --------------------------------------------------------------------------------------
# Rate-paced background thread
# --------------------------------------------------------------------------------------


class _PacedThread(threading.Thread):
    """Runs ``tick()`` ``rate_per_sec`` times per second until stopped."""

    def __init__(self, name: str, clock: Clock, rate_per_sec: float) -> None:
        super().__init__(name=name, daemon=True)
        self.clock = clock
        self.period = 1.0 / rate_per_sec
        self._stop = threading.Event()
        self.exception: BaseException | None = None

    def stop(self, join_timeout: float = 10.0) -> None:
        self._stop.set()
        self.join(join_timeout)

    def run(self) -> None:
        try:
            next_tick = time.monotonic()
            while not self._stop.is_set():
                self.tick()
                next_tick += self.period
                delay = next_tick - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    next_tick = time.monotonic()  # fell behind: do not try to catch up
        except BaseException as exc:  # stored for the runner to report; the thread just ends
            self.exception = exc
        finally:
            self.close()

    def tick(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _server_name(conn: psycopg.Connection[Any]) -> str | None:
    row = conn.execute(NODE_NAME_SQL).fetchone()
    return str(row[0]) if row and row[0] else None


def _error_text(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{exc.__class__.__name__}: {text[:160]}"


# --------------------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------------------


class Writer(_PacedThread):
    """Inserts one row per transaction through HAProxy and records every attempt.

    Explicit BEGIN / INSERT / COMMIT so the two failure kinds can be told apart:
    an error on INSERT means nothing was committed (``failed``); an error on COMMIT means
    the server may or may not have committed (``unknown``).
    """

    def __init__(
        self,
        clock: Clock,
        ledger: Ledger,
        *,
        rate_per_sec: float = 50.0,
        first_id: int = 1,
        port: int = HAPROXY_WRITE_PORT,
        payload: str = "x" * 64,
    ) -> None:
        super().__init__("writer", clock, rate_per_sec)
        self.ledger = ledger
        self.next_id = first_id
        self.port = port
        self.payload = payload
        self._conn: psycopg.Connection[Any] | None = None
        self._server: str | None = None

    def _connect(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(client_conninfo(self.port), autocommit=False)
            self._server = _server_name(self._conn)
            self._conn.rollback()  # end the transaction _server_name opened
        return self._conn

    def _drop_connection(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(psycopg.Error):
                self._conn.close()
        self._conn = None
        self._server = None

    def tick(self) -> None:
        write_id = self.next_id
        self.next_id += 1
        sent_at = self.clock.now()
        result, error = WriteResult.FAILED, None
        server: str | None = None
        try:
            conn = self._connect()
            server = self._server
        except psycopg.Error as exc:
            error = f"connect: {_error_text(exc)}"
            self._drop_connection()
        else:
            try:
                conn.execute(
                    f"insert into {LEDGER_TABLE} (id, sent_at, payload) values (%s, now(), %s)",
                    (write_id, self.payload),
                )
            except psycopg.Error as exc:
                error = f"insert: {_error_text(exc)}"
                self._drop_connection()
            else:
                try:
                    conn.commit()
                    result = WriteResult.ACKED
                except psycopg.Error as exc:
                    # COMMIT was sent. Whether it landed is decided later against the table.
                    result, error = WriteResult.UNKNOWN, f"commit: {_error_text(exc)}"
                    self._drop_connection()
        self.ledger.add(
            LedgerEntry(
                id=write_id,
                sent_at=sent_at,
                done_at=self.clock.now(),
                result=result,
                server=server,
                error=error,
            )
        )

    def close(self) -> None:
        self._drop_connection()


# --------------------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------------------


class ReadSample(BaseModel):
    sent_at: float
    done_at: float
    ok: bool
    server: str | None = None
    error: str | None = None


class Reader(_PacedThread):
    """Runs a small query through HAProxy :5001 and records success and the node hit."""

    def __init__(
        self, clock: Clock, *, rate_per_sec: float = 10.0, port: int = HAPROXY_READ_PORT
    ) -> None:
        super().__init__("reader", clock, rate_per_sec)
        self.port = port
        self.samples: list[ReadSample] = []
        self._lock = threading.Lock()
        self._conn: psycopg.Connection[Any] | None = None

    def snapshot(self) -> list[ReadSample]:
        with self._lock:
            return list(self.samples)

    def tick(self) -> None:
        sent_at = self.clock.now()
        ok, server, error = False, None, None
        try:
            if self._conn is None or self._conn.closed:
                self._conn = psycopg.connect(client_conninfo(self.port), autocommit=True)
            row = self._conn.execute(
                "select current_setting('leaderfall.node_name', true), pg_is_in_recovery()"
            ).fetchone()
            if row is not None:
                server, ok = str(row[0]), bool(row[1])
        except psycopg.Error as exc:
            error = _error_text(exc)
            self.close()
        with self._lock:
            self.samples.append(
                ReadSample(
                    sent_at=sent_at, done_at=self.clock.now(), ok=ok, server=server, error=error
                )
            )

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(psycopg.Error):
                self._conn.close()
            self._conn = None


# --------------------------------------------------------------------------------------
# Node poller
# --------------------------------------------------------------------------------------


class NodeSample(BaseModel):
    """One node, one round, seen through a direct connection."""

    node: str
    in_recovery: bool | None = None  # None: not reachable
    timeline: int | None = None
    write_ok: bool | None = None  # probe write result; only attempted when in_recovery is False
    error: str | None = None


class PollRound(BaseModel):
    round: int
    t: float
    leader: str | None = (
        None  # who Patroni's /cluster calls the leader, None if nobody / unreachable
    )
    members: dict[str, dict[str, Any]] = {}  # name -> {role, state, lag, timeline}
    patroni_error: str | None = None
    nodes: list[NodeSample] = []

    @property
    def primaries(self) -> list[str]:
        return [n.node for n in self.nodes if n.in_recovery is False]

    @property
    def writable(self) -> list[str]:
        return [n.node for n in self.nodes if n.write_ok]


class NodePoller(_PacedThread):
    """Ground truth per node, independent of HAProxy and of Patroni's own view.

    Each round, for every node over its direct port: ``pg_is_in_recovery()`` and the
    control-file timeline. A node that says it is a primary is also asked to commit a
    probe row. Two nodes committing in the same round is the definition of split brain.
    The round also records who Patroni's ``/cluster`` endpoint reports as leader.

    Nodes are sampled in parallel. A node that stops answering is reconnected in the
    background and reported as unreachable until that succeeds, so a dead node never
    stretches a round: on Docker Desktop the published port of a dead container still
    accepts TCP connections, and only a timeout ends the attempt.
    """

    def __init__(
        self,
        clock: Clock,
        patroni: PatroniClient,
        *,
        rate_per_sec: float = 2.0,
        nodes: tuple[Node, ...] = NODES,
    ) -> None:
        super().__init__("poller", clock, rate_per_sec)
        self.patroni = patroni
        self.nodes = nodes
        self.rounds: list[PollRound] = []
        self._lock = threading.Lock()
        self._conns: dict[str, psycopg.Connection[Any]] = {}
        self._reconnects: dict[str, Future[psycopg.Connection[Any]]] = {}
        self._round = 0
        self._pool = ThreadPoolExecutor(max_workers=len(nodes), thread_name_prefix="poll")
        self._dialer = ThreadPoolExecutor(max_workers=len(nodes), thread_name_prefix="dial")

    def snapshot(self) -> list[PollRound]:
        with self._lock:
            return list(self.rounds)

    @staticmethod
    def _dial(node: Node) -> psycopg.Connection[Any]:
        return psycopg.connect(client_conninfo(node.pg_port, connect_timeout=2), autocommit=True)

    def _conn(self, node: Node) -> psycopg.Connection[Any] | None:
        """The node's connection, or ``None`` while a background reconnect is in flight."""
        conn = self._conns.get(node.name)
        if conn is not None and not conn.closed:
            return conn
        future = self._reconnects.get(node.name)
        if future is None:
            self._reconnects[node.name] = self._dialer.submit(self._dial, node)
            return None
        if not future.done():
            return None
        del self._reconnects[node.name]
        conn = future.result()  # raises psycopg.Error if the dial failed; caller retries
        self._conns[node.name] = conn
        return conn

    def _drop(self, node: Node) -> None:
        conn = self._conns.pop(node.name, None)
        if conn is not None:
            with contextlib.suppress(psycopg.Error):
                conn.close()

    def _sample_node(self, node: Node, round_no: int) -> NodeSample:
        sample = NodeSample(node=node.name)
        try:
            conn = self._conn(node)
            if conn is None:
                sample.error = "reconnecting"
                return sample
            row = conn.execute(
                "select pg_is_in_recovery(), (pg_control_checkpoint()).timeline_id"
            ).fetchone()
        except psycopg.Error as exc:
            sample.error = _error_text(exc)
            self._drop(node)
            return sample
        if row is None:
            return sample
        sample.in_recovery, sample.timeline = bool(row[0]), int(row[1])
        if sample.in_recovery:
            return sample
        try:
            conn.execute(
                f"insert into {PROBE_TABLE} (node, round) values (%s, %s)", (node.name, round_no)
            )
            sample.write_ok = True
        except psycopg.Error as exc:
            sample.write_ok = False
            sample.error = f"probe: {_error_text(exc)}"
            self._drop(node)
        return sample

    def tick(self) -> None:
        self._round += 1
        rnd = PollRound(round=self._round, t=self.clock.now())
        try:
            state = self.patroni.cluster()
        except ClusterUnreachableError as exc:
            rnd.patroni_error = str(exc)
        else:
            rnd.leader = state.leader.name if state.leader else None
            rnd.members = {
                m.name: {"role": m.role, "state": m.state, "lag": m.lag, "timeline": m.timeline}
                for m in state.members
            }
        round_no = self._round
        rnd.nodes = list(self._pool.map(lambda n: self._sample_node(n, round_no), self.nodes))
        with self._lock:
            self.rounds.append(rnd)

    def close(self) -> None:
        self._pool.shutdown(wait=True)
        self._dialer.shutdown(wait=True)
        for future in self._reconnects.values():
            with contextlib.suppress(psycopg.Error):
                future.result().close()
        self._reconnects.clear()
        for node in self.nodes:
            self._drop(node)


# --------------------------------------------------------------------------------------
# Helpers used by the scenario runner
# --------------------------------------------------------------------------------------


def prepare_schema(port: int = HAPROXY_WRITE_PORT) -> None:
    """Create the workload tables on the primary and start from empty ones."""
    with psycopg.connect(conninfo(port, timeout=5), autocommit=True) as conn:
        for statement in SCHEMA_SQL:
            conn.execute(statement)
        conn.execute(f"truncate {LEDGER_TABLE}, {PROBE_TABLE}")


def ledger_ids_present(port: int, ids: list[int]) -> set[int]:
    """Which of ``ids`` exist in the ledger table on the node behind ``port``."""
    if not ids:
        return set()
    with psycopg.connect(conninfo(port, timeout=5)) as conn:
        rows = conn.execute(f"select id from {LEDGER_TABLE} where id = any(%s)", (ids,)).fetchall()
    return {int(r[0]) for r in rows}


def ledger_row_count(port: int) -> int | None:
    try:
        with psycopg.connect(conninfo(port, timeout=5)) as conn:
            row = conn.execute(f"select count(*) from {LEDGER_TABLE}").fetchone()
    except psycopg.Error:
        return None
    return int(row[0]) if row else None


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)
