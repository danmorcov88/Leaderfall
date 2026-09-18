# Leaderfall

Break a PostgreSQL HA cluster on purpose, and prove with numbers that it survives.

Leaderfall runs a real PostgreSQL 17 high-availability cluster (Patroni + etcd + HAProxy) on one machine with Docker Compose. Then it runs automated failure tests against it: kill the primary, cut the network, break etcd quorum, freeze a process, fill the WAL disk.

Each test keeps a ledger of every commit the database confirmed, injects a fault, and measures what really happened:

- **RTO** — how long writes were down
- **RPO** — whether any confirmed commit was lost
- **Split brain** — whether two primaries ever accepted writes at the same time
- **Rejoin** — whether and how fast the failed node comes back as a replica

The results are checked against SLO limits and written to a report. The suite runs in GitHub Actions on every push and every night.

## Status

The cluster, the measurement harness, the scenario engine and the six core scenarios are done. Every push runs `primary-sigkill` on a real cluster in GitHub Actions. The advanced scenarios (etcd loss, frozen primary, sync mode, disk full, ...) and the HTML reports come next. See [docs/architecture.md](docs/architecture.md) for what runs where and how things are measured, [docs/runbooks](docs/runbooks) for what each scenario does and what a DBA should do about it, and [docs/adr](docs/adr) for the design decisions.

### Core suite

`make chaos-all` (`leaderfall run --all --tag core`), `default` profile (ttl 30 / loop_wait 10 / retry_timeout 10), async replication, 50 writes/s, one laptop. Every scenario passed its SLO.

| Scenario | Failover | Detection | RTO write | Read-only window | Lost acked | Unknown | Split brain | Old primary fenced | Rejoin |
|---|---|---|---|---|---|---|---|---|---|
| `primary-sigkill` | yes | 23.2 s | 29.3 s | 2.2 s | 0 | 1 | no | – | 7.6 s |
| `primary-clean-stop` | yes | 1.6 s | 5.5 s | – | 0 | 0 | no | – | 2.6 s |
| `primary-partition` | yes | 30.2 s | 32.5 s | – | 0 | 0 | no | 15.2 s | 20.1 s |
| `replica-loss` | no | – | 0.0 s | – | 0 | 0 | no | – | 3.2 s |
| `planned-switchover` | yes | 7.2 s | 9.7 s | – | 0 | 0 | no | – | – |
| `haproxy-restart` | no | – | 6.8 s | – | 0 | 0 | no | – | – |

What the core suite showed:

- **A clean stop or a switchover costs 5–10 s; a hard kill costs 30–37 s.** The difference is the leader lease (`ttl`). Plan maintenance; do not pull plugs.
- **A partitioned primary fences itself at ~12–15 s, well before the new leader appears at ~27–30 s.** That gap (`ttl - retry_timeout - loop_wait`) is the split-brain safety margin. It held in every run. The poller kept probing the isolated node through `docker exec` to be sure.
- **HAProxy sent one write to a replica after a restart** because a fresh HAProxy assumes every server is UP until the first check. `init-state down` fixed it. The lab found a config bug in its own proxy.
- **HAProxy soft-stops on SIGTERM**: a connected client kept committing for the whole 5 s grace period; the outage only began when Docker killed the process.

### First numbers (Phase 2)

Five `primary-sigkill` runs in a row, `default` profile (ttl 30), async replication, 50 writes/s, on one laptop:

| Run | Detection | RTO write | Read-only window | Lost acked commits | Unknown commits | Split brain | Rejoin |
|---|---|---|---|---|---|---|---|
| 1 | 27.7 s | 29.9 s | 0 | 0 | 0 | no | 5.5 s |
| 2 | 28.3 s | 31.9 s | 0 | 0 | 0 | no | 6.1 s |
| 3 | 25.2 s | 29.3 s | 0.4 s | 0 | 1 (missing) | no | 5.6 s |
| 4 | 29.7 s | 37.3 s | 5.1 s | 0 | 0 | no | 6.2 s |
| 5 | 28.7 s | 32.2 s | 0 | 0 | 0 | no | 6.7 s |

Two things these runs showed that a demo would not:

