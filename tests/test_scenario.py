"""Unit tests for the scenario model, SLO evaluation and target resolution. No cluster."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from leaderfall import measure, slo
from leaderfall.cluster import ClusterState, PatroniProfile, SyncMode
from leaderfall.faults import FaultError, Target, TargetKind, resolve_target
from leaderfall.scenario import ScenarioSpec, Step, find_scenario, load_all, rewind_info
from leaderfall.workload import NodeSample, PollRound

REPO = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------------------
# Scenario files and model
# --------------------------------------------------------------------------------------


class TestScenarioFiles:
    def test_every_shipped_scenario_parses(self) -> None:
        specs = load_all(REPO / "scenarios", tag="all")
        assert {s.name for s in specs} >= {
            "primary-sigkill",
            "primary-clean-stop",
            "primary-partition",
            "replica-loss",
            "planned-switchover",
            "haproxy-restart",
        }
        for spec in specs:
            assert "core" in spec.tags or "advanced" in spec.tags, spec.name

    def test_core_tag_filter(self) -> None:
        core = load_all(REPO / "scenarios", tag="core")
        assert core
        assert all("core" in s.tags for s in core)
        assert load_all(REPO / "scenarios", tag="no-such-tag") == []

    def test_find_scenario_unknown(self) -> None:
        with pytest.raises(FileNotFoundError, match="available"):
            find_scenario("does-not-exist", REPO / "scenarios")

    def test_shipped_slo_file_parses(self) -> None:
        cfg = slo.SloConfig.load(REPO / "slo.yml")
        assert set(cfg.sync) == {"off", "on", "strict"}  # YAML on/off keys normalised
        assert cfg.profiles["default"].rto_write_sec_max == 45
        assert cfg.profiles["fast"].rto_write_sec_max == 25


def minimal(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "x",
        "description": "d",
        "steps": [{"fault": "kill", "target": "primary"}],
    }
    data.update(overrides)
    return data


class TestScenarioModel:
    def test_defaults(self) -> None:
        spec = ScenarioSpec.model_validate(minimal())
        assert spec.cluster.patroni_profile is PatroniProfile.DEFAULT
        assert spec.cluster.synchronous_mode is SyncMode.OFF
        assert spec.workload.rate_per_sec == 50
        assert spec.expect.model_fields_set == set()

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (False, SyncMode.OFF),
            (True, SyncMode.ON),
            ("on", SyncMode.ON),
            ("strict", SyncMode.STRICT),
        ],
    )
    def test_synchronous_mode_accepts_bool_and_names(
        self, value: object, expected: SyncMode
    ) -> None:
        spec = ScenarioSpec.model_validate(minimal(cluster={"synchronous_mode": value}))
        assert spec.cluster.synchronous_mode is expected

    def test_name_must_match_file(self, tmp_path: Path) -> None:
        path = tmp_path / "other.yml"
        path.write_text(yaml.safe_dump(minimal()), encoding="utf-8")
        with pytest.raises(ValueError, match="does not match"):
            ScenarioSpec.load(path)

    def test_unknown_keys_rejected(self) -> None:
        with pytest.raises(ValueError, match="extra"):
            ScenarioSpec.model_validate(minimal(bogus=1))

    def test_step_needs_exactly_one_kind(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Step.model_validate({"fault": "kill", "wait_for": "new_leader", "target": "primary"})
        with pytest.raises(ValueError, match="exactly one"):
            Step.model_validate({})

    def test_step_unknown_names(self) -> None:
        with pytest.raises(ValueError, match="unknown fault"):
            Step.model_validate({"fault": "explode", "target": "primary"})
        with pytest.raises(ValueError, match="unknown wait_for"):
            Step.model_validate({"wait_for": "rain"})
        with pytest.raises(ValueError, match="needs a target"):
            Step.model_validate({"action": "start"})

    def test_step_kinds_and_labels(self) -> None:
        assert Step.model_validate({"hold_sec": 5}).kind == "hold"
        assert Step.model_validate({"hold_sec": 5}).label == "hold 5s"
        assert Step.model_validate({"wait_for": "writes_ok"}).kind == "wait_for"
        assert Step.model_validate({"action": "heal", "target": "failed_node"}).kind == "action"

    def test_expect_null_is_recorded_as_set(self) -> None:
        spec = ScenarioSpec.model_validate(minimal(expect={"lost_acked_commits_max": None}))
        assert "lost_acked_commits_max" in spec.expect.model_fields_set


# --------------------------------------------------------------------------------------
# SLO layering and evaluation
# --------------------------------------------------------------------------------------


def slo_config() -> slo.SloConfig:
    return slo.SloConfig.model_validate(
        {
            "defaults": {
                "split_brain": False,
                "lost_acked_commits_max": None,
                "rejoin_sec_max": 60,
            },
            "profiles": {"default": {"rto_write_sec_max": 45}, "fast": {"rto_write_sec_max": 25}},
            "sync": {"on": {"lost_acked_commits_max": 0}},
        }
    )


class TestLimits:
    def test_layering(self) -> None:
        cfg = slo_config()
        limits = cfg.limits_for("fast", "on")
        assert limits.rto_write_sec_max == 25
        assert limits.lost_acked_commits_max == 0
        assert limits.split_brain is False

    def test_scenario_null_disables_a_check(self) -> None:
        cfg = slo_config()
        expect = slo.Limits.model_validate({"rto_write_sec_max": None})
        limits = cfg.limits_for("default", "off", expect)
        assert limits.rto_write_sec_max is None
        assert limits.rejoin_sec_max == 60  # untouched

    def test_scenario_overrides_value(self) -> None:
        cfg = slo_config()
        expect = slo.Limits.model_validate({"rto_write_sec_max": 5, "failover": False})
        limits = cfg.limits_for("default", "off", expect)
        assert limits.rto_write_sec_max == 5
        assert limits.failover is False

    def test_yaml_bool_keys_in_sync(self) -> None:
        cfg = slo.SloConfig.model_validate(
            {"sync": {True: {"lost_acked_commits_max": 0}, False: {}}}
        )
        assert set(cfg.sync) == {"on", "off"}


def metrics(**overrides: Any) -> measure.Metrics:
    base: dict[str, Any] = {
        "failed_node": "pg-1",
        "recovery_action": "start",
        "old_leader": "pg-1",
        "new_leader": "pg-2",
        "detection_sec": 27.0,
        "demotion_expected": False,
        "demotion_sec": None,
        "write_outage": measure.WriteOutage(
            first_failure_at=15.0,
            last_ack_before_fault=14.9,
            first_ack_after_fault=45.0,
            rto_write_sec=30.0,
            gap_sec=30.1,
            failed_during_outage=1400,
            unknown_during_outage=0,
        ),
        "rto_read_sec": 0.2,
        "rpo": measure.RpoResult(lost_acked=[], unknown=[], unknown_present=[], unknown_missing=[]),
        "split_brain": measure.SplitBrainResult(detected=False, rounds=[], multi_primary_rounds=[]),
        "rejoin_sec": 5.0,
        "diverged_rows_max": 0,
        "timeline_before": 1,
        "timeline_after": 2,
        "ledger": measure.LedgerSummary(attempts=3000, acked=1500, failed=1500, unknown=0),
    }
    base.update(overrides)
    return measure.Metrics(**base)


def healthy_final() -> measure.FinalTopology:
    return measure.final_topology(
        "pg-2",
        ["pg-1", "pg-3"],
        {"pg-1": 2, "pg-2": 2, "pg-3": 2},
        {"pg-1": 1, "pg-2": 1, "pg-3": 1},
        [],
    )


def by_name(checks: list[slo.Check]) -> dict[str, slo.Check]:
    return {c.name: c for c in checks}


class TestEvaluate:
    def test_all_pass(self) -> None:
        limits = slo.Limits(
            failover=True,
            detection_sec_max=40,
            rto_write_sec_max=45,
            rejoin_sec_max=60,
            split_brain=False,
            final_topology="1-leader-2-replicas",
        )
        checks = slo.evaluate(metrics(), healthy_final(), limits)
        assert {c.name for c in checks} == {
            "failover", "detection_sec", "rto_write_sec", "rejoin_sec", "split_brain", "final_topology"
        }  # fmt: skip
        assert slo.passed(checks)

    def test_unset_limits_produce_no_checks(self) -> None:
        assert slo.evaluate(metrics(), healthy_final(), slo.Limits()) == []

    def test_rto_over_limit_fails(self) -> None:
        checks = by_name(slo.evaluate(metrics(), healthy_final(), slo.Limits(rto_write_sec_max=20)))
        assert not checks["rto_write_sec"].passed
        assert checks["rto_write_sec"].actual == 30.0

    def test_never_recovered_fails_with_note(self) -> None:
        m = metrics()
        m.write_outage.rto_write_sec = None
        checks = by_name(slo.evaluate(m, healthy_final(), slo.Limits(rto_write_sec_max=45)))
        assert not checks["rto_write_sec"].passed
        assert checks["rto_write_sec"].note == "writes never recovered"

    def test_unexpected_failover_fails(self) -> None:
        checks = by_name(slo.evaluate(metrics(), healthy_final(), slo.Limits(failover=False)))
        assert not checks["failover"].passed

    def test_no_failover_skips_detection_when_none_expected(self) -> None:
        m = metrics(new_leader=None, detection_sec=None)
        limits = slo.Limits(failover=False, detection_sec_max=40)
        checks = by_name(slo.evaluate(m, healthy_final(), limits))
        assert checks["failover"].passed
        assert "detection_sec" not in checks

    def test_lost_commits(self) -> None:
        m = metrics(
            rpo=measure.RpoResult(
                lost_acked=[7, 8], unknown=[], unknown_present=[], unknown_missing=[]
            )
        )
        checks = by_name(slo.evaluate(m, healthy_final(), slo.Limits(lost_acked_commits_max=0)))
        assert not checks["lost_acked_commits"].passed
        assert checks["lost_acked_commits"].actual == 2
        assert slo.evaluate(m, healthy_final(), slo.Limits(lost_acked_commits_max=None)) == []

    def test_split_brain_fails(self) -> None:
        sb = measure.SplitBrainResult(
            detected=True,
            rounds=[
                measure.SplitBrainRound(
                    round=3, t=30.0, primaries=["pg-1", "pg-2"], writable=["pg-1", "pg-2"]
                )
            ],
            multi_primary_rounds=[],
        )
        checks = by_name(
            slo.evaluate(metrics(split_brain=sb), healthy_final(), slo.Limits(split_brain=False))
        )
        assert not checks["split_brain"].passed

    def test_rejoin_only_checked_when_a_recovery_action_happened(self) -> None:
        m = metrics(recovery_action=None, rejoin_sec=None)
        assert slo.evaluate(m, healthy_final(), slo.Limits(rejoin_sec_max=60)) == []
        m = metrics(rejoin_sec=None)  # start happened, never came back
        checks = by_name(slo.evaluate(m, healthy_final(), slo.Limits(rejoin_sec_max=60)))
        assert not checks["rejoin_sec"].passed

    def test_demotion_only_checked_when_expected(self) -> None:
        assert slo.evaluate(metrics(), healthy_final(), slo.Limits(demotion_sec_max=40)) == []
        m = metrics(demotion_expected=True, demotion_sec=12.0)
        assert by_name(slo.evaluate(m, healthy_final(), slo.Limits(demotion_sec_max=40)))[
            "demotion_sec"
        ].passed
        m = metrics(demotion_expected=True, demotion_sec=None)
        assert not by_name(slo.evaluate(m, healthy_final(), slo.Limits(demotion_sec_max=40)))[
            "demotion_sec"
        ].passed

    def test_final_topology(self) -> None:
        bad = measure.final_topology(
            "pg-2", ["pg-3"], {"pg-1": None, "pg-2": 2, "pg-3": 2}, {}, ["pg-1 missing"]
        )
        checks = by_name(
            slo.evaluate(metrics(), bad, slo.Limits(final_topology="1-leader-2-replicas"))
        )
        assert not checks["final_topology"].passed
        assert checks["final_topology"].actual == "pg-1 missing"
        with pytest.raises(ValueError, match="unsupported"):
            slo.evaluate(metrics(), healthy_final(), slo.Limits(final_topology="anything"))


# --------------------------------------------------------------------------------------
# Target resolution
# --------------------------------------------------------------------------------------


def state(leader: str = "pg-2", sync: str | None = None) -> ClusterState:
    members = []
    for name in ("pg-1", "pg-2", "pg-3"):
        if name == leader:
            members.append({"name": name, "role": "leader", "state": "running", "timeline": 1})
        else:
            role = "sync_standby" if name == sync else "replica"
            members.append(
                {"name": name, "role": role, "state": "streaming", "timeline": 1, "lag": 0}
            )
    return ClusterState.model_validate({"members": members})


class TestResolveTarget:
    def test_primary(self) -> None:
        t = resolve_target("primary", state("pg-2"))
        assert t == Target("leaderfall-pg-2", TargetKind.PG, node="pg-2")
        assert t.label == "pg-2"

    def test_any_replica_is_deterministic(self) -> None:
        assert resolve_target("any_replica", state("pg-2")).node == "pg-1"
        assert resolve_target("any_replica", state("pg-1")).node == "pg-2"

    def test_sync_replica(self) -> None:
        assert resolve_target("sync_replica", state("pg-1", sync="pg-3")).node == "pg-3"
        with pytest.raises(FaultError, match="sync_standby"):
            resolve_target("sync_replica", state("pg-1"))

    def test_failed_node(self) -> None:
        failed = Target("leaderfall-pg-3", TargetKind.PG, node="pg-3")
        assert resolve_target("failed_node", state(), failed) is failed
        with pytest.raises(FaultError, match="before any fault"):
            resolve_target("failed_node", state())

    def test_services_and_names(self) -> None:
        assert resolve_target("haproxy", state()).kind is TargetKind.HAPROXY
        assert resolve_target("etcd-2", state()).container == "leaderfall-etcd-2"
        assert resolve_target("pg-3", state()).node == "pg-3"
        with pytest.raises(FaultError, match="unknown etcd"):
            resolve_target("etcd-9", state())
        with pytest.raises(FaultError, match="unknown target"):
            resolve_target("moon", state())

    def test_no_leader(self) -> None:
        s = state("pg-2")
        s.members[1].role = "replica"
        with pytest.raises(FaultError, match="no primary"):
            resolve_target("primary", s)


# --------------------------------------------------------------------------------------
# Demotion / fencing metrics and rewind log parsing
# --------------------------------------------------------------------------------------


def rnd(n: int, t: float, writable: list[str]) -> PollRound:
    nodes = [
        NodeSample(node=name, in_recovery=name not in writable, write_ok=(name in writable) or None)
        for name in ("pg-1", "pg-2", "pg-3")
    ]
    return PollRound(round=n, t=t, leader=None, nodes=nodes)


class TestDemotion:
    def test_fenced_after_a_while(self) -> None:
        rounds = [
            rnd(1, 14.5, ["pg-1"]),
            rnd(2, 15.5, ["pg-1"]),
            rnd(3, 27.5, ["pg-1"]),
            rnd(4, 28.0, []),
            rnd(5, 28.5, ["pg-2"]),
        ]
        assert measure.demotion_time(rounds, 15.0, "pg-1") == (13.0, 12.5)

    def test_fenced_at_once(self) -> None:
        rounds = [rnd(1, 15.5, []), rnd(2, 16.0, [])]
        assert measure.demotion_time(rounds, 15.0, "pg-1") == (0.0, None)

    def test_still_writable_at_end_of_window(self) -> None:
        rounds = [rnd(1, 15.5, ["pg-1"]), rnd(2, 16.0, ["pg-1"])]
        assert measure.demotion_time(rounds, 15.0, "pg-1") == (None, 1.0)
        assert measure.demotion_time(rounds, 15.0, "pg-1", until=15.8) == (None, 0.5)
        assert measure.demotion_time([], 15.0, "pg-1") == (None, None)
        assert measure.demotion_time(rounds, 15.0, None) == (None, None)

    def test_node_fenced(self) -> None:
        rounds = [rnd(1, 1.0, ["pg-1"]), rnd(2, 1.5, []), rnd(3, 2.0, [])]
        assert measure.node_fenced(rounds, "pg-1")
        assert not measure.node_fenced(rounds[:2], "pg-1")
        assert not measure.node_fenced(rounds[:1], "pg-1")


class TestRewindInfo:
    def test_parses_last_rewind(self) -> None:
        logs = (
            "pg_rewind: servers diverged at WAL location 0/3000000 on timeline 1\n"
            "pg_rewind: Done!\n"
            "pg_rewind: servers diverged at WAL location 0/4083808 on timeline 8\npg_rewind: Done!\n"
        )
        assert rewind_info(logs) == {
            "ran": True,
            "done": True,
            "diverged_at_lsn": "0/4083808",
            "diverged_on_timeline": 8,
        }

    def test_no_rewind(self) -> None:
        assert rewind_info("started streaming WAL from primary")["ran"] is False
