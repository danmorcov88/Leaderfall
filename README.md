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

Early development. The cluster is done; the chaos harness is being built phase by phase. See [docs/architecture.md](docs/architecture.md) for what runs where and [docs/adr](docs/adr) for the design decisions.

## Quick start

```
git clone https://github.com/danmorcov88/Leaderfall.git
cd Leaderfall
make up      # start the cluster and wait until it is healthy
make status  # topology: node, role, state, timeline, lag
make chaos   # run the smoke scenario and write a report (Phase 2)
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
