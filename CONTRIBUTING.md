# Contributing

## Set up

```
make install     # venv + dependencies
make lint        # ruff + mypy, must be clean
make test        # unit tests, no Docker needed
make up          # the cluster; then `.venv/bin/pytest -m integration`
```

Python 3.12, type hints everywhere, `mypy --strict`. No fixed sleeps in the harness: every wait polls a real condition with a timeout. All timings come from the host's monotonic clock. Image tags and Python dependencies are pinned.

## Add a scenario

A scenario is one YAML file in `scenarios/`. The file name must equal the `name` field.

```yaml
name: replica-clean-stop
description: Stop one replica cleanly; writes must not notice.
tags: [advanced]
cluster:
  patroni_profile: default        # default | fast
  synchronous_mode: false         # false | true | on | strict
  # maximum_lag_on_failover: 1048576
workload:
  rate_per_sec: 50
  warmup_sec: 15
steps:
  - fault: stop
    target: any_replica
    grace_sec: 30
  - hold_sec: 20
  - action: start
    target: failed_node
  - wait_for: node_streaming
    timeout_sec: 120
expect:
  failover: false
  rto_write_sec_max: 1
  lost_acked_commits_max: 0
  detection_sec_max: null         # null disables a limit inherited from slo.yml
```

Steps, in order:

| Step | Fields | What it does |
|---|---|---|
| `fault: <name>` | `target`, plus `signal`, `grace_sec`, `netem`, `candidate` where relevant | Injects a fault. The first `fault` sets the fault time and the "failed node" that `failed_node` refers to. |
| `action: <name>` | same as fault; `write_wal` takes `mb` and no target | Anything done on purpose that is not the fault under test: `start`, `unpause`, `heal`, `netem_clear`, `free_disk`, `write_wal`, or a second `kill`/`stop`/`pause`/`partition`. `start`/`unpause`/`heal` on the failed node is the recovery the rejoin metric is measured from. |
| `wait_for: <condition>` | `timeout_sec`, `target` for the node conditions, `max_lag_bytes` | `new_leader`, `writes_ok`, `cluster_healthy`, `node_streaming`, `node_fenced`, `no_leader`. A timeout fails the scenario. |
| `hold_sec: N` | | Keep the workload running for N seconds. Use it when the scenario needs time to pass, not to wait for something. |

Targets: `primary`, `sync_replica`, `any_replica` (lowest-named replica not hit yet), `failed_node`, `failed_node_2`, `old_primary`, `haproxy`, `etcd-1..3`, or `pg-1..3`.

`expect` holds the limits from `slo.yml` that this scenario overrides; the full list is in `src/leaderfall/slo.py` (`Limits`). Set a key to `null` to report the value without enforcing it, and say why in a comment.

Then:

1. `.venv/bin/leaderfall run <name>` until it does what you meant. Read `reports/<timestamp>-<name>/rounds.jsonl` and `ledger.jsonl` around the fault before trusting the numbers; most measurement bugs in this project were found there.
2. Run it three times. If it is flaky, find out why and write it down; do not widen a limit to make it pass.
3. Write `docs/runbooks/<name>.md`: what breaks, what you see in Patroni, HAProxy and the logs (real lines, from the containers), what the cluster does by itself, what you measured, what a DBA should do in production. Add it to `docs/runbooks/README.md` and to the table in `README.md`.
4. `make lint test`. `tests/test_scenario.py` checks that every shipped scenario parses and has a known tag.

Tags: `core` runs in `make chaos-all` and in the smoke job (`smoke`); `advanced` runs nightly with `core`; anything else is opt-in.

## Add a fault primitive

Add a function in `src/leaderfall/faults.py` that takes the `Clock` and a `Target`, returns a `FaultTiming` (request and confirm times), and never sleeps. Register its name in `FAULTS` or `ACTIONS` in `src/leaderfall/scenario.py`, dispatch it in `_do_fault_or_action`, and if it needs undoing when a run aborts, teach `_recover` about it. Faults that need kernel features or privileges the PostgreSQL containers do not have should refuse clearly, the way `netem` and `fill_disk` do, not fake it.

## Commits and pull requests

Small commits, Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`). One branch per change, CI green (lint, unit tests, and the smoke job that runs `primary-sigkill` on a real cluster) before merging. Numbers in docs come from real runs; say which run and on what machine.
