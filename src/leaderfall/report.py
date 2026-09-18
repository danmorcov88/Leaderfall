"""Report writers: JSON, Markdown and HTML, for one run and for a suite.

A run directory holds:

- ``result.json``   everything: config, versions, events, metrics, checks, final state
- ``timeline.json`` the ordered events only
- ``report.md`` / ``report.html``  the human-readable report, with an SVG timeline
- ``ledger.jsonl``, ``rounds.jsonl``, ``reads.jsonl``  the raw data

A suite directory holds ``suite.json``, ``suite.md`` and ``index.html`` (one table, linking
to each run). ``build_site`` copies a suite and its runs into one folder for publishing.
"""

from __future__ import annotations

import html
import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup
from pydantic import BaseModel

from leaderfall.scenario import RunResult

_env = Environment(
    loader=PackageLoader("leaderfall", "templates"),
    autoescape=select_autoescape(["html", "j2"]),
)
_CSS = (Path(__file__).parent / "templates" / "report.css").read_text(encoding="utf-8")

RUN_FILES = ("result.json", "timeline.json", "report.md", "report.html")


def fmt(value: float | None, unit: str = " s") -> str:
    return "-" if value is None else f"{value:.1f}{unit}"


# --------------------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------------------


def metric_rows(r: RunResult) -> list[tuple[str, str, str]]:
    m = r.metrics
    o = m.write_outage
    rows: list[tuple[str, str, str]] = []
    if m.new_leader:
        rows.append(("failover", f"{m.old_leader} → {m.new_leader}", ""))
    else:
        rows.append(("failover", "none", f"leader stayed {m.old_leader}"))
    rows += [
        ("detection", fmt(m.detection_sec), "fault → Patroni shows a new leader"),
        ("RTO write", fmt(o.rto_write_sec), "fault → first acked write after writes failed"),
        ("write gap", fmt(o.gap_sec), "last ack before → first ack after"),
        (
            "commit stall",
            fmt(o.ack_gap_sec),
            f"longest gap between two acked writes; slowest write {fmt(o.max_write_latency_sec)}",
        ),
        (
            "read-only window",
            fmt(o.readonly_window_sec),
            f"{o.readonly_failures} writes reached a node that was still read-only",
        ),
        (
            "RTO read",
            fmt(m.rto_read_sec),
            "longest gap between good reads"
            + (
                f"; {m.reads_on_primary} reads fell back to the primary"
                if m.reads_on_primary
                else ""
            ),
        ),
        (
            "lost acked commits",
            str(m.rpo.lost_acked_commits),
            f"of {m.ledger.acked} acked"
            + (f": ids {m.rpo.lost_acked[:10]}" if m.rpo.lost_acked else ""),
        ),
        (
            "unknown commits",
            str(m.ledger.unknown),
            f"{len(m.rpo.unknown_present)} present, {len(m.rpo.unknown_missing)} missing",
        ),
        (
            "split brain",
            "yes" if m.split_brain.detected else "no",
            f"{len(m.split_brain.rounds)} rounds with 2 writable primaries"
            + (f" over {m.split_brain.window_sec:.1f} s" if m.split_brain.window_sec else "")
            + f", {len(m.split_brain.multi_primary_rounds)} with 2 primaries but 1 writable",
        ),
    ]
    if m.demotion_expected:
        rows.append(
            (
                "demotion",
                fmt(m.demotion_sec),
                f"fault → {m.demotion_node} stops taking writes "
                f"(last accepted at {fmt(m.old_primary_last_write_sec)})",
            )
        )
    if m.failed_node:
        rows.append(
            (
                "rejoin",
                fmt(m.rejoin_sec),
                f"{m.recovery_action or 'no action'} → streaming with lag 0",
            )
        )
    rewind = (
        f"pg_rewind ran: {r.rewind.get('ran')}, diverged at {r.rewind.get('diverged_at_lsn')}"
        if r.rewind
        else ""
    )
    rows += [
        ("timeline", f"{m.timeline_before} → {m.timeline_after}", rewind),
        (
            "ledger",
            str(m.ledger.attempts),
            f"{m.ledger.acked} acked, {m.ledger.failed} failed, {m.ledger.unknown} unknown",
        ),
        (
            "final state",
            "healthy" if r.final.healthy else "unhealthy",
            f"leader {r.final.leader}, replicas {r.final.replicas}, rows {r.final.row_counts}",
        ),
    ]
    return rows


