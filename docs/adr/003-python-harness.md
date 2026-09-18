# ADR-003: Python for the chaos harness

**Status:** Accepted
**Date:** 2026-09-18

## Context

The harness controls Docker, drives a write workload, polls Patroni and PostgreSQL, measures timings, checks SLOs, and writes reports. It also needs unit tests for the measurement logic. Options were shell, Go, and Python.

## Decision

Write the harness in **Python 3.12** with type hints everywhere, checked by `mypy` in strict mode and linted by `ruff`.

Main libraries, all pinned:

| Need | Library |
|---|---|
| Docker control | `docker` SDK |
| PostgreSQL client | `psycopg` 3 |
| Patroni REST | `httpx` |
| CLI | `typer` + `rich` |
| Scenario and result models | `pydantic` |
| Scenario files | `pyyaml` |
| Reports and Patroni config | `jinja2` |
| Tests | `pytest` |

## Why

- **Speed of writing.** The harness is mostly glue and measurement. Python is the fastest way to write that and keep it readable.
- **The ecosystem fits.** `psycopg` 3 and the Docker SDK are mature. Patroni itself is Python, so the config templating and REST calls feel native.
- **Testable measurement.** RTO, RPO, and split-brain math can be unit tested with fake ledgers and fake poll results, with no cluster.
- **Familiar to DBREs.** The people this lab is for read and write Python.

## Rejected

- **Shell scripts**: fine for a demo, bad for measurement logic, timelines, and reports. Hard to test.
- **Go**: good for a distributable binary, but slower to iterate and less common in the target audience. Not needed for a lab that runs from a repo.

## Consequences

- Users need Python 3.12+ and Docker. Nothing else.
- All timings use `time.monotonic()` on the host. Never the container clock, never wall time for durations.
- Every scenario is YAML. Adding a scenario should need no Python.
