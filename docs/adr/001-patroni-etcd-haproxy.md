# ADR-001: Patroni + etcd + HAProxy for the HA stack

**Status:** Accepted
**Date:** 2026-09-18

## Context

The lab needs a PostgreSQL high-availability setup that is close to what teams run in production, so that the failure tests say something useful. It must fail over by itself, keep a single primary, and give clients one stable address for writes and one for reads.

The common options are:

- Patroni with a distributed configuration store (etcd, Consul, ZooKeeper)
- repmgr
- pg_auto_failover
- Stolon
- A managed cloud service

## Decision

Use **Patroni** for cluster management, **etcd** (3 members, v3 API) as the distributed configuration store, and **HAProxy** as the client-facing router.

## Why

- **Patroni** is the most widely deployed open-source failover manager for PostgreSQL. It is what most operators and Kubernetes charts are built on. It has a REST API that exposes role and health per node, which the harness can poll directly. It supports `pg_rewind`, replication slots, synchronous mode (normal and strict), and `maximum_lag_on_failover`. Every one of these is something the scenarios want to test.
- **etcd** is the DCS that Patroni's own docs and most deployments use. Running three members lets the lab break quorum on purpose, which is one of the most important scenarios (the primary must demote itself when it cannot renew its lease).
- **HAProxy** is simple and does one thing: it asks each node "are you the primary?" over Patroni's REST API and routes writes only to the one that says yes. This makes the client path realistic without adding a connection pooler or a virtual IP, which are listed as future work.

## Rejected

- **repmgr**: no built-in DCS; the witness model is weaker for split-brain testing.
- **pg_auto_failover**: good design, but the monitor is a single point of failure and it is less common in the field.
- **Stolon**: Kubernetes-focused, less active.
- **Managed cloud services**: hide the failure mechanics that this lab exists to expose.

## Consequences

- The cluster has 7 containers (3 PostgreSQL + Patroni, 3 etcd, 1 HAProxy). This is fine on one machine.
- Patroni's watchdog cannot be used in Docker. The docs must say what this means for the "frozen primary" scenario.
- The harness talks to Patroni's REST API for topology and to PostgreSQL directly for ground truth (`pg_is_in_recovery()`, timeline, row counts).
