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
| `fast` | 20 | 5 | 5 |

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

## How a scenario is measured

`leaderfall run primary-sigkill` collects raw data with three background threads and computes every metric from that data afterwards. Nothing is estimated.

### Data collectors (`workload.py`)

| Thread | Rate | What it records |
|---|---|---|
| Writer | 50/s | One row per transaction through HAProxy `:5000`, as `BEGIN` / `INSERT` / `COMMIT`. Every attempt goes into `ledger.jsonl` with its id, send time, done time and result. |
| Reader | 10/s | A small query through HAProxy `:5001`: ok or failed, and which node answered. |
| Node poller | 2/s | For every node over its direct port: `pg_is_in_recovery()`, the control-file timeline, and, if the node says it is a primary, a real probe `INSERT` + `COMMIT`. The same round records who Patroni's `/cluster` calls the leader. |

Ledger results:

- `acked`: the server confirmed `COMMIT`.
- `failed`: an error before `COMMIT` was sent (connect failure, `INSERT` error). The row is certainly not committed.
- `unknown`: `COMMIT` was sent and the connection broke before the answer came back. The row may or may not exist. This is the case every application has to handle after a failover.

Nodes are identified by a custom GUC, `leaderfall.node_name`, set per node in `patroni.yml`. (`cluster_name` cannot be used: Patroni sets it to the scope on every node.)

The poller reconnects to a dead node in the background. On Docker Desktop the published port of a dead container still accepts TCP connections, so a blocking reconnect would stretch every round to the timeout and ruin the 0.5 s resolution.

### Metrics (`measure.py`)

| Metric | Definition |
|---|---|
| Detection | Fault → first poller round in which Patroni reports a leader other than the old one. |
| RTO write | Fault → first `acked` write after writes started failing. A write sent after the fault was requested but before the signal landed can still be acked by the dying primary; that does not end the outage. |
| Write gap | Last ack before the outage → first ack after it. What a client feels. |
| Read-only window | First → last write that reached a node that was still in recovery. Those writes fail with `cannot execute INSERT in a read-only transaction`. Usually the new primary between Patroni's promote request and PostgreSQL finishing the promotion. |
| RTO read | Longest gap between two successful reads. |
| Lost acked commits (RPO) | Ids marked `acked` that are not in the table on the final primary. |
| Unknown commits | Count of `unknown` ids, split into present and missing on the final primary. Reported, never a failure. |
| Split brain | A poller round in which two nodes both committed the probe row. Rounds where two nodes said "not in recovery" but only one accepted the write are reported separately. |
| Rejoin | `docker start` of the failed node → first round in which Patroni shows it `streaming` with lag 0. |
| Diverged rows (max) | Lost acked + unknown-and-missing. An upper bound on the rows `pg_rewind` discarded. The run also records whether `pg_rewind` ran and the LSN it diverged at, from the node's log. |
| Final state | One leader, two streaming replicas, same timeline, same row count on all nodes. |

The fault time is the moment the `docker kill` was *requested*. The signal lands within tens of milliseconds; the Docker CLI then takes about half a second to return, and using that later time would hide part of the outage.

| Demotion | For faults that leave the old primary running (partition, freeze): fault → first poller round after which it never again committed the probe row. The moment it stopped taking writes, in Patroni's own words "Demoting self (offline)". |

### Output

`reports/<UTC timestamp>-<scenario>/` holds `result.json` (config, versions, events, metrics, SLO checks, final state), `timeline.json`, `ledger.jsonl`, `rounds.jsonl` and `reads.jsonl`. The raw files are kept so any number in the report can be recomputed. A suite run adds `reports/<UTC timestamp>-suite-<tag>/suite.json`.

### Watching a node nobody can reach

A node cut off the network keeps running, but its published port dies with its network endpoint. The poller then samples it through `docker exec psql` (one `psql` call: the state query, then the probe write). That is how `primary-partition` can show the isolated primary refusing writes 12 s after the partition and the new leader appearing 15 s later, and prove there was no overlap.

### Watching a frozen node

`docker pause` stops every process in a container but not its kernel: TCP keepalives are still answered, so a query to a frozen node hangs instead of failing, and `docker exec` hangs too. The poller gives every node its own worker thread and waits for a sample only briefly; a sample that does not return is reported as "no answer" in that round and lands in the round in which it finally completes, marked with the round that issued it. The poller also checks the container's state before an `exec`. This keeps rounds at 0.5 s through a freeze. At the thaw, the hung query runs, and its probe write is the first thing the old primary does: that is how `primary-frozen` catches the split-brain window to the round.

