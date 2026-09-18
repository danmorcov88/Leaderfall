# etcd-quorum-loss

**What breaks:** two of the three etcd members die (`docker kill`). The third is alive but alone: no quorum, so no request can be committed. Patroni can neither renew nor acquire a lease. This is a DCS outage: an etcd upgrade gone wrong, a network split that isolates two members, a full disk on two of them.

**Scenario file:** [`scenarios/etcd-quorum-loss.yml`](../../scenarios/etcd-quorum-loss.yml)

## What you see

**On the primary**, the lease keepalive against the surviving member does not fail; it times out, over and over, while the client also tries the dead members and fails DNS lookups:

```
ERROR: Request to server http://etcd-3:2379 failed: ReadTimeoutError(... read timeout=3.33)
ERROR: Request to server http://etcd-1:2379 failed: MaxRetryError(...)
WARNING: failed to resolve host etcd-2: [Errno -2] Name or service not known
ERROR: Error communicating with DCS
INFO: demoting self because DCS is not accessible and I was a leader
INFO: Demoting self (offline)
LOG:  received fast shutdown request
```

Then, still without a DCS, Patroni starts PostgreSQL again as a standby with no primary to follow:

```
WARNING:  specified neither "primary_conninfo" nor "restore_command"
LOG:  entering standby mode
INFO: demoted self because DCS is not accessible and I was a leader
INFO: Lock owner: pg-3; I am pg-3
```

Note the last line: with no DCS to ask, Patroni's view of the lock is frozen at "I own it". Its REST API keeps answering `/primary` with 200 for a moment, so HAProxy sends writes to a read-only node. They fail with `cannot execute INSERT in a read-only transaction` until Patroni's role catches up and the check fails. In this run that was 2.5 s and 113 writes.

**The replicas** cannot take the lock either: there is nobody to grant it. The cluster has no leader and no writes until etcd is back.

**When etcd returns**, the old primary, which has the most WAL, re-acquires the lock and promotes itself again. New timeline, no data lost.

## What the cluster does by itself

1. The primary keeps running for a while, then fences itself: `retry_timeout` per DCS operation, and each operation against a quorumless member burns a full read timeout instead of failing at once.
2. No leader anywhere. Reads work (the replicas are healthy standbys, and the read backend falls back to the primary if needed). Writes do not.
3. When quorum returns, the node with the highest LSN wins the election. Here it was the old primary every time.

## Measured

| Metric | Value |
|---|---|
| Quorum lost → old primary stops taking writes | **28.8 s** |
| Same thing after a network partition (`primary-partition`) | 12–15 s |
| Writes down | as long as etcd is down, plus ~3 s |
| Lost acked commits | 0 (SLO: 0) |
| Split brain | no: nobody can take the lock while the DCS is down |
| Write errors seen by clients while the fenced node was still "primary" for HAProxy | 113 (`read-only transaction`), 2.5 s |

The 28.8 s deserves attention. When the DCS refuses connections (partition), Patroni's retries fail fast and it demotes after ~`retry_timeout` + one loop. When one member is alive but has no quorum, it accepts the connection and lets the request time out, so the same retry budget takes three times longer to exhaust. The lease itself expires after `ttl` = 30 s; the fence landed 1.2 s before it. Nothing else could have taken the lock, so this was safe, but it is closer to the edge than the partition case.

## What a DBA should do in production

- **Treat etcd like the database.** Three members on separate failure domains, monitored, backed up (`etcdctl snapshot save`). A DCS outage is a full write outage for every Patroni cluster that uses it.
- **Restore quorum first, then look at PostgreSQL.** Patroni recovers on its own once it can talk to etcd. Do not promote by hand while the DCS is down; that is how you get two primaries when it comes back.
- **Know `failsafe_mode`.** This lab runs with it off (the Patroni default). With `failsafe_mode: on`, a primary that loses the DCS but can still reach every other member over the REST API keeps running and accepting writes. It trades this scenario's write outage for a dependency on the members' network. Decide on purpose.
- **Expect a burst of read-only errors** at the moment the primary fences itself: the proxy learns about it one health check late.
