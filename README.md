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

Early development. The cluster and the chaos harness are being built phase by phase. See [docs/adr](docs/adr) for the design decisions taken so far.

## Quick start

```
git clone https://github.com/danmorcov88/Leaderfall.git
cd Leaderfall
make up      # start the cluster and wait until it is healthy
make chaos   # run the smoke scenario and write a report
```

Needs Docker (with Compose v2) and Python 3.12+. Nothing else.

## Development

```
make install   # venv + dependencies
make lint      # ruff + mypy
make test      # unit tests
```

## License

Apache-2.0. See [LICENSE](LICENSE).
