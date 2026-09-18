"""SLO limits per profile and pass/fail evaluation.

Limits come from three layers, later ones winning: ``slo.yml`` defaults, the block for the
Patroni profile, the block for the sync mode, and finally the scenario's own ``expect``
block. A key set to ``null`` in a later layer disables that check ("report only").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from leaderfall.measure import FinalTopology, Metrics

FINAL_TOPOLOGY_HEALTHY = "1-leader-2-replicas"


class Limits(BaseModel):
    """Every check a scenario can be held to. ``None`` means "not checked"."""

    model_config = ConfigDict(extra="forbid")

    failover: bool | None = None  # must a new leader appear (True) or not (False)?
    detection_sec_max: float | None = None
    rto_write_sec_max: float | None = None
    rto_read_sec_max: float | None = None
    rejoin_sec_max: float | None = None
    demotion_sec_max: float | None = None
    lost_acked_commits_max: int | None = None
    split_brain: bool | None = None  # allowed value; False means "must never happen"
    final_topology: str | None = None

    def overlay(self, other: Limits) -> Limits:
        """``other``'s explicitly set fields (including explicit nulls) replace ours."""
        merged = self.model_dump()
        for key in other.model_fields_set:
            merged[key] = getattr(other, key)
        return Limits(**merged)


class SloConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: Limits = Limits()
    profiles: dict[str, Limits] = {}
    sync: dict[str, Limits] = {}

    @field_validator("sync", mode="before")
    @classmethod
    def _bool_keys(cls, value: object) -> object:
        # YAML 1.1 reads unquoted `on:` and `off:` as booleans.
        if isinstance(value, dict):
            return {{True: "on", False: "off"}.get(k, k): v for k, v in value.items()}
        return value

    @staticmethod
    def load(path: Path) -> SloConfig:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return SloConfig.model_validate(data)

    def limits_for(self, profile: str, sync: str, expect: Limits | None = None) -> Limits:
        limits = self.defaults
        limits = limits.overlay(self.profiles.get(profile, Limits()))
        limits = limits.overlay(self.sync.get(sync, Limits()))
        if expect is not None:
            limits = limits.overlay(expect)
        return limits


class Check(BaseModel):
    name: str
    limit: Any
    actual: Any
    passed: bool
    note: str | None = None


def _max_check(name: str, limit: float | None, actual: float | None, missing: str) -> Check | None:
    if limit is None:
        return None
    if actual is None:
        return Check(name=name, limit=limit, actual=None, passed=False, note=missing)
    return Check(name=name, limit=limit, actual=round(actual, 2), passed=actual <= limit)


def evaluate(metrics: Metrics, final: FinalTopology, limits: Limits) -> list[Check]:
    """One ``Check`` per limit that is set. The scenario passes if all of them pass."""
    checks: list[Check] = []
    had_failover = metrics.new_leader is not None

    if limits.failover is not None:
        checks.append(
            Check(
                name="failover",
                limit=limits.failover,
                actual=had_failover,
                passed=had_failover == limits.failover,
                note=f"{metrics.old_leader} -> {metrics.new_leader}" if had_failover else None,
            )
        )

    if limits.detection_sec_max is not None and (limits.failover is not False or had_failover):
        c = _max_check(
            "detection_sec", limits.detection_sec_max, metrics.detection_sec, "no new leader"
        )
        if c:
            checks.append(c)

    c = _max_check(
        "rto_write_sec",
        limits.rto_write_sec_max,
        metrics.write_outage.rto_write_sec,
        "writes never recovered",
    )
    if c:
        checks.append(c)

    c = _max_check("rto_read_sec", limits.rto_read_sec_max, metrics.rto_read_sec, "no good read")
    if c:
        checks.append(c)

    if limits.rejoin_sec_max is not None and metrics.recovery_action is not None:
        c = _max_check(
            "rejoin_sec", limits.rejoin_sec_max, metrics.rejoin_sec, "node never rejoined"
        )
        if c:
            checks.append(c)

    if limits.demotion_sec_max is not None and metrics.demotion_expected:
        c = _max_check(
            "demotion_sec",
            limits.demotion_sec_max,
            metrics.demotion_sec,
            "old primary never demoted",
        )
        if c:
            checks.append(c)

    if limits.lost_acked_commits_max is not None:
        lost = metrics.rpo.lost_acked_commits
        checks.append(
            Check(
                name="lost_acked_commits",
                limit=limits.lost_acked_commits_max,
                actual=lost,
                passed=lost <= limits.lost_acked_commits_max,
                note=f"ids {metrics.rpo.lost_acked[:10]}" if lost else None,
            )
        )

    if limits.split_brain is not None:
        detected = metrics.split_brain.detected
        checks.append(
            Check(
                name="split_brain",
                limit=limits.split_brain,
                actual=detected,
                passed=detected == limits.split_brain,
                note=f"{len(metrics.split_brain.rounds)} rounds" if detected else None,
            )
        )

    if limits.final_topology is not None:
        if limits.final_topology != FINAL_TOPOLOGY_HEALTHY:
            raise ValueError(f"unsupported final_topology {limits.final_topology!r}")
        checks.append(
            Check(
                name="final_topology",
                limit=limits.final_topology,
                actual="healthy" if final.healthy else "; ".join(final.problems),
                passed=final.healthy,
            )
        )

    return checks


def passed(checks: list[Check]) -> bool:
    return all(c.passed for c in checks)
