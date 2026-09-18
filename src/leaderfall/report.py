"""Report writers: JSON now, Markdown and HTML in Phase 5.

A run directory holds:

- ``result.json``   everything: config, versions, events, metrics, final state
- ``timeline.json`` the ordered events only
- ``ledger.jsonl``  every write attempt (streamed by the workload while it runs)
- ``rounds.jsonl``  every poller round (per-node ground truth)
- ``reads.jsonl``   every read attempt
"""

from __future__ import annotations

import json
from pathlib import Path

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
