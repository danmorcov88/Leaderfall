# double-fault

**What breaks:** the primary dies, and 10 s later, before the election has happened, one replica dies too. One node is left.

**Scenario file:** [`scenarios/double-fault.yml`](../../scenarios/double-fault.yml)

## What you see

The surviving replica waits out the lease like in `primary-sigkill`, then takes the lock and promotes. `patronictl list` shows one member. HAProxy has one server UP in the write backend and, the moment that node is promoted, none in the read backend.

## What the cluster does by itself

1. Lease expires; the last node promotes. There is no quorum concept among PostgreSQL nodes; Patroni's quorum is etcd's, which is intact.
2. Reads: the read frontend falls back to the primary when `nbsrv(replicas)` is 0, so `:5001` keeps answering, from the primary.
3. When the two dead nodes are started, both rejoin: the old primary through `pg_rewind`, the replica by streaming from where it stopped.

## Measured

| Metric | Value |
|---|---|
| Failover | yes |
| Detection | 36.9 s |
| RTO write | 38.6 s |
| RTO read | 0.2 s, with 197 reads served by the primary |
| Lost acked commits | 0 |
| Rejoin of the old primary | 7.6 s |

**A finding that changed the config.** The first run had a 16.7 s read outage: the last node was promoted, `/replica` on it started returning 503, HAProxy emptied the read backend, and every read failed until a replica came back. The read frontend now has `use_backend primary if nbsrv(replicas) eq 0`. With it, reads move to the primary within one health-check interval and the outage is 0.2 s.

## What a DBA should do in production

- **This is your last node.** A third failure is a full outage with no automatic recovery. Rebuild capacity before anything else.
- **Give reads a fallback.** A read replica pool that goes empty when the last replica is promoted is a common self-inflicted outage. Fall back to the primary (as here) or fail the reads on purpose, but decide.
- **In sync mode this is different.** Patroni only promotes a member listed in the DCS `sync` key. With `synchronous_mode: on` and the sync standby among the dead, the survivor is not a candidate. Not measured in this lab yet; a three-node sync cluster should be assumed to survive one failure, not two.
