# sync-mode-primary-sigkill

**What breaks:** the primary dies hard, exactly like `primary-sigkill`, but the cluster runs with `synchronous_mode: on`. Every `COMMIT` the client was told about was flushed on the sync standby first.

**Scenario file:** [`scenarios/sync-mode-primary-sigkill.yml`](../../scenarios/sync-mode-primary-sigkill.yml)

## What you see

The same as `primary-sigkill`, with one difference in `patronictl list`: one replica has the role `sync_standby`, and after the failover it is the one that becomes leader. Patroni only promotes the sync standby in synchronous mode; the other replica is not a candidate, however up to date it is.

## What the cluster does by itself

1. Lease expires, the sync standby takes the lock and promotes.
2. It has everything the client was ever acknowledged, by construction: the primary did not answer `COMMIT` before the standby flushed the WAL.
3. Patroni picks the remaining replica as the new sync standby (`synchronous_standby_names` changes on the new leader).

## Measured

| Metric | async (`primary-sigkill`) | sync (this scenario) |
|---|---|---|
| Detection | 23–30 s | 28.8 s |
| RTO write | 29–37 s | 32.1 s |
| Lost acked commits | 0 in every run so far, **not guaranteed** | 0, **guaranteed** (SLO: 0) |
| Unknown commits | 0–1 | 0 |
| Rejoin | 5–8 s | 6.9 s |

Failover time is the same. The price of sync mode is paid on every commit, not at failover: each `COMMIT` waits for a network round trip and a flush on the standby. At this lab's load (50 small writes/s over a local Docker network) the difference is not visible; on a real network it is the standby's distance in milliseconds.

## What a DBA should do in production

- **Use sync mode when a lost commit costs more than a slower commit.** Payments, orders, anything a customer was told succeeded.
- **Watch `synchronous_standby_names`.** After a failover it must name a live replica. `patronictl list` shows the `sync_standby` role.
- **Decide what happens when the sync standby dies.** That is the next scenario, `sync-replica-loss`, and the difference between `on` and `strict`.
