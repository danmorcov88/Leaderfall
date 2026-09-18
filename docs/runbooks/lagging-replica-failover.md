# lagging-replica-failover and lagging-replica-stale-optime

**What breaks:** both replicas fall far behind, then the primary dies. `maximum_lag_on_failover` (set to 16 kB for these scenarios) says a replica that far behind must not become leader. What Patroni does depends on a detail most people do not know.

Two scenarios: [`lagging-replica-failover`](../../scenarios/lagging-replica-failover.yml) proves the setting works; [`lagging-replica-stale-optime`](../../scenarios/lagging-replica-stale-optime.yml) shows when it cannot.

## How the lag is made

The plan was `tc netem` delay on the replicas. The Docker Desktop kernel (WSL2) does not ship the `sch_netem` module, so the `netem` fault only works on a Linux host or a CI runner. The scenarios use a portable method instead: both replicas are frozen (`docker pause`), and the primary writes 16 MB of WAL with one bulk insert. A frozen replica's kernel still fills its TCP receive buffer, but 16 MB is far more than a buffer holds, so when the replicas thaw they are megabytes behind, and the WAL they miss is only on the primary. Then the primary is killed.

## What Patroni compares

Patroni does not ask the dead primary how far ahead it was; it cannot. Each replica compares its own WAL position with the leader's LSN **as last written to the DCS** (the `optime` in the `/status` key), which the leader updates once per loop (`loop_wait`, 10 s). `maximum_lag_on_failover` is checked against that number.

So the guard sees the lag only if the leader had a chance to publish its LSN after writing the WAL.

## lagging-replica-failover: the guard works

Steps: freeze both replicas, write 16 MB, **wait 12 s** (one loop: the leader publishes its LSN), kill the primary, thaw the replicas, wait 60 s.

```
INFO: following a different leader because i am not the healthiest node
```

Neither replica takes the lock. `patronictl list` shows two replicas and no leader, for as long as it takes. Writes are down; reads work. When the old primary is started, it takes the lock back (it has the highest LSN and nobody else is eligible), the replicas stream the missing 16 MB from it, and nothing is lost.

| Metric | Value |
|---|---|
| Failover | none (SLO: none) |
| Leaderless | 60 s, by design, until the primary returned |
| RTO write | 81 s: the outage lasts until someone brings the primary back |
| Lost acked commits | 0 (SLO: 0) |

## lagging-replica-stale-optime: the guard is blind

Same steps **without the 12 s wait**: the primary is killed within a second of the bulk write. The DCS still holds the LSN from before the write. Both replicas have drained their TCP buffers past that stale number, so both look "not lagging".

| Metric | Value |
|---|---|
| Failover | yes: a replica missing ~16 MB was promoted |
| Detection / RTO write | 39.1 s / 42.7 s |
| Lost acked commits | **109 of 1732** |

The 109 rows were acknowledged to the client by the old primary and never reached a replica. After the old primary is restarted it runs `pg_rewind` and throws them away.

## What a DBA should do in production

- **`maximum_lag_on_failover` protects against long-standing lag, not against a burst.** Anything written in the last `loop_wait` seconds is invisible to it. Do not use it as a data-loss guarantee; that is what synchronous mode is for.
- **Alert on replica lag** independently of Patroni. A replica minutes behind is a failover that will lose data or a failover that will not happen. Both are incidents.
- **Decide what a leaderless cluster costs you.** With the guard active and no eligible replica, Patroni waits for a human. `patronictl failover --candidate <node>` promotes a lagging replica if you accept the loss.
- **Bulk loads before maintenance are a trap.** Load, wait for the replicas to catch up, then switch over.
