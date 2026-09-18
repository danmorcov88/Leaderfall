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

The cluster, the measurement harness, the scenario engine and 16 scenarios (6 core, 10 advanced) are done. Every push runs `primary-sigkill` on a real cluster in GitHub Actions. HTML reports, the nightly run and Grafana come next. See [docs/architecture.md](docs/architecture.md) for what runs where and how things are measured, [docs/runbooks](docs/runbooks) for what each scenario does and what a DBA should do about it, and [docs/adr](docs/adr) for the design decisions.

### What this lab found

Things you would not learn from a demo that stops at "the cluster starts". Each one is measured, reproducible with one command, and written up in a runbook.

1. **A hard kill costs ~30 s; a clean stop or switchover ~5–10 s.** The difference is the leader lease. Plan maintenance; do not pull plugs.
2. **The new primary is read-only for 0–8 s after Patroni calls it the leader.** Patroni answers `/primary` with 200 as soon as it sends the promote request; PostgreSQL only acts on it when its startup process wakes from the WAL-receiver retry loop (`wal_retrieve_retry_interval`, 5 s). Writes in that window fail with `read-only transaction`. This is most of the RTO variance.
3. **Patroni's minimum `ttl` is 20 s.** A configured 15 is silently raised. The planned "fast" profile was impossible; it is 20/5/5 now, and detection runs 19–25 s instead of 23–30 s.
4. **A partitioned primary fences itself in 12–15 s; a primary that lost etcd quorum takes 14–29 s.** A member that is alive but quorumless answers slowly, and Patroni's retry budget takes longer to run out than when connections are refused outright. Both are inside `ttl`, so nobody else could take the lock.
5. **A frozen primary that thaws accepts writes for one Patroni loop.** Measured: one 0.5 s poll round with two writable primaries, then "Demoting self (immediate-nolock)". The hardware watchdog that closes this window cannot exist in a container; see below.
6. **`maximum_lag_on_failover` cannot see lag younger than one `loop_wait`.** It compares against the leader LSN last written to the DCS. With the leader killed within a loop of a bulk write, a replica missing ~16 MB was promoted and **109 acked commits were lost**. With one loop of time to publish the LSN, the guard held and the cluster stayed leaderless instead.
7. **Losing the sync standby is a 6 s commit stall with zero errors.** `on` and `strict` behave the same while another replica exists. A dashboard that counts errors shows nothing.
8. **Sync mode did not change the failover time**, it changed the guarantee: 0 lost commits by construction instead of by luck.
9. **Two proxy bugs, in the lab's own config:** a fresh HAProxy assumes every server is UP until the first check (one write went to a replica after a restart; `init-state down`), and a read pool that goes empty when the last replica is promoted (17 s read outage; `use_backend primary if nbsrv(replicas) eq 0`).
10. **`unknown` commits happen.** `COMMIT` sent, connection gone, row absent. Clients must treat that as unknown, not failed.

### The watchdog limit

Patroni's answer to a frozen or hung primary is a kernel watchdog: Patroni pets `/dev/watchdog` every loop, and if it stops, the kernel resets the whole machine before the lease can expire elsewhere. A frozen primary then reboots as a replica instead of waking up as a primary. Containers cannot use it: the device belongs to the Docker host, and Docker Desktop's VM does not even load `softdog`. This lab therefore proves the software fence (Patroni demotes on its first loop after a thaw) and measures the window the watchdog would close (about one loop). It cannot show the watchdog itself. In production, run `watchdog: mode: required`. Details in [docs/runbooks/primary-frozen.md](docs/runbooks/primary-frozen.md).

### Other limits of the Docker version

- `tc netem` needs the `sch_netem` kernel module. Docker Desktop's WSL2 kernel does not ship it; standard Linux hosts and GitHub runners do. The lagging-replica scenarios use `docker pause` plus a bulk write instead, which works everywhere.
- `wal-disk-full` needs a bounded `pg_wal` volume. On Docker Desktop and CI runners `pg_wal` sits on the shared host disk, and the `fill_disk` fault refuses to fill it. The scenario is written and wired but tagged `bounded-wal`; [its runbook](docs/runbooks/wal-disk-full.md) says how to run it on a host you control.

### Results

The table between the markers is rewritten by the nightly workflow from the last full run on a GitHub runner; the full HTML report with a timeline per scenario is at **https://danmorcov88.github.io/Leaderfall/**. Times in seconds; "reported" checks (split brain after a thaw, lost commits in the stale-optime scenario) are deliberately not enforced, and the runbook says why.

<!-- results:start -->
Last full run: 2026-09-18T12:24:04+00:00, 15/16 passed, on Windows-10-10.0.19045-SP0 (12 CPUs).

