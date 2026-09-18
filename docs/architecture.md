# Architecture

This page describes what runs where, and how the harness talks to it. It grows with each phase.

## Containers

```
                 +-------------------+
   workload ---> |  HAProxy          |  :5000 -> primary (writes)
   (writer,      |  health checks    |  :5001 -> replicas (reads)
    reader)      |  via Patroni REST |  :7000 -> stats page
                 +---------+---------+
                           |
        +------------------+------------------+
        |                  |                  |
   +----+-----+       +----+-----+       +----+-----+
   | pg-1     |       | pg-2     |       | pg-3     |
   | Patroni  |       | Patroni  |       | Patroni  |
   | Postgres |       | Postgres |       | Postgres |
   +----+-----+       +----+-----+       +----+-----+
        |                  |                  |
        +---------+--------+---------+--------+
                  |                  |
             +----+---+  +--------+  +--------+
             | etcd-1 |  | etcd-2 |  | etcd-3 |
             +--------+  +--------+  +--------+

   leaderfall CLI (on the host): controls Docker, runs workload,
   injects faults, measures, writes reports
```

| Container | Image | Role |
|---|---|---|
| `leaderfall-pg-1..3` | `leaderfall/postgres-patroni` (built from `postgres:17.11-trixie`, Patroni 4.1.5) | PostgreSQL managed by Patroni |
| `leaderfall-etcd-1..3` | `quay.io/coreos/etcd:v3.5.33` | Distributed configuration store (v3 API) |
| `leaderfall-haproxy` | `haproxy:3.2.23` | Routes writes to the primary and reads to replicas |

All containers share one Docker network, `leaderfall`. Every node has its own data volume, so a stopped or killed node keeps its data and can rejoin.

## Host ports

The harness runs on the host and reaches everything through published ports.

| Port | What |
|---|---|
| 5000 | HAProxy → current primary (writes) |
| 5001 | HAProxy → replicas, round-robin (reads) |
| 7000 | HAProxy stats page |
| 5441, 5442, 5443 | PostgreSQL on pg-1, pg-2, pg-3, direct. Used for ground-truth checks (`pg_is_in_recovery()`, timeline, row counts) that must not go through the proxy |
| 8011, 8012, 8013 | Patroni REST API on pg-1, pg-2, pg-3 |
| 2381, 2382, 2383 | etcd client API on etcd-1, etcd-2, etcd-3 |

Credentials are fixed lab values: superuser `postgres` / `postgres`, replication user `replicator` / `replicator`. This is a lab, not a deployment.

## How HAProxy finds the primary

HAProxy does not know about PostgreSQL roles. It asks Patroni. Every 2 seconds it sends `OPTIONS /primary` to each node's REST API on port 8008. Patroni answers 200 only on the node that currently holds the leader lock. The same is done with `/replica` for the read port.

`fall 2 rise 2` means a node is taken out after two failed checks and put back after two good ones. So after a failover, HAProxy starts routing to the new primary about 4 seconds after Patroni promotes it. `on-marked-down shutdown-sessions` kills client connections to a node the moment it is marked down, so clients do not sit on a dead socket.

Server addresses are resolved through Docker's DNS at run time (`resolvers docker`), so a node that comes back with a new IP is found again.

## How the Patroni config is managed

`docker/postgres-patroni/patroni.yml.j2` is rendered inside the container at start. The `bootstrap.dcs` block is written to etcd exactly once, when the cluster is first created. After that, the values live in etcd, not in the file.

`leaderfall up --profile <default|fast> --sync <off|on|strict>` therefore does not touch the Compose file. It starts the containers, waits for a healthy cluster, reads `GET /config`, and if anything differs from the requested profile, sends `PATCH /config`. Patroni applies the change on its next loop. This means you can switch profile or sync mode on a running cluster without a restart and without a failover.

| Profile | `ttl` | `loop_wait` | `retry_timeout` |
|---|---|---|---|
| `default` | 30 | 10 | 10 |
| `fast` | 15 | 5 | 5 |

| Sync mode | `synchronous_mode` | `synchronous_mode_strict` |
|---|---|---|
| `off` | false | false |
| `on` | true | false |
| `strict` | true | true |

## What "healthy" means

`leaderfall up` returns, and `leaderfall status` prints `healthy`, only when all of this is true:

- exactly one member has the `leader` role and is `running`
- every other member is a `streaming` replica (role `replica` or `sync_standby`) with lag 0
- all members are on the same timeline
- when sync mode is on, exactly one member is a `sync_standby`; when it is off, none
- `up` additionally checks that HAProxy port 5000 reaches a primary and port 5001 reaches a replica

There are no fixed sleeps. Every wait polls a real condition with a timeout, using the host's monotonic clock.

## Image builds

`leaderfall up` always runs `docker compose build --quiet` first. The build is cached and takes under a second when nothing changed. Attestations are disabled during the build (`BUILDX_NO_DEFAULT_ATTESTATIONS=1`); without that, BuildKit gives the image a new ID on every build and Compose recreates all three PostgreSQL containers, which causes a failover.
