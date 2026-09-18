# primary-partition

**What breaks:** the primary is cut off the network (`docker network disconnect`). It keeps running, but it cannot reach etcd, the replicas or HAProxy, and nobody can reach it. This is a switch failure, a bad firewall push, or a VM whose NIC died.

This is the scenario that tests the promise "no split brain".

**Scenario file:** [`scenarios/primary-partition.yml`](../../scenarios/primary-partition.yml)

## What you see

**On the isolated primary**, Patroni cannot renew its lease:

```
ERROR: Request to server http://etcd-2:2379 failed: ... Network is unreachable
INFO: Reconnection allowed, looking for another server.
ERROR: Error communicating with DCS
INFO: demoting self because DCS is not accessible and I was a leader
INFO: Demoting self (offline)
LOG:  received fast shutdown request
```

It shuts PostgreSQL down. Later, still with no DCS, Patroni starts PostgreSQL again as a read-only standby, so that reads could continue if anyone could reach it. It stays that way until the network is back.

**On the other side**, the replicas see the leader key expire, exactly like in `primary-sigkill`, and elect a new leader.

**After healing**, the old primary reconnects to etcd, sees a different leader on a newer timeline, and runs `pg_rewind`:

```
pg_rewind: servers diverged at WAL location 0/4581958 on timeline 19
pg_rewind: Done!
LOG:  started streaming WAL from primary at ... on timeline 20
```

## What the cluster does by itself

Two clocks run at the same time, and the order between them is what matters:

1. The isolated primary stops accepting writes after `retry_timeout` (10 s) plus up to one `loop_wait`: **12–15 s** after the partition.
2. The replicas elect a new leader after the lease expires: **27–30 s** after the partition.

Because 1 happens before 2, there is never a moment with two writable primaries. This is not luck. Patroni's rule is that a primary that cannot talk to the DCS for `retry_timeout` must give up its role, and `retry_timeout` is much shorter than `ttl`. The safety margin in the default profile is roughly `ttl - retry_timeout - loop_wait` = 10 s.

## Measured

| Metric | Value |
|---|---|
| Old primary stops taking writes (demotion) | 12.2–15.2 s |
| Detection (new leader) | 27–30 s |
| RTO write | 30–33 s |
| Two writable primaries in the same poll round | never |
| Lost acked commits | 0 |
| Rejoin after heal | 20–23 s (`pg_rewind` + restart) |

How the harness knows: the poller keeps probing the isolated node through `docker exec`, because its published port dies with the network endpoint. It saw the last successful probe write 12 s after the partition, then "connection refused" while PostgreSQL was down, then `pg_is_in_recovery() = true`.

## What a DBA should do in production

- **Trust the fence, but know its size.** The window in which an isolated primary can still accept writes from clients on *its* side of the partition is `retry_timeout` + `loop_wait`. If that is too long, shorten both (the `fast` profile: 5/5). Never make `ttl` smaller than `loop_wait + retry_timeout * 2`; Patroni refuses it for a reason.
- **No watchdog in Docker.** In a real deployment, enable Patroni's watchdog (`/dev/watchdog`, softdog). It resets the whole node if Patroni itself hangs and cannot demote. Containers cannot use it, so this lab only proves the software path, not the hardware one. See the README.
- **Do not rush to heal.** When the network comes back, Patroni handles the rejoin. If `pg_rewind` fails, `patronictl reinit`.
- **Look at `failsafe_mode`.** Leaderfall runs with `failsafe_mode: false` (the Patroni default). With it on, a primary that loses the DCS but can still reach all replicas over REST keeps running. That changes this scenario completely; it is a Phase 4 comparison.