| Scenario | Profile / sync | Result | Detection | RTO write | Commit stall | Lost acked | Unknown | Split brain | Fenced | Rejoin |
|---|---|---|---|---|---|---|---|---|---|---|
| `double-fault` | default / off | pass | 34.4 s | 38.0 s | 37.9 s | 0 | 0 | no | - | 7.9 s |
| `etcd-one-member-loss` | default / off | pass | - | 0.0 s | 0.1 s | 0 | 0 | no | - | - |
| `etcd-quorum-loss` | default / off | pass | - | 54.1 s | 30.1 s | 0 | 0 | no | 24.7 s | - |
| `fast-profile-primary-sigkill` | fast / off | FAIL | 25.2 s | 29.3 s | 29.2 s | 0 | 0 | no | - | 8.2 s |
| `haproxy-restart` | default / off | pass | - | 6.3 s | 1.2 s | 0 | 0 | no | - | - |
| `lagging-replica-failover` | default / off | pass | - | 81.9 s | 68.2 s | 0 | 1 | no | - | 65.3 s |
| `lagging-replica-stale-optime` | default / off | pass | 40.1 s | 42.7 s | 41.0 s | 8 | 0 | no | - | 0.1 s |
| `planned-switchover` | default / off | pass | 8.4 s | 10.6 s | 3.8 s | 0 | 0 | no | - | - |
| `primary-clean-stop` | default / off | pass | 1.9 s | 5.6 s | 5.2 s | 0 | 0 | no | - | 2.0 s |
| `primary-frozen` | default / off | pass | 30.7 s | 33.9 s | 33.8 s | 0 | 0 | yes | 44.6 s | 11.0 s |
| `primary-partition` | default / off | pass | 22.7 s | 26.4 s | 26.3 s | 0 | 0 | no | 8.7 s | 27.7 s |
| `primary-sigkill` | default / off | pass | 25.2 s | 29.4 s | 29.2 s | 0 | 1 | no | - | 7.6 s |
| `replica-loss` | default / off | pass | - | 0.0 s | 0.0 s | 0 | 0 | no | - | 3.1 s |
| `sync-mode-primary-sigkill` | default / on | pass | 34.8 s | 37.3 s | 37.2 s | 0 | 1 | no | - | 8.6 s |
| `sync-replica-loss-strict` | default / strict | pass | - | 0.0 s | 6.3 s | 0 | 0 | no | - | 2.0 s |
| `sync-replica-loss` | default / on | pass | - | 0.0 s | 6.2 s | 0 | 0 | no | - | 3.0 s |
<!-- results:end -->

The table below is a hand-picked full run on one laptop (12 CPUs, Docker Desktop), 50 writes/s, kept for the notes. `default` profile is ttl 30 / loop_wait 10 / retry_timeout 10; `fast` is 20 / 5 / 5.

| Scenario | Profile / sync | Failover | Detection | RTO write | Commit stall | Lost acked | Unknown | Split brain | Fenced | Rejoin |
|---|---|---|---|---|---|---|---|---|---|---|
| `primary-sigkill` | default / async | yes | 25.2 s | 29.4 s | – | 0 | 1 | no | – | 7.6 s |
| `primary-clean-stop` | default / async | yes | 1.9 s | 5.6 s | – | 0 | 0 | no | – | 2.0 s |
| `primary-partition` | default / async | yes | 22.7 s | 26.4 s | – | 0 | 0 | no | 8.7 s | 27.7 s |
| `replica-loss` | default / async | no | – | 0.0 s | – | 0 | 0 | no | – | 3.1 s |
| `planned-switchover` | default / async | yes | 8.4 s | 10.6 s | – | 0 | 0 | no | – | – |
| `haproxy-restart` | default / async | no | – | 6.3 s | – | 0 | 0 | no | – | – |
| `etcd-one-member-loss` | default / async | no | – | 0.0 s | – | 0 | 0 | no | – | – |
| `etcd-quorum-loss` | default / async | no | – | 54.1 s ¹ | – | 0 | 0 | no | 24.7 s | – |
| `primary-frozen` | default / async | yes | 30.7 s | 33.9 s | – | 0 | 0 | **yes, 1 round** (reported) | 44.6 s ² | 11.0 s |
| `sync-mode-primary-sigkill` | default / **sync** | yes | 34.8 s | 37.3 s | – | 0 | 1 | no | – | 8.6 s |
| `sync-replica-loss` | default / sync | no | – | 0.0 s | **6.2 s** | 0 | 0 | no | – | 3.0 s |
| `sync-replica-loss-strict` | default / strict | no | – | 0.0 s | **6.3 s** | 0 | 0 | no | – | 2.0 s |
| `fast-profile-primary-sigkill` | **fast** / async | yes | 25.2 s | 29.3 s | – | 0 | 0 | no | – | 8.2 s |
| `lagging-replica-failover` | default / async, max lag 16 kB | **no** | – | 81.9 s ³ | – | 0 | 1 | no | – | – |
| `lagging-replica-stale-optime` | default / async, max lag 16 kB | yes | 40.1 s | 42.7 s | – | **8** (reported; 109 in another run) | 0 | no | – | – |
| `double-fault` | default / async | yes | 34.4 s | 38.0 s | – | 0 | 0 | no | – | 7.9 s |

