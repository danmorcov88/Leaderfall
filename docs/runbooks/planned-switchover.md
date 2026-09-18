# planned-switchover

**What breaks:** nothing. An operator asks Patroni to move the leader role to another node (`POST /switchover`, the same as `patronictl switchover`). This is what maintenance should look like.

**Scenario file:** [`scenarios/planned-switchover.yml`](../../scenarios/planned-switchover.yml)

## What you see

**Patroni** on the leader:

```
INFO: received switchover request with leader=pg-1 candidate=None scheduled_at=None
INFO: switchover: demoting myself
INFO: Demoting self (graceful)
LOG:  received fast shutdown request
INFO: Leader key released
INFO: switchover: demote in progress
```

Then on the candidate: `promoted self to leader by acquiring session lock`. The old leader starts again as a replica by itself. No `pg_rewind` is needed, because it shut down cleanly before the candidate promoted.

**HAProxy.** `primary/pg-1 is DOWN` (Patroni stops answering `/primary` the moment it demotes), then `primary/pg-3 is UP` two checks later.

## What the cluster does by itself

1. Patroni checks that a candidate is healthy and in sync.
2. The leader demotes: fast shutdown, key released.
3. The candidate promotes.
4. The old leader restarts as a replica of the new one.

## Measured

| Metric | Value |
|---|---|
| Detection | 2–7 s |
| RTO write | 5–10 s |
| Read-only window | 0–0.1 s (a handful of writes) |
| Lost acked commits | 0 (SLO: 0) |
| Unknown commits | 0 |

The variance in detection comes from where in its 10 s loop the leader is when the request arrives. Even here a few writes reached the new primary before PostgreSQL finished the promotion (the read-only window). It is the same mechanism as in `primary-sigkill`, just short, because the candidate's WAL receiver was already idle.

## What a DBA should do in production

- **Use it for every planned change:** kernel updates, minor PostgreSQL upgrades, hardware moves. `patronictl switchover --leader <node> --candidate <node>`.
- **Schedule it.** `patronictl switchover --scheduled "2026-09-19T02:00:00"` puts the switchover in the DCS; Patroni runs it at that time.
- **Expect 5–10 s of write errors.** Applications must reconnect and retry. Connection poolers with short `connect_timeout` help.
- **Pick the candidate.** Without one, Patroni chooses the most up-to-date replica. With synchronous mode on, it can only be the sync standby.
