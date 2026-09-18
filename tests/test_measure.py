"""Unit tests for the metric math, with hand-made ledgers and poller rounds. No cluster."""

from __future__ import annotations

from leaderfall import measure
from leaderfall.workload import LedgerEntry, NodeSample, PollRound, ReadSample, WriteResult

# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def entry(
    id_: int,
    sent_at: float,
    result: WriteResult = WriteResult.ACKED,
    latency: float = 0.005,
    server: str | None = "pg-1",
) -> LedgerEntry:
    return LedgerEntry(
        id=id_, sent_at=sent_at, done_at=sent_at + latency, result=result, server=server
    )


def steady(start: float, end: float, first_id: int, rate: float = 50.0) -> list[LedgerEntry]:
    """Acked writes at ``rate`` per second from ``start`` to ``end``."""
    period = 1.0 / rate
    out: list[LedgerEntry] = []
    t, i = start, first_id
    while t < end:
        out.append(entry(i, t))
        t, i = t + period, i + 1
    return out


def outage_ledger(
    fault_at: float = 15.0,
    outage_sec: float = 29.0,
    unknown_at_fault: int = 0,
    acked_after_request: int = 0,
) -> list[LedgerEntry]:
    """Steady writes, then an outage, then steady writes again.

    ``acked_after_request`` writes are sent *after* the fault was requested but still
    acked, because the signal had not landed yet.
    """
    before = steady(0.0, fault_at, first_id=1)
    next_id = before[-1].id + 1
    t = fault_at
    skew: list[LedgerEntry] = []
    for _ in range(acked_after_request):
        skew.append(entry(next_id, t))
        next_id, t = next_id + 1, t + 0.02
    unknown = [
        entry(next_id + k, t + 0.02 * k, WriteResult.UNKNOWN, latency=0.5)
        for k in range(unknown_at_fault)
    ]
    next_id += unknown_at_fault
    t += 0.02 * unknown_at_fault
    failed: list[LedgerEntry] = []
    while t < fault_at + outage_sec:
        failed.append(entry(next_id, t, WriteResult.FAILED, latency=0.0, server=None))
        next_id, t = next_id + 1, t + 0.02
    after = steady(fault_at + outage_sec, fault_at + outage_sec + 10, first_id=next_id)
    return before + skew + unknown + failed + after


def poll_round(
    n: int,
    t: float,
    leader: str | None,
    primaries: dict[str, bool],
    streaming: dict[str, int] | None = None,
) -> PollRound:
    """``primaries`` maps node -> probe write ok; every other node is a replica."""
    nodes = []
    for name in ("pg-1", "pg-2", "pg-3"):
        if name in primaries:
            nodes.append(
                NodeSample(node=name, in_recovery=False, timeline=2, write_ok=primaries[name])
            )
        else:
            nodes.append(NodeSample(node=name, in_recovery=True, timeline=2))
    members = {
        name: {
            "role": "leader" if name == leader else "replica",
            "state": "running",
            "lag": None,
            "timeline": 2,
        }
        for name in ("pg-1", "pg-2", "pg-3")
    }
    for name, lag in (streaming or {}).items():
        members[name] = {"role": "replica", "state": "streaming", "lag": lag, "timeline": 2}
    return PollRound(round=n, t=t, leader=leader, members=members, nodes=nodes)


# --------------------------------------------------------------------------------------
# Ledger summary and RPO
# --------------------------------------------------------------------------------------


class TestLedgerSummary:
    def test_counts(self) -> None:
        ledger = outage_ledger(unknown_at_fault=2)
        s = measure.summarize_ledger(ledger)
        assert s.attempts == len(ledger)
        assert s.unknown == 2
        assert s.acked + s.failed + s.unknown == s.attempts
        assert (s.first_id, s.last_id) == (1, ledger[-1].id)

    def test_empty(self) -> None:
        s = measure.summarize_ledger([])
        assert s.attempts == 0
        assert s.first_id is None


class TestRpo:
    def test_no_loss(self) -> None:
        ledger = outage_ledger()
        present = {e.id for e in ledger if e.result is WriteResult.ACKED}
        r = measure.rpo(ledger, present)
        assert r.lost_acked == []
        assert r.lost_acked_commits == 0
        assert r.unknown == []

    def test_lost_acked_commits_are_the_ones_missing(self) -> None:
        ledger = outage_ledger()
        acked = sorted(e.id for e in ledger if e.result is WriteResult.ACKED)
        lost = acked[-15:-12]  # three commits acked just before the fault, gone after failover
        present = set(acked) - set(lost)
        r = measure.rpo(ledger, present)
        assert r.lost_acked == lost
        assert r.lost_acked_commits == 3

    def test_unknown_split_into_present_and_missing(self) -> None:
        ledger = outage_ledger(unknown_at_fault=3)
        unknown = [e.id for e in ledger if e.result is WriteResult.UNKNOWN]
        present = {e.id for e in ledger if e.result is WriteResult.ACKED} | {unknown[0]}
        r = measure.rpo(ledger, present)
        assert r.unknown == unknown
        assert r.unknown_present == [unknown[0]]
        assert r.unknown_missing == unknown[1:]
        assert r.lost_acked == []  # unknown commits are never counted as lost
        assert measure.diverged_rows_max(r) == 2

    def test_failed_writes_never_count(self) -> None:
        ledger = outage_ledger()
        present = {e.id for e in ledger if e.result is WriteResult.ACKED}
        r = measure.rpo(ledger, present)  # failed ids are absent from the table, and that is fine
        assert r.lost_acked == []


