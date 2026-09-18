"""Metrics: detection time, RTO, RPO, split brain, rejoin time, diverged rows, final state.

Everything here is a pure function over data collected by ``workload``: the ledger, the
reader samples and the poller rounds. Nothing talks to the cluster, so all of it is unit
tested with hand-made inputs.

Time values are seconds since run start (see ``workload.Clock``).
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from pydantic import BaseModel

from leaderfall.workload import LedgerEntry, PollRound, ReadSample, WriteResult

# --------------------------------------------------------------------------------------
# Result models
# --------------------------------------------------------------------------------------


class LedgerSummary(BaseModel):
    attempts: int
    acked: int
    failed: int
    unknown: int
    first_id: int | None = None
    last_id: int | None = None


class RpoResult(BaseModel):
    """What happened to the commits the client was told about."""

    lost_acked: list[int]  # acked by the server, absent on the final primary: data loss
    unknown: list[int]  # COMMIT sent, no answer
    unknown_present: list[int]  # ... and the row is there
    unknown_missing: list[int]  # ... and the row is not

    @property
    def lost_acked_commits(self) -> int:
        return len(self.lost_acked)


class SplitBrainRound(BaseModel):
    round: int
    t: float
    primaries: list[str]  # nodes with pg_is_in_recovery() = false
    writable: list[str]  # ... that also committed the probe row


class SplitBrainResult(BaseModel):
    detected: bool
    rounds: list[SplitBrainRound]  # rounds with two or more writable primaries
    multi_primary_rounds: list[SplitBrainRound]  # two primaries seen, but only one writable


class WriteOutage(BaseModel):
    """The window around the fault in which no write was acked.

    The outage starts with the first write sent after the fault that was not acked. A
    write sent after the fault was requested but before it landed can still be acked by
    the dying primary; that is not recovery.
    """

    first_failure_at: float | None  # sent_at of the first non-acked write after the fault
    last_ack_before_fault: float | None  # done_at of the last ack before the outage began
    first_ack_after_fault: float | None  # done_at of the first ack after the outage began
    rto_write_sec: float | None  # fault -> first ack after the outage; 0 if no write failed
    gap_sec: float | None  # last ack before -> first ack after (what a client feels)
    failed_during_outage: int
    unknown_during_outage: int
    # Writes that reached the new primary before PostgreSQL finished the promotion. They
    # fail with "cannot execute INSERT in a read-only transaction".
    readonly_failures: int = 0
    readonly_window_sec: float | None = None  # first such failure -> first ack after


class FinalTopology(BaseModel):
    leader: str | None
    replicas: list[str]
    timeline: int | None
    same_timeline: bool
    row_counts: dict[str, int | None]
    same_row_count: bool
    healthy: bool
    problems: list[str]


class Metrics(BaseModel):
    old_leader: str | None
    new_leader: str | None
    detection_sec: float | None
    write_outage: WriteOutage
    rto_read_sec: float | None
    rpo: RpoResult
    split_brain: SplitBrainResult
    rejoin_sec: float | None
    diverged_rows_max: int  # upper bound: rows the old primary may have had that are gone
    timeline_before: int | None
    timeline_after: int | None
    ledger: LedgerSummary


# --------------------------------------------------------------------------------------
# Ledger-based metrics
# --------------------------------------------------------------------------------------


def summarize_ledger(entries: Sequence[LedgerEntry]) -> LedgerSummary:
    counts = {r: 0 for r in WriteResult}
    for e in entries:
        counts[e.result] += 1
    ids = [e.id for e in entries]
    return LedgerSummary(
        attempts=len(entries),
        acked=counts[WriteResult.ACKED],
        failed=counts[WriteResult.FAILED],
        unknown=counts[WriteResult.UNKNOWN],
        first_id=min(ids) if ids else None,
        last_id=max(ids) if ids else None,
    )


READONLY_ERROR = "read-only transaction"


def write_outage(entries: Sequence[LedgerEntry], fault_at: float) -> WriteOutage:
    """RTO for writes: from the fault to the first ack after writes started failing."""
    ordered = sorted(entries, key=lambda e: e.sent_at)
    first_failure = next(
        (e.sent_at for e in ordered if e.sent_at >= fault_at and e.result is not WriteResult.ACKED),
        None,
    )
    if first_failure is None:
        # Nothing failed after the fault: no outage from the client's point of view.
        last_before = max(
            (e.done_at for e in ordered if e.result is WriteResult.ACKED and e.sent_at < fault_at),
            default=None,
        )
        return WriteOutage(
            first_failure_at=None,
            last_ack_before_fault=last_before,
            first_ack_after_fault=None,
            rto_write_sec=0.0,
            gap_sec=0.0,
            failed_during_outage=0,
            unknown_during_outage=0,
        )

    acked = [e for e in ordered if e.result is WriteResult.ACKED]
    last_before = max((e.done_at for e in acked if e.sent_at < first_failure), default=None)
    first_after = min((e.done_at for e in acked if e.sent_at > first_failure), default=None)
    window_end = first_after if first_after is not None else float("inf")
    in_window = [e for e in ordered if first_failure <= e.sent_at < window_end]
    readonly = [e for e in in_window if e.error and READONLY_ERROR in e.error]
    return WriteOutage(
        first_failure_at=first_failure,
        last_ack_before_fault=last_before,
        first_ack_after_fault=first_after,
        rto_write_sec=None if first_after is None else first_after - fault_at,
        gap_sec=None if first_after is None or last_before is None else first_after - last_before,
        failed_during_outage=sum(e.result is WriteResult.FAILED for e in in_window),
        unknown_during_outage=sum(e.result is WriteResult.UNKNOWN for e in in_window),
        readonly_failures=len(readonly),
        readonly_window_sec=(
            first_after - readonly[0].sent_at if readonly and first_after is not None else None
        ),
    )


def rpo(entries: Sequence[LedgerEntry], present_ids: set[int]) -> RpoResult:
    """Compare what the client was told with what the final primary has."""
    acked = sorted(e.id for e in entries if e.result is WriteResult.ACKED)
    unknown = sorted(e.id for e in entries if e.result is WriteResult.UNKNOWN)
    return RpoResult(
        lost_acked=[i for i in acked if i not in present_ids],
        unknown=unknown,
        unknown_present=[i for i in unknown if i in present_ids],
        unknown_missing=[i for i in unknown if i not in present_ids],
    )


def diverged_rows_max(result: RpoResult) -> int:
    """Rows the old primary may have committed that the new timeline does not have.

    Lost acked commits were certainly on the old primary. Unknown-and-missing commits may
    have been. The sum is an upper bound on the rows ``pg_rewind`` threw away.
    """
    return len(result.lost_acked) + len(result.unknown_missing)


# --------------------------------------------------------------------------------------
# Reader-based metrics
# --------------------------------------------------------------------------------------


def rto_read(samples: Sequence[ReadSample], run_end: float | None = None) -> float | None:
    """Longest gap between two successful reads. ``None`` if there was never a good read.

    If ``run_end`` is given and reads were still failing at the end, the gap from the last
    good read to ``run_end`` counts too.
    """
    ok_times = sorted(s.done_at for s in samples if s.ok)
    if not ok_times:
        return None
    gaps = [b - a for a, b in pairwise(ok_times)]
    if run_end is not None and run_end > ok_times[-1]:
        gaps.append(run_end - ok_times[-1])
    return max(gaps, default=0.0)


# --------------------------------------------------------------------------------------
# Poller-based metrics
# --------------------------------------------------------------------------------------


def detection_time(
    rounds: Sequence[PollRound], fault_at: float, old_leader: str | None
) -> tuple[float | None, str | None]:
    """Fault -> first round where Patroni reports a leader other than the old one."""
    for r in rounds:
        if r.t >= fault_at and r.leader is not None and r.leader != old_leader:
            return r.t - fault_at, r.leader
    return None, None


def leader_lost_time(
    rounds: Sequence[PollRound], fault_at: float, old_leader: str | None
) -> float | None:
    """Fault -> first round where Patroni no longer reports the old leader."""
    for r in rounds:
        if r.t >= fault_at and r.leader != old_leader:
            return r.t - fault_at
    return None


def split_brain(rounds: Sequence[PollRound]) -> SplitBrainResult:
    """Two nodes that both commit a probe row in the same round."""
    hits: list[SplitBrainRound] = []
    near: list[SplitBrainRound] = []
    for r in rounds:
        primaries, writable = r.primaries, r.writable
        if len(writable) >= 2:
            hits.append(
                SplitBrainRound(round=r.round, t=r.t, primaries=primaries, writable=writable)
            )
        elif len(primaries) >= 2:
            near.append(
                SplitBrainRound(round=r.round, t=r.t, primaries=primaries, writable=writable)
            )
    return SplitBrainResult(detected=bool(hits), rounds=hits, multi_primary_rounds=near)


def rejoin_time(
    rounds: Sequence[PollRound], started_at: float, node: str, max_lag_bytes: int = 0
) -> float | None:
    """Node start -> first round where Patroni shows it streaming with lag under the limit."""
    for r in rounds:
        if r.t < started_at:
            continue
        m = r.members.get(node)
        if m is None:
            continue
        lag = m.get("lag")
        if m.get("state") == "streaming" and isinstance(lag, int) and lag <= max_lag_bytes:
            return r.t - started_at
    return None


def timeline_at(rounds: Sequence[PollRound], t: float, node: str | None) -> int | None:
    """Control-file timeline of ``node`` in the last round at or before ``t``."""
    if node is None:
        return None
    for r in reversed(rounds):
        if r.t <= t:
            for n in r.nodes:
                if n.node == node and n.timeline is not None:
                    return n.timeline
    return None


def final_topology(
    leader: str | None,
    replicas: list[str],
    timelines: dict[str, int | None],
    row_counts: dict[str, int | None],
    problems: list[str],
) -> FinalTopology:
    tls = {t for t in timelines.values() if t is not None}
    counts = {c for c in row_counts.values() if c is not None}
    all_timelines_known = None not in timelines.values()
    all_counts_known = None not in row_counts.values()
    return FinalTopology(
        leader=leader,
        replicas=replicas,
        timeline=next(iter(tls)) if len(tls) == 1 else None,
        same_timeline=len(tls) == 1 and all_timelines_known,
        row_counts=row_counts,
        same_row_count=len(counts) == 1 and all_counts_known,
        healthy=not problems,
        problems=problems,
    )