### Stalls that are not errors

Synchronous replication turns a lost sync standby into commits that wait, not commits that fail. Two ledger-derived numbers catch that: the longest gap between two acked writes over the whole run (`ack_gap_sec`, with an SLO limit `ack_gap_sec_max`) and the slowest single acked write.

## Scenario engine

A scenario is a YAML file in `scenarios/`, validated by a pydantic model (`scenario.py`). The runner:

1. runs `up` with the scenario's profile and sync mode (a no-op if the cluster already matches), empties the workload tables
2. starts the poller, writer and reader and waits for the warm-up: `warmup_sec` elapsed *and* at least one acked write
3. executes the steps in order
4. stops the workload, waits for a healthy cluster with equal row counts on all nodes, computes the metrics, and checks them against the limits

Steps:

| Step | Meaning |
|---|---|
| `fault: <name>` / `action: <name>` with `target` | Run a fault primitive. The first `fault` sets the fault time and the "failed node". `start`, `unpause` and `heal` on the failed node set the recovery time for the rejoin metric. |
| `wait_for: new_leader` | Patroni reports a leader other than the one before the fault. |
| `wait_for: writes_ok` | The most recent write sent after the fault was acked. |
| `wait_for: cluster_healthy` | One leader, two streaming replicas, same timeline, sync standby if sync mode is on. |
| `wait_for: node_streaming` | The target (default: the failed node) is a streaming replica with lag at or under `max_lag_bytes`. |
| `wait_for: node_fenced` | The target has not accepted a probe write in the last two poller rounds. |
| `hold_sec: N` | Keep the workload running for N seconds. Part of the workload definition, not a wait. |

Every wait has a `timeout_sec`. On a timeout or a fault error the runner records the error, does its best to bring the failed node back (unpause, start or reconnect, depending on the container's state), still waits for a healthy cluster, and reports the run as failed with whatever metrics it has.

Targets are resolved when the step runs: `primary`, `sync_replica` and `any_replica` (the lowest-named replica the scenario has not hit yet) from Patroni's `/cluster`; `failed_node`, `failed_node_2` and `old_primary` from what earlier steps established, without asking anyone (they must work while nobody answers); `haproxy`, `etcd-N`, or a node name.

### Fault primitives

| Primitive | Implementation | Note |
|---|---|---|
| `kill` | `docker kill -s <signal>` | fault time = request time |
| `stop` | `docker stop -t <grace>` | Patroni shuts down cleanly and releases the key |
| `pause` / `unpause` | `docker pause` / `unpause` | every process frozen, TCP stays open |
| `partition` / `heal` | `docker network disconnect` / `connect` | the node keeps running; published port dies |
| `restart_service` | `docker restart -t <grace>` | HAProxy, etcd members |
| `switchover` | Patroni `POST /switchover` | |
| `netem` / `netem_clear` | `tc qdisc ... netem <spec>` in a throwaway container that shares the target's network namespace (`--net container:` + `NET_ADMIN`) | the PostgreSQL containers keep no capability. Needs `sch_netem` in the host kernel: standard Linux and GitHub runners yes, Docker Desktop's WSL2 kernel no |
| `write_wal` | one bulk insert on the primary, `mb` megabytes | not a fault: creates real replication lag behind a frozen replica, whose TCP buffer would otherwise absorb a small workload |
| `fill_disk` / `free_disk` | `fallocate` in the `pg_wal` directory | refuses unless the `pg_wal` filesystem is under 4 GB; on Docker Desktop and CI it is the shared host disk. See the `wal-disk-full` runbook |

### SLO limits

`slo.yml` holds limits in layers: `defaults`, then per Patroni profile, then per sync mode. The scenario's `expect` block is the last layer. Later layers win; a key set to `null` disables a check. Checks: `failover` (must or must not happen), `detection_sec_max`, `rto_write_sec_max`, `rto_read_sec_max`, `rejoin_sec_max` (only when a recovery action ran), `demotion_sec_max` (only when the old primary stayed up), `lost_acked_commits_max`, `split_brain`, `final_topology`. A scenario passes when every check passes and no step failed. The suite passes when every scenario passes; `leaderfall run` exits 1 otherwise, which is what fails CI.
