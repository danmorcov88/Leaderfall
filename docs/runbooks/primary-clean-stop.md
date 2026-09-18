# primary-clean-stop

**What breaks:** the primary container is stopped the polite way (`docker stop`, SIGTERM). This is a planned host reboot, a `systemctl stop patroni`, or an orchestrator draining a node.

**Scenario file:** [`scenarios/primary-clean-stop.yml`](../../scenarios/primary-clean-stop.yml)

## What you see

**Patroni** on the stopping node shuts PostgreSQL down cleanly:

```
LOG:  received fast shutdown request
LOG:  shutting down
LOG:  checkpoint starting: shutdown immediate
LOG:  checkpoint complete: ... lsn=0/49AB858, redo lsn=0/49AB858
LOG:  database system is shut down
```

Patroni does not log it, but on the way out it also deletes its leader key from etcd. The proof is in the timing: the replicas saw the key vanish and promoted 1.6 s after the stop was requested. There is no 30 s lease to wait out.

**Clients** see a burst of failures for a few seconds while the shutdown, the election and the HAProxy checks happen.

## What the cluster does by itself

1. Patroni stops PostgreSQL with a fast shutdown. All WAL is flushed and the replicas receive it, so nothing is lost.
2. Patroni deletes the leader key from etcd.
3. A replica takes the lock and promotes.
4. The old node comes back as a replica when started. `pg_rewind` runs if the timelines diverged (the old primary wrote a shutdown checkpoint after the last WAL the new primary replayed), but it has very little to do.

## Measured

| Metric | Value |
|---|---|
| Detection | 1.6–1.7 s |
| RTO write | 5.5–6.5 s |
| Lost acked commits | 0 (and the SLO requires 0) |
| Unknown commits | 0 |
| Rejoin | 2–3 s |

Compare with `primary-sigkill`: a clean stop is about five times faster to recover from and cannot lose acked data. The difference is entirely the leader-lease wait.

## What a DBA should do in production

- **Prefer a switchover.** If you know you are going to stop the primary, `patronictl switchover` first (see `planned-switchover`). It is even cleaner: Patroni picks the candidate and waits for it to be in sync.
- **Stop Patroni, not PostgreSQL.** Stopping PostgreSQL under Patroni makes Patroni try to restart it. Stop the Patroni service; it stops PostgreSQL for you.
- **Give the shutdown time.** Patroni waits for the fast shutdown, which waits for the checkpoint. `docker stop` here uses a 60 s grace. A 10 s grace followed by SIGKILL turns a clean stop into `primary-sigkill`.
