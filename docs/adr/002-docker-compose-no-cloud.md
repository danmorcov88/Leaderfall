# ADR-002: Docker Compose on one machine, no cloud

**Status:** Accepted
**Date:** 2026-09-18

## Context

The lab needs a place to run a seven-container cluster, inject faults into it, and repeat that on every push. Options range from a single laptop to a cloud account to a Kubernetes cluster.

## Decision

Everything runs locally with **Docker Compose**. No cloud account, no Kubernetes, no paid service. The same Compose file runs on a developer machine and in GitHub Actions.

## Why

- **Anyone can run it.** `git clone`, `make up`, `make chaos`. No accounts, no credentials, no bill.
- **Faults are easy to inject.** Docker gives `kill`, `stop`, `pause`, `network disconnect`, and the container has `tc` for network delay. This covers every scenario in scope.
- **CI is free.** GitHub's hosted runners have Docker. The full suite fits in a nightly job.
- **Reproducible.** Image tags and Python dependencies are pinned. A run on one machine should give numbers in the same range as a run on another.

## Rejected

- **Kubernetes with a Postgres operator**: realistic, but it adds a control plane between the harness and the cluster, hides some faults, and needs more resources than a hosted runner has. Listed as future work.
- **Cloud VMs**: cost money, need credentials in CI, and slow down the edit-run loop.

## Consequences

- Timing numbers depend on the host. The reports record the host and versions used, and the README compares numbers across runs instead of claiming absolute values.
- Some kernel features are not available in containers. The Patroni watchdog (softdog) is the main one. This is documented, not faked.
- PostgreSQL containers get `NET_ADMIN` only for the scenarios that use `tc netem`.
