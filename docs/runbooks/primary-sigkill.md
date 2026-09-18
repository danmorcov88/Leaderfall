# primary-sigkill

**What breaks:** the PostgreSQL primary and its Patroni agent die at once (`docker kill -s SIGKILL`). No shutdown, no goodbye. This is what a kernel panic, an OOM kill of the container, or a pulled power cable looks like.

**Scenario file:** [`scenarios/primary-sigkill.yml`](../../scenarios/primary-sigkill.yml)

## What you see

**Clients.** Every write fails at once. The connection through HAProxy is reset (`server closed the connection unexpectedly`). New connections fail for the next 2–4 seconds while HAProxy still lists the dead node as UP (`inter 2s fall 2`), then HAProxy refuses them: no server is UP on `:5000`. Reads on `:5001` keep working; they never touched the primary.

**Patroni.** The two replicas keep logging `no action. I am (pg-2), a secondary, and following a leader (pg-1)` for a while. Nothing happens until the leader key in etcd expires. The key has a TTL of `ttl` (30 s) and was last refreshed up to `loop_wait` (10 s) before the kill, so the replicas notice the loss between 20 and 30 s after the fault, then take up to one more loop to act. Then one of them wins the race:

```
INFO: promoted self to leader by acquiring session lock
LOG:  received promote request
```

**HAProxy.** `Server primary/pg-1 is DOWN, reason: Layer4 connection problem` a few seconds after the kill; `Server primary/pg-2 is UP` two checks after Patroni starts answering `/primary` with 200.

## What the cluster does by itself

1. The replicas wait for the leader lease to expire (up to `ttl`).
2. The healthiest replica (most WAL received, within `maximum_lag_on_failover`) takes the lock and promotes. PostgreSQL picks a new timeline.
3. HAProxy sees `/primary` return 200 on the new node and routes writes to it.
4. When the old node is started again, Patroni sees that its timeline diverged, runs `pg_rewind` against the new primary, and starts it as a streaming replica. Any rows the old primary committed that never reached the new one are gone.

## Measured

Five runs in Phase 2, plus the Phase 3 suite run:

| Metric | Range | Notes |
|---|---|---|
| Detection (Patroni shows a new leader) | 23–30 s | bounded by `ttl` + `loop_wait` |
| RTO write | 29–37 s | detection + promotion + 2 HAProxy checks |
| Read-only window | 0–5 s | see below |
| Lost acked commits | 0 in all runs | async: not guaranteed |
| Unknown commits | 0–1 per run | one `COMMIT` in flight at the kill |
| Split brain | never | the dead node cannot write |
| Rejoin after `docker start` | 5–8 s | `pg_rewind` runs every time |

**The read-only window is the surprise.** Patroni takes the leader lock and answers `/primary` with 200 as soon as it sends the promote request. HAProxy routes writes there at once. But the PostgreSQL startup process only acts on the promote when it wakes from its WAL-receiver retry loop (`wal_retrieve_retry_interval`, 5 s by default). Until then the node is still a standby and every write fails with `cannot execute INSERT in a read-only transaction`. In the worst run this was 5.1 s and 231 writes. It is the main reason RTO varies by 8 s between runs with identical settings.

**Unknown commits are real.** In one run a `COMMIT` was sent, the connection died, and the row was not there afterwards. An application that retries such a write without an idempotency key will duplicate it; one that does not retry will lose it.

## What a DBA should do in production

- **Nothing, for the failover itself.** That is the point of Patroni. Do not promote by hand while Patroni is running; you will race it.
- **Watch the timeline.** After the failover, `patronictl list` shows the new timeline. The old node must come back on the same timeline after `pg_rewind`. If `pg_rewind` fails (usually `wal_log_hints` or checksums off, or the WAL it needs is gone), the node has to be re-initialised: `patronictl reinit <cluster> <node>`.
- **Check for lost commits.** In async mode, anything the old primary acknowledged after the last WAL the new primary received is lost. `pg_rewind` reports the divergence LSN in the Patroni log. If the application cannot tolerate that, run synchronous mode (see the sync scenarios).
- **Make the read-only window shorter** if 5 s matters to you: set `wal_retrieve_retry_interval` to 1 s in `postgresql.parameters`. Leaderfall keeps the PostgreSQL default on purpose, so the baseline numbers are what you get out of the box.
- **Tell the application about `unknown`.** Every client must treat "connection lost after COMMIT was sent" as *unknown*, not as *failed*, and check before retrying.
