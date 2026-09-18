"""Report writers: JSON now, Markdown and HTML in Phase 5.

A run directory holds:

- ``result.json``   everything: config, versions, events, metrics, checks, final state
- ``timeline.json`` the ordered events only
- ``ledger.jsonl``  every write attempt (streamed by the workload while it runs)
- ``rounds.jsonl``  every poller round (per-node ground truth)
- ``reads.jsonl``   every read attempt

A suite run additionally writes ``suite.json`` with one summary row per scenario.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from leaderfall.scenario import RunResult


def write_run(result: RunResult) -> Path:
    report_dir = Path(result.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "result.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (report_dir / "timeline.json").write_text(
        json.dumps([e.model_dump() for e in result.events], indent=2) + "\n", encoding="utf-8"
    )
    return report_dir


def load_run(report_dir: Path) -> RunResult:
    return RunResult.model_validate_json((report_dir / "result.json").read_text(encoding="utf-8"))


class SuiteRow(BaseModel):
    scenario: str
    profile: str
    sync: str
    passed: bool
    error: str | None
    detection_sec: float | None
    rto_write_sec: float | None
    ack_gap_sec: float | None
    readonly_window_sec: float | None
    rto_read_sec: float | None
    lost_acked_commits: int
    unknown_commits: int
    split_brain: bool
    demotion_sec: float | None
    rejoin_sec: float | None
    failed_checks: list[str]
    report_dir: str

    @staticmethod
    def from_result(r: RunResult) -> SuiteRow:
        m = r.metrics
        return SuiteRow(
            scenario=r.scenario,
            profile=str(r.cluster.get("profile")),
            sync=str(r.cluster.get("sync")),
            passed=r.passed,
            error=r.error,
            detection_sec=m.detection_sec,
            rto_write_sec=m.write_outage.rto_write_sec,
            ack_gap_sec=m.write_outage.ack_gap_sec,
            readonly_window_sec=m.write_outage.readonly_window_sec,
            rto_read_sec=m.rto_read_sec,
            lost_acked_commits=m.rpo.lost_acked_commits,
            unknown_commits=m.ledger.unknown,
            split_brain=m.split_brain.detected,
            demotion_sec=m.demotion_sec,
            rejoin_sec=m.rejoin_sec,
            failed_checks=[c.name for c in r.checks if not c.passed],
            report_dir=r.report_dir,
        )


class SuiteResult(BaseModel):
    started_at: str
    finished_at: str
    tag: str
    rows: list[SuiteRow]
    passed: bool
    host: dict[str, Any] = {}
    versions: dict[str, Any] = {}


def write_suite(suite: SuiteResult, reports_root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = reports_root / f"{stamp}-suite-{suite.tag}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "suite.json").write_text(suite.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
