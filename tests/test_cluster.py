"""Unit tests for cluster state evaluation and configuration mapping. No Docker needed."""

from typing import Any

import pytest

from leaderfall.cluster import (
    ClusterConfig,
    ClusterState,
    PatroniProfile,
    SyncMode,
    WaitTimeoutError,
    config_diff,
    expected_sync_standbys,
    wait_until,
)


def member(
    name: str, role: str, state: str, timeline: int | None = 1, **extra: Any
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": name,
        "role": role,
        "state": state,
        "api_url": f"http://{name}:8008/patroni",
        "host": name,
        "port": 5432,
    }
    if timeline is not None:
        data["timeline"] = timeline
    data.update(extra)
    return data


def healthy_payload() -> dict[str, Any]:
    """Shape of Patroni 4's GET /cluster on a healthy 3-node cluster."""
    return {
        "scope": "leaderfall",
        "members": [
            member("pg-1", "replica", "streaming", lag=0, receive_lag=0, replay_lag=0),
            member("pg-2", "leader", "running"),
            member("pg-3", "replica", "streaming", lag=0),
        ],
    }


class TestClusterState:
    def test_healthy(self) -> None:
        state = ClusterState.model_validate(healthy_payload())
        assert state.health_problems() == []
        assert state.is_healthy()
        assert state.leader is not None
        assert state.leader.name == "pg-2"
        assert [m.name for m in state.replicas] == ["pg-1", "pg-3"]

    def test_sync_standby_counts_as_replica(self) -> None:
        payload = healthy_payload()
        payload["members"][0]["role"] = "sync_standby"
        assert ClusterState.model_validate(payload).is_healthy()

    def test_no_leader(self) -> None:
        payload = healthy_payload()
        payload["members"][1] = member("pg-2", "replica", "running", lag=0)
        problems = ClusterState.model_validate(payload).health_problems()
        assert "expected 1 leader, found 0" in problems

    def test_two_leaders(self) -> None:
        payload = healthy_payload()
        payload["members"][0] = member("pg-1", "leader", "running")
        problems = ClusterState.model_validate(payload).health_problems()
        assert "expected 1 leader, found 2" in problems

    def test_missing_member(self) -> None:
        payload = healthy_payload()
        del payload["members"][2]
        problems = ClusterState.model_validate(payload).health_problems()
        assert "expected 3 members, found 2" in problems

    def test_replica_not_streaming(self) -> None:
        payload = healthy_payload()
        payload["members"][2] = member("pg-3", "replica", "starting", timeline=None)
        problems = ClusterState.model_validate(payload).health_problems()
        assert "pg-3 is replica/starting, not a streaming replica" in problems
        assert "some members have no timeline yet" in problems

    def test_lag_over_limit(self) -> None:
        payload = healthy_payload()
        payload["members"][2]["lag"] = 4096
        state = ClusterState.model_validate(payload)
        assert state.health_problems() == ["pg-3 lag is 4096, limit 0"]
        assert state.is_healthy(max_lag_bytes=4096)

    def test_unknown_lag_is_a_problem(self) -> None:
        payload = healthy_payload()
        payload["members"][2]["lag"] = "unknown"
        state = ClusterState.model_validate(payload)
        pg3 = state.member("pg-3")
        assert pg3 is not None
        assert pg3.lag_bytes is None
        assert "pg-3 lag is unknown, limit 0" in state.health_problems()

    def test_sync_standby_expected_but_missing(self) -> None:
        state = ClusterState.model_validate(healthy_payload())
        assert state.health_problems(sync_standbys=None) == []
        assert state.health_problems(sync_standbys=1) == ["expected 1 sync standby, found 0"]

    def test_sync_standby_present_when_sync_off(self) -> None:
        payload = healthy_payload()
        payload["members"][0]["role"] = "sync_standby"
        state = ClusterState.model_validate(payload)
        assert state.health_problems(sync_standbys=0) == ["expected 0 sync standby, found 1"]
        assert state.health_problems(sync_standbys=1) == []

    def test_timeline_split(self) -> None:
        payload = healthy_payload()
        payload["members"][0]["timeline"] = 2
        problems = ClusterState.model_validate(payload).health_problems()
        assert "members are on different timelines: [1, 2]" in problems


class TestClusterConfig:
    def test_default_profile_async(self) -> None:
        cfg = ClusterConfig()
        assert cfg.dcs_config() == {
            "ttl": 30,
            "loop_wait": 10,
            "retry_timeout": 10,
            "maximum_lag_on_failover": 1_048_576,
            "synchronous_mode": False,
            "synchronous_mode_strict": False,
        }
        assert cfg.expected_sync_standbys == 0

    def test_fast_profile_strict(self) -> None:
        cfg = ClusterConfig(profile=PatroniProfile.FAST, sync=SyncMode.STRICT)
        dcs = cfg.dcs_config()
        assert (dcs["ttl"], dcs["loop_wait"], dcs["retry_timeout"]) == (20, 5, 5)
        assert dcs["synchronous_mode"] is True
        assert dcs["synchronous_mode_strict"] is True
        assert cfg.expected_sync_standbys == 1

    def test_sync_on_not_strict(self) -> None:
        dcs = ClusterConfig(sync=SyncMode.ON).dcs_config()
        assert dcs["synchronous_mode"] is True
        assert dcs["synchronous_mode_strict"] is False


class TestExpectedSyncStandbys:
    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            ({}, 0),
            ({"synchronous_mode": False}, 0),
            ({"synchronous_mode": "off"}, 0),
            ({"synchronous_mode": True}, 1),
            ({"synchronous_mode": "on"}, 1),
            ({"synchronous_mode": "quorum"}, 1),
            ({"synchronous_mode": True, "synchronous_node_count": 2}, 2),
        ],
    )
    def test_from_dynamic_config(self, config: dict[str, object], expected: int) -> None:
        assert expected_sync_standbys(config) == expected


class TestConfigDiff:
    def test_no_diff_when_equal(self) -> None:
        desired = ClusterConfig().dcs_config()
        assert config_diff(desired, {**desired, "postgresql": {"use_slots": True}}) == {}

    def test_bool_and_string_forms_match(self) -> None:
        desired = {"synchronous_mode": True, "synchronous_mode_strict": False}
        current = {"synchronous_mode": "on", "synchronous_mode_strict": "off"}
        assert config_diff(desired, current) == {}

    def test_reports_only_changed_keys(self) -> None:
        desired = ClusterConfig(profile=PatroniProfile.FAST).dcs_config()
        current = ClusterConfig().dcs_config()
        assert config_diff(desired, current) == {"ttl": 20, "loop_wait": 5, "retry_timeout": 5}

    def test_missing_key_is_a_diff(self) -> None:
        assert config_diff({"ttl": 30}, {}) == {"ttl": 30}


class TestWaitUntil:
    def test_returns_value_and_elapsed(self) -> None:
        calls = 0

        def check() -> tuple[bool, str, str | None]:
            nonlocal calls
            calls += 1
            return calls >= 3, "ready", f"attempt {calls}"

        value, waited = wait_until(check, what="three calls", timeout=5, interval=0.01)
        assert value == "ready"
        assert calls == 3
        assert waited >= 0.02

    def test_timeout_carries_last_detail(self) -> None:
        def check() -> tuple[bool, None, str | None]:
            return False, None, "still waiting"

        with pytest.raises(WaitTimeoutError) as info:
            wait_until(check, what="never", timeout=0.05, interval=0.01)
        assert "waiting for never" in str(info.value)
        assert info.value.last == "still waiting"
