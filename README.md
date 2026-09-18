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

Early development. The cluster and the measurement harness are done; the first scenario, `primary-sigkill`, runs end to end. The scenario engine, the other scenarios and the HTML reports come next. See [docs/architecture.md](docs/architecture.md) for what runs where and how things are measured, and [docs/adr](docs/adr) for the design decisions.

### First numbers

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

## Development

```
make install                        # venv + dependencies
make lint                           # ruff + mypy
make test                           # unit tests, no Docker needed
.venv/bin/pytest -m integration     # checks against a running cluster
```

## License

Apache-2.0. See [LICENSE](LICENSE).