def timeline_svg(r: RunResult, width: int = 1000) -> str:
    """One horizontal time axis with the events that matter marked on it."""
    m = r.metrics
    end = max(r.duration_sec, 1.0)
    left, right, top, height = 40, width - 20, 20, 150
    scale = (right - left) / end

    def x(t: float) -> float:
        return left + t * scale

    fault_at = r.faults[0].requested_at if r.faults else None
    marks: list[tuple[float, str, str]] = []  # (t, label, colour)
    for e in r.events:
        if e.name == "fault":
            marks.append((e.t, e.detail.split(" (")[0] if e.detail else "fault", "#b42318"))
        elif e.name == "wait:new_leader":
            marks.append((e.t, "new leader", "#175cd3"))
        elif e.name == "action":
            marks.append((e.t, e.detail.split(" (")[0] if e.detail else "action", "#b54708"))
    o = m.write_outage
    if o.first_ack_after_fault is not None:
        marks.append((o.first_ack_after_fault, "writes ok", "#157f3b"))
    if m.rejoin_sec is not None and r.metrics.recovery_action:
        rec = next((f for f in r.faults if f.action == r.metrics.recovery_action), None)
        if rec:
            marks.append((rec.done_at + m.rejoin_sec, "rejoined", "#157f3b"))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="timeline of {html.escape(r.scenario)}">',
        f'<line x1="{left}" y1="{top + 60}" x2="{right}" y2="{top + 60}" stroke="#98a2b3" stroke-width="2"/>',
    ]
    # writes-down band
    if o.first_failure_at is not None:
        x0 = x(o.first_failure_at)
        x1 = x(o.first_ack_after_fault) if o.first_ack_after_fault is not None else x(end)
        parts.append(
            f'<rect x="{x0:.1f}" y="{top + 48}" width="{max(x1 - x0, 1):.1f}" height="24" fill="#fdecea"/>'
        )
        parts.append(
            f'<text x="{x0 + 3:.1f}" y="{top + 88}" fill="#b42318">writes down '
            f"{fmt(o.rto_write_sec)}</text>"
        )
    # split brain band
    if m.split_brain.detected and m.split_brain.rounds:
        x0 = x(m.split_brain.rounds[0].t)
        x1 = x(m.split_brain.rounds[-1].t) + 3
        parts.append(
            f'<rect x="{x0:.1f}" y="{top + 40}" width="{max(x1 - x0, 3):.1f}" height="40" fill="#b42318" opacity="0.6"/>'
        )
        parts.append(f'<text x="{x0:.1f}" y="{top + 34}" fill="#b42318">split brain</text>')
    # ticks
    step = 10 if end <= 120 else 30
    t = 0
    while t <= end:
        parts.append(
            f'<line x1="{x(t):.1f}" y1="{top + 56}" x2="{x(t):.1f}" y2="{top + 64}" stroke="#98a2b3"/>'
        )
        parts.append(
            f'<text x="{x(t):.1f}" y="{top + 105}" text-anchor="middle" fill="#667085">{t}s</text>'
        )
        t += step
    # marks, labels alternate above/below to avoid overlap
    for i, (mt, label, colour) in enumerate(sorted(marks)):
        cx = x(mt)
        parts.append(f'<circle cx="{cx:.1f}" cy="{top + 60}" r="5" fill="{colour}"/>')
        y = top + 12 if i % 2 == 0 else top + 125
        anchor = "end" if cx > right - 70 else "start" if cx < left + 70 else "middle"
        parts.append(
            f'<text x="{cx:.1f}" y="{y}" text-anchor="{anchor}" fill="{colour}">'
            f"{html.escape(label)} {mt:.1f}s</text>"
        )
        parts.append(
            f'<line x1="{cx:.1f}" y1="{top + 60}" x2="{cx:.1f}" y2="{y + (4 if y < top + 60 else -12)}" stroke="{colour}" stroke-dasharray="2,2"/>'
        )
    if fault_at is None:
        parts.append(f'<text x="{left}" y="{top + 125}" fill="#667085">no fault injected</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def render_run_html(r: RunResult) -> str:
    return _env.get_template("run.html.j2").render(
        r=r,
        m=r.metrics,
        css=Markup(_CSS),
        svg=Markup(timeline_svg(r)),
        rows=metric_rows(r),
        fmt=fmt,
    )


def render_run_markdown(r: RunResult) -> str:
    lines = [
        f"# {r.scenario}: {'pass' if r.passed else 'FAIL'}",
        "",
        r.description,
        "",
        f"{r.started_at} · profile `{r.cluster.get('profile')}` · sync `{r.cluster.get('sync')}` · "
        f"{r.workload.rate_per_sec:g} writes/s · {r.duration_sec:.0f} s",
        "",
    ]
    if r.error:
        lines += [f"**Run aborted:** {r.error}", ""]
    lines += [
        "## SLO checks",
        "",
        "| check | limit | actual | result | note |",
        "|---|---|---|---|---|",
    ]
    for c in r.checks:
        lines.append(
            f"| {c.name} | {c.limit} | {c.actual} | {'pass' if c.passed else 'FAIL'} | {c.note or ''} |"
        )
    lines += ["", "## Metrics", "", "| metric | value | note |", "|---|---|---|"]
    for name, value, note in metric_rows(r):
        lines.append(f"| {name} | {value} | {note} |")
    lines += ["", "## Events", "", "| t | event | detail |", "|---|---|---|"]
    for e in r.events:
        lines.append(f"| {e.t:.2f} s | {e.name} | {e.detail or ''} |")
    lines += ["", "## Environment", ""]
    for k, v in {**r.versions, **{f"host {k}": v for k, v in r.host.items()}}.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) + "\n"