¹ etcd was down for 20 s on purpose; the outage lasts as long as the DCS is gone. ² Measured from the freeze; the node was frozen for 37 s and fenced 0.3 s after the thaw. ³ Leaderless by design for 60 s until the primary was restarted.

**Default vs fast, async vs sync** (`primary-sigkill` under each setting; ranges over all runs today):

| | Detection | RTO write | Lost acked commits |
|---|---|---|---|
| `default`, async | 23–30 s | 29–37 s | 0 in 12 runs, not guaranteed |
| `fast`, async | 19–25 s | 22–29 s | 0, not guaranteed |
| `default`, sync | 29–35 s | 32–37 s | 0, **guaranteed** |

Sync mode does not change how long the failover takes; it changes what you can promise. The fast profile saves about 5–8 s on detection and nothing on the promotion itself. The most valuable single tuning for this workload is not in either profile: `wal_retrieve_retry_interval = 1s` would cut up to 8 s of read-only window after promotion.

"Commit stall" is the longest gap between two acked writes over the run; it is how a lost sync standby shows up, because it produces no errors at all.

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
make chaos-all                     # every scenario tagged core, about 7 minutes
make chaos-all TAG=core,advanced   # the full suite, about 20 minutes
leaderfall list                    # what is available
leaderfall run <name>              # one scenario
```

Each run writes `reports/<timestamp>-<scenario>/` with `result.json`, the event timeline, and the raw ledger, poller rounds and read samples, so any number can be recomputed. A suite run adds `reports/<timestamp>-suite-<tag>/suite.json`.

Needs Docker (with Compose v2) and Python 3.12+. Nothing else.

`make up` builds the Patroni image, starts 3 x etcd, 3 x PostgreSQL + Patroni and HAProxy, and returns only when there is one leader and two streaming replicas on the same timeline and HAProxy routes writes to the primary. From zero this takes about 20 seconds on a laptop once the image is built.

Writes go to `localhost:5000`, reads to `localhost:5001`, the HAProxy stats page is at `http://localhost:7000/`.

### Profiles and sync mode

```
leaderfall up --profile fast          # ttl 20 / loop_wait 5 / retry_timeout 5 (default: 30/10/10)
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

Faults: `kill`, `stop`, `pause`, `partition`, `switchover`, `restart_service`, `netem`, `fill_disk`. Actions: `start`, `unpause`, `heal`, `netem_clear`, `free_disk`, `write_wal`. Waits: `new_leader`, `writes_ok`, `cluster_healthy`, `node_streaming`, `node_fenced`, `no_leader`. Targets: `primary`, `sync_replica`, `any_replica`, `failed_node`, `failed_node_2`, `old_primary`, `haproxy`, `etcd-1..3`, or a node name.

| Scenario | Tag | What breaks | Runbook |
|---|---|---|---|
| `primary-sigkill` | core, smoke | primary dies hard | [runbook](docs/runbooks/primary-sigkill.md) |
| `primary-clean-stop` | core | primary stops cleanly | [runbook](docs/runbooks/primary-clean-stop.md) |
| `primary-partition` | core | primary cut off the network, then healed | [runbook](docs/runbooks/primary-partition.md) |
| `replica-loss` | core | one replica dies | [runbook](docs/runbooks/replica-loss.md) |
| `planned-switchover` | core | operator moves the leader | [runbook](docs/runbooks/planned-switchover.md) |
| `haproxy-restart` | core | the proxy restarts | [runbook](docs/runbooks/haproxy-restart.md) |
| `etcd-one-member-loss` | advanced | one etcd member dies | [runbook](docs/runbooks/etcd-one-member-loss.md) |
| `etcd-quorum-loss` | advanced | two etcd members die | [runbook](docs/runbooks/etcd-quorum-loss.md) |
| `primary-frozen` | advanced | primary frozen past ttl, then thawed | [runbook](docs/runbooks/primary-frozen.md) |
| `sync-mode-primary-sigkill` | advanced, sync | primary dies with sync replication | [runbook](docs/runbooks/sync-mode-primary-sigkill.md) |
| `sync-replica-loss` / `-strict` | advanced, sync | the sync standby dies | [runbook](docs/runbooks/sync-replica-loss.md) |
| `fast-profile-primary-sigkill` | advanced | primary dies, fast profile | [runbook](docs/runbooks/fast-profile-primary-sigkill.md) |
| `lagging-replica-failover` | advanced | replicas far behind, primary dies | [runbook](docs/runbooks/lagging-replica-failover.md) |
| `lagging-replica-stale-optime` | advanced | same, within one loop_wait | [runbook](docs/runbooks/lagging-replica-failover.md) |
| `double-fault` | advanced | primary and a replica die | [runbook](docs/runbooks/double-fault.md) |
| `wal-disk-full` | bounded-wal | the WAL volume fills | [runbook](docs/runbooks/wal-disk-full.md) |

## Development

```
make install                        # venv + dependencies
make lint                           # ruff + mypy
make test                           # unit tests, no Docker needed
.venv/bin/pytest -m integration     # checks against a running cluster
```

## License

Apache-2.0. See [LICENSE](LICENSE).