- **The new primary can be read-only for seconds after Patroni calls it the leader.** Patroni takes the lock and answers `/primary` with 200 as soon as it sends the promote request. HAProxy starts routing writes at once. But PostgreSQL's startup process is asleep in its WAL-receiver retry loop (`wal_retrieve_retry_interval`, 5 s by default) and only acts on the promote when it wakes up. In run 4 that window was 5.1 s and 231 writes failed with `cannot execute INSERT in a read-only transaction`. This is why RTO varies by 8 s between runs with the same settings.
- **`unknown` commits are real.** In run 3 one `COMMIT` was sent, the connection died, and the row was not there afterwards. An application that retries such a write without an idempotency key will duplicate it; one that does not retry will lose it.

## Quick start

```
git clone https://github.com/danmorcov88/Leaderfall.git
cd Leaderfall
make up      # start the cluster and wait until it is healthy
make status  # topology: node, role, state, timeline, lag
make chaos   # run primary-sigkill and write a report
make down    # stop it (VOLUMES=1 also deletes the data)
```

Then, for the whole core suite:

```
make chaos-all             # every scenario tagged core, about 7 minutes
leaderfall list            # what is available
leaderfall run <name>      # one scenario
```

Each run writes `reports/<timestamp>-<scenario>/` with `result.json`, the event timeline, and the raw ledger, poller rounds and read samples, so any number can be recomputed. A suite run adds `reports/<timestamp>-suite-<tag>/suite.json`.

Needs Docker (with Compose v2) and Python 3.12+. Nothing else.

`make up` builds the Patroni image, starts 3 x etcd, 3 x PostgreSQL + Patroni and HAProxy, and returns only when there is one leader and two streaming replicas on the same timeline and HAProxy routes writes to the primary. From zero this takes about 20 seconds on a laptop once the image is built.

Writes go to `localhost:5000`, reads to `localhost:5001`, the HAProxy stats page is at `http://localhost:7000/`.

### Profiles and sync mode

```
leaderfall up --profile fast          # ttl 15 / loop_wait 5 / retry_timeout 5 (default: 30/10/10)
leaderfall up --sync on               # synchronous replication with one sync standby
leaderfall up --sync strict           # ... and refuse writes when no sync standby is available
```

These can be changed on a running cluster. `up` patches Patroni's dynamic configuration through its REST API, so nothing restarts and no failover happens.

## Scenarios

Scenarios are YAML files in [`scenarios/`](scenarios/). Adding one needs no Python. Each has steps (`fault`, `action`, `wait_for`, `hold_sec`) and an `expect` block that overrides the limits in [`slo.yml`](slo.yml).

```yaml
name: primary-sigkill
description: Kill the primary process with SIGKILL during steady writes.
tags: [core, smoke]
cluster:
  patroni_profile: default
  synchronous_mode: false
workload:
  rate_per_sec: 50
  warmup_sec: 15
steps:
  - fault: kill
    target: primary
    signal: SIGKILL
  - wait_for: new_leader
    timeout_sec: 90
  - wait_for: writes_ok
  - hold_sec: 10
  - action: start
    target: failed_node
  - wait_for: node_streaming
    timeout_sec: 180
expect:
  failover: true
  rto_write_sec_max: 45
  lost_acked_commits_max: null   # async: report only
  split_brain: false
  final_topology: 1-leader-2-replicas
```

Faults: `kill`, `stop`, `pause`, `partition`, `switchover`, `restart_service`. Actions: `start`, `unpause`, `heal`. Waits: `new_leader`, `writes_ok`, `cluster_healthy`, `node_streaming`, `node_fenced`. Targets: `primary`, `sync_replica`, `any_replica`, `failed_node`, `haproxy`, `etcd-1..3`, or a node name.

| Scenario | Tag | What breaks | Runbook |
|---|---|---|---|
| `primary-sigkill` | core, smoke | primary dies hard | [runbook](docs/runbooks/primary-sigkill.md) |
| `primary-clean-stop` | core | primary stops cleanly | [runbook](docs/runbooks/primary-clean-stop.md) |
| `primary-partition` | core | primary cut off the network, then healed | [runbook](docs/runbooks/primary-partition.md) |
| `replica-loss` | core | one replica dies | [runbook](docs/runbooks/replica-loss.md) |
| `planned-switchover` | core | operator moves the leader | [runbook](docs/runbooks/planned-switchover.md) |
| `haproxy-restart` | core | the proxy restarts | [runbook](docs/runbooks/haproxy-restart.md) |

## Development

```
make install                        # venv + dependencies
make lint                           # ruff + mypy
make test                           # unit tests, no Docker needed
.venv/bin/pytest -m integration     # checks against a running cluster
```

## License

Apache-2.0. See [LICENSE](LICENSE).
