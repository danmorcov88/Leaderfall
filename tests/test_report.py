"""Report rendering, from a real run and a real suite saved under tests/data."""

from pathlib import Path

import pytest

from leaderfall import report
from leaderfall.scenario import RunResult

DATA = Path(__file__).parent / "data"


@pytest.fixture
def run() -> RunResult:
    return RunResult.model_validate_json((DATA / "result.json").read_text(encoding="utf-8"))


@pytest.fixture
def suite() -> report.SuiteResult:
    return report.SuiteResult.model_validate_json((DATA / "suite.json").read_text(encoding="utf-8"))


def test_run_html_has_the_numbers_and_an_unescaped_svg(run: RunResult) -> None:
    page = report.render_run_html(run)
    assert "<svg" in page
    assert "&lt;svg" not in page
    assert run.scenario in page
    assert "SLO checks" in page
    assert f"{run.metrics.detection_sec:.1f} s" in page


def test_run_markdown(run: RunResult) -> None:
    md = report.render_run_markdown(run)
    assert md.startswith(f"# {run.scenario}: ")
    assert "| detection |" in md
    assert "## Events" in md


def test_timeline_marks_fault_and_recovery(run: RunResult) -> None:
    svg = report.timeline_svg(run)
    assert "writes down" in svg
    assert "new leader" in svg
    assert svg.count("<circle") >= 3


def test_write_run_produces_all_files(run: RunResult, tmp_path: Path) -> None:
    run.report_dir = str(tmp_path / "run")
    out = report.write_run(run)
    assert {p.name for p in out.iterdir()} == set(report.RUN_FILES)


def test_suite_table_and_readme_update(suite: report.SuiteResult, tmp_path: Path) -> None:
    table = report.suite_markdown_table(suite)
    assert table.count("\n") == len(suite.rows) + 1
    readme = tmp_path / "README.md"
    readme.write_text(
        f"intro\n{report.README_START}\nold\n{report.README_END}\noutro\n", encoding="utf-8"
    )
    assert report.update_readme_results(readme, suite)
    text = readme.read_text(encoding="utf-8")
    assert "old" not in text
    assert text.startswith("intro\n")
    assert text.endswith("outro\n")
    assert "Last full run:" in text
    assert not report.update_readme_results(readme, suite)  # idempotent


def test_readme_without_markers_is_an_error(suite: report.SuiteResult, tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("no markers", encoding="utf-8")
    with pytest.raises(ValueError, match="markers"):
        report.update_readme_results(readme, suite)


def test_suite_html(suite: report.SuiteResult) -> None:
    page = report.render_suite_html(
        suite, {r.scenario: f"{r.scenario}/report.html" for r in suite.rows}
    )
    assert page.count("<tr>") == len(suite.rows) + 1
    assert 'href="primary-sigkill/report.html"' in page