def write_run(result: RunResult) -> Path:
    report_dir = Path(result.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "result.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (report_dir / "timeline.json").write_text(
        json.dumps([e.model_dump() for e in result.events], indent=2) + "\n", encoding="utf-8"
    )
    (report_dir / "report.md").write_text(render_run_markdown(result), encoding="utf-8")
    (report_dir / "report.html").write_text(render_run_html(result), encoding="utf-8")
    return report_dir


def load_run(report_dir: Path) -> RunResult:
    return RunResult.model_validate_json((report_dir / "result.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# A suite
# --------------------------------------------------------------------------------------


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


def suite_markdown_table(s: SuiteResult) -> str:
    """The results table, also used to update the README between its markers."""
    head = (
        "| Scenario | Profile / sync | Result | Detection | RTO write | Commit stall | "
        "Lost acked | Unknown | Split brain | Fenced | Rejoin |\n|---|---|---|---|---|---|---|---|---|---|---|"
    )
    rows = [
        f"| `{r.scenario}` | {r.profile} / {r.sync} | {'pass' if r.passed else 'FAIL'} | "
        f"{fmt(r.detection_sec)} | {fmt(r.rto_write_sec)} | {fmt(r.ack_gap_sec)} | "
        f"{r.lost_acked_commits} | {r.unknown_commits} | {'yes' if r.split_brain else 'no'} | "
        f"{fmt(r.demotion_sec)} | {fmt(r.rejoin_sec)} |"
        for r in s.rows
    ]
    return "\n".join([head, *rows])


def render_suite_markdown(s: SuiteResult) -> str:
    return (
        f"# leaderfall suite `{s.tag}`: {'pass' if s.passed else 'FAIL'}\n\n"
        f"{s.started_at} → {s.finished_at} · {len(s.rows)} scenarios · "
        f"{sum(r.passed for r in s.rows)} passed\n\n" + suite_markdown_table(s) + "\n"
    )


def render_suite_html(s: SuiteResult, links: dict[str, str]) -> str:
    return _env.get_template("suite.html.j2").render(s=s, css=Markup(_CSS), links=links, fmt=fmt)


def _run_links(s: SuiteResult, suite_dir: Path) -> dict[str, str]:
    """Relative links from the suite page to each run's report, when both are on disk."""
    links: dict[str, str] = {}
    for row in s.rows:
        run_dir = Path(row.report_dir)
        try:
            rel = (
                Path(*[".."] * 1) / run_dir.name if run_dir.parent == suite_dir.parent else run_dir
            )
        except ValueError:
            rel = run_dir
        links[row.scenario] = (rel / "report.html").as_posix()
    return links


def write_suite(suite: SuiteResult, reports_root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = reports_root / f"{stamp}-suite-{suite.tag.replace(',', '+')}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "suite.json").write_text(suite.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (path / "suite.md").write_text(render_suite_markdown(suite), encoding="utf-8")
    (path / "index.html").write_text(
        render_suite_html(suite, _run_links(suite, path)), encoding="utf-8"
    )
    return path


def load_suite(suite_dir: Path) -> SuiteResult:
    return SuiteResult.model_validate_json((suite_dir / "suite.json").read_text(encoding="utf-8"))


def latest_report_dir(reports_root: Path) -> Path | None:
    """The newest suite directory, or the newest run directory if there is no suite."""
    suites = sorted(p for p in reports_root.glob("*-suite-*") if (p / "suite.json").is_file())
    if suites:
        return suites[-1]
    runs = sorted(p for p in reports_root.glob("*") if (p / "result.json").is_file())
    return runs[-1] if runs else None


def build_site(suite_dir: Path, out: Path, run_files: Sequence[str] = RUN_FILES) -> Path:
    """Copy a suite and its runs into ``out`` as a self-contained static site.

    ``out/index.html`` is the suite page; each run lives in ``out/<scenario>/``.
    """
    suite = load_suite(suite_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    links: dict[str, str] = {}
    for row in suite.rows:
        src = Path(row.report_dir)
        dst = out / row.scenario
        dst.mkdir()
        for name in run_files:
            if (src / name).is_file():
                shutil.copy2(src / name, dst / name)
        links[row.scenario] = f"{row.scenario}/report.html"
    (out / "index.html").write_text(render_suite_html(suite, links), encoding="utf-8")
    (out / "suite.json").write_text(suite.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (out / "suite.md").write_text(render_suite_markdown(suite), encoding="utf-8")
    return out


README_START = "<!-- results:start -->"
README_END = "<!-- results:end -->"


def update_readme_results(readme: Path, suite: SuiteResult) -> bool:
    """Replace the block between the markers with the suite table. Returns True if changed."""
    text = readme.read_text(encoding="utf-8")
    if README_START not in text or README_END not in text:
        raise ValueError(f"{readme} has no {README_START} / {README_END} markers")
    head, rest = text.split(README_START, 1)
    _, tail = rest.split(README_END, 1)
    body = (
        f"{README_START}\n"
        f"Last full run: {suite.finished_at}, {sum(r.passed for r in suite.rows)}/{len(suite.rows)} "
        f"passed, on {suite.host.get('platform', '?')} ({suite.host.get('cpus', '?')} CPUs).\n\n"
        f"{suite_markdown_table(suite)}\n{README_END}"
    )
    new = head + body + tail
    if new == text:
        return False
    readme.write_text(new, encoding="utf-8")
    return True