# --------------------------------------------------------------------------------------
# RTO write
# --------------------------------------------------------------------------------------


class TestWriteOutage:
    def test_plain_outage(self) -> None:
        o = measure.write_outage(outage_ledger(fault_at=15.0, outage_sec=29.0), fault_at=15.0)
        assert o.first_failure_at == 15.0
        assert o.rto_write_sec is not None
        assert 29.0 <= o.rto_write_sec <= 29.02
        assert o.gap_sec is not None
        assert o.gap_sec > 29.0
        assert o.failed_during_outage == 29.0 / 0.02
        assert o.unknown_during_outage == 0

    def test_ack_after_request_but_before_signal_is_not_recovery(self) -> None:
        """The regression that gave RTO 0.0 s: the dying primary acked 2 more writes."""
        ledger = outage_ledger(fault_at=15.0, outage_sec=29.0, acked_after_request=2)
        o = measure.write_outage(ledger, fault_at=15.0)
        assert o.first_failure_at is not None
        assert o.first_failure_at > 15.0
        assert o.rto_write_sec is not None
        assert o.rto_write_sec > 29.0
        assert o.last_ack_before_fault is not None
        assert o.last_ack_before_fault > 15.0  # the skewed acks count as "before"

    def test_unknown_commits_sit_in_the_outage_window(self) -> None:
        ledger = outage_ledger(unknown_at_fault=2)
        o = measure.write_outage(ledger, fault_at=15.0)
        assert o.unknown_during_outage == 2
        assert o.first_failure_at == 15.0  # the first unknown starts the outage

    def test_readonly_window(self) -> None:
        """Writes routed to the new primary before PostgreSQL finished promoting."""
        ledger = outage_ledger(fault_at=15.0, outage_sec=29.0)
        for e in ledger:
            if e.result is WriteResult.FAILED and e.sent_at >= 40.0:
                e.error = "insert: ReadOnlySqlTransaction: cannot execute INSERT in a read-only transaction"
        o = measure.write_outage(ledger, fault_at=15.0)
        assert o.readonly_failures == 4.0 / 0.02
        assert o.readonly_window_sec is not None
        assert 3.96 <= o.readonly_window_sec <= 4.0

    def test_no_readonly_window_when_no_such_error(self) -> None:
        o = measure.write_outage(outage_ledger(), fault_at=15.0)
        assert o.readonly_failures == 0
        assert o.readonly_window_sec is None

    def test_ack_gap_catches_a_stall_without_errors(self) -> None:
        ledger = steady(0, 10, 1) + steady(14.8, 20, 10_000)  # a 4.8 s stall, no failures
        o = measure.write_outage(ledger, fault_at=10.0)
        assert o.rto_write_sec == 0.0
        assert o.failed_during_outage == 0
        assert o.ack_gap_sec is not None
        assert 4.8 <= o.ack_gap_sec <= 4.83

    def test_max_write_latency(self) -> None:
        ledger = steady(0, 1, 1)
        ledger[10] = entry(11, ledger[10].sent_at, latency=2.5)
        o = measure.write_outage(ledger, fault_at=5.0)
        assert o.max_write_latency_sec == 2.5

    def test_no_failure_means_zero_rto(self) -> None:
        o = measure.write_outage(steady(0, 60, 1), fault_at=30.0)
        assert o.rto_write_sec == 0.0
        assert o.gap_sec == 0.0
        assert o.first_ack_after_fault is None
        assert o.failed_during_outage == 0

    def test_never_recovered(self) -> None:
        ledger = steady(0, 15, 1) + [
            entry(10_000 + k, 15 + 0.02 * k, WriteResult.FAILED, latency=0.0) for k in range(100)
        ]
        o = measure.write_outage(ledger, fault_at=15.0)
        assert o.rto_write_sec is None
        assert o.first_ack_after_fault is None
        assert o.failed_during_outage == 100


# --------------------------------------------------------------------------------------
# RTO read
# --------------------------------------------------------------------------------------


class TestRtoRead:
    def test_longest_gap(self) -> None:
        samples = [ReadSample(sent_at=t, done_at=t + 0.002, ok=True) for t in (0.0, 0.1, 0.2)]
        samples += [ReadSample(sent_at=t, done_at=t + 0.001, ok=False) for t in (0.3, 0.4, 0.5)]
        samples += [ReadSample(sent_at=t, done_at=t + 0.002, ok=True) for t in (2.7, 2.8)]
        assert measure.rto_read(samples) == 2.5

    def test_tail_failure_counts_when_run_end_given(self) -> None:
        samples = [ReadSample(sent_at=t, done_at=t, ok=True) for t in (0.0, 1.0)]
        assert measure.rto_read(samples) == 1.0
        assert measure.rto_read(samples, run_end=5.0) == 4.0

    def test_no_good_read(self) -> None:
        assert measure.rto_read([ReadSample(sent_at=0, done_at=0, ok=False)]) is None


# --------------------------------------------------------------------------------------
# Poller-based: detection, split brain, rejoin
# --------------------------------------------------------------------------------------


class TestDetection:
    def test_first_round_with_a_different_leader(self) -> None:
        rounds = [
            poll_round(1, 14.5, "pg-1", {"pg-1": True}),
            poll_round(2, 15.5, "pg-1", {}),  # still reported, but dead
            poll_round(3, 30.0, None, {}),
            poll_round(4, 41.5, "pg-2", {"pg-2": True}),
            poll_round(5, 42.0, "pg-2", {"pg-2": True}),
        ]
        assert measure.detection_time(rounds, fault_at=15.0, old_leader="pg-1") == (26.5, "pg-2")
        assert measure.leader_lost_time(rounds, fault_at=15.0, old_leader="pg-1") == 15.0

    def test_not_detected(self) -> None:
        rounds = [poll_round(1, 20.0, "pg-1", {})]
        assert measure.detection_time(rounds, 15.0, "pg-1") == (None, None)


class TestSplitBrain:
    def test_clean_failover(self) -> None:
        rounds = [
            poll_round(1, 1.0, "pg-1", {"pg-1": True}),
            poll_round(2, 40.0, "pg-2", {"pg-2": True}),
        ]
        r = measure.split_brain(rounds)
        assert not r.detected
        assert r.rounds == []
        assert r.multi_primary_rounds == []

    def test_two_writable_primaries(self) -> None:
        rounds = [poll_round(7, 33.0, "pg-2", {"pg-1": True, "pg-2": True})]
        r = measure.split_brain(rounds)
        assert r.detected
        assert r.rounds[0].round == 7
        assert sorted(r.rounds[0].writable) == ["pg-1", "pg-2"]

    def test_two_primaries_one_writable_is_reported_not_failed(self) -> None:
        """An old primary that still says in_recovery=false but rejects the probe write."""
        rounds = [poll_round(7, 33.0, "pg-2", {"pg-1": False, "pg-2": True})]
        r = measure.split_brain(rounds)
        assert not r.detected
        assert len(r.multi_primary_rounds) == 1
        assert r.multi_primary_rounds[0].writable == ["pg-2"]


class TestRejoin:
    def test_first_streaming_round_after_start(self) -> None:
        rounds = [
            poll_round(1, 58.0, "pg-2", {"pg-2": True}),
            poll_round(2, 60.0, "pg-2", {"pg-2": True}, streaming={"pg-1": 4096}),
            poll_round(3, 60.5, "pg-2", {"pg-2": True}, streaming={"pg-1": 0}),
        ]
        assert measure.rejoin_time(rounds, started_at=59.0, node="pg-1") == 1.5
        assert measure.rejoin_time(rounds, started_at=59.0, node="pg-1", max_lag_bytes=4096) == 1.0
        assert measure.rejoin_time(rounds, started_at=61.0, node="pg-1") is None


class TestTimelineAndFinal:
    def test_timeline_at(self) -> None:
        rounds = [
            poll_round(1, 10.0, "pg-1", {"pg-1": True}),
            poll_round(2, 20.0, "pg-1", {"pg-1": True}),
        ]
        rounds[0].nodes[0].timeline = 1
        assert measure.timeline_at(rounds, 15.0, "pg-1") == 1
        assert measure.timeline_at(rounds, 25.0, "pg-1") == 2
        assert measure.timeline_at(rounds, 5.0, "pg-1") is None
        assert measure.timeline_at(rounds, 25.0, None) is None

    def test_final_topology(self) -> None:
        f = measure.final_topology(
            leader="pg-2",
            replicas=["pg-1", "pg-3"],
            timelines={"pg-1": 2, "pg-2": 2, "pg-3": 2},
            row_counts={"pg-1": 100, "pg-2": 100, "pg-3": 100},
            problems=[],
        )
        assert f.healthy
        assert f.same_timeline
        assert f.same_row_count
        assert f.timeline == 2

    def test_final_topology_mismatch(self) -> None:
        f = measure.final_topology(
            leader="pg-2",
            replicas=["pg-1"],
            timelines={"pg-1": 1, "pg-2": 2, "pg-3": None},
            row_counts={"pg-1": 99, "pg-2": 100, "pg-3": None},
            problems=["pg-3 is replica/starting, not a streaming replica"],
        )
        assert not f.healthy
        assert not f.same_timeline
        assert not f.same_row_count
        assert f.timeline is None
