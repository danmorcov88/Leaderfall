# Runbooks

One page per scenario. Each page says what breaks, what you see in Patroni, HAProxy and the logs, what the cluster does by itself, what the harness measured, and what a DBA should do when it happens in production.

The numbers are from real runs on one laptop (12 CPUs, Docker Desktop), async replication and the `default` profile (ttl 30 / loop_wait 10 / retry_timeout 10) unless the scenario says otherwise, 50 writes/s. They move by a few seconds between runs; the ranges are in the text.

## Core

| Scenario | What breaks | Failover | RTO write | Page |
|---|---|---|---|---|
| `primary-sigkill` | Primary process dies hard | yes | ~29–37 s | [primary-sigkill.md](primary-sigkill.md) |
| `primary-clean-stop` | Primary is stopped cleanly | yes | ~5–7 s | [primary-clean-stop.md](primary-clean-stop.md) |
| `primary-partition` | Primary cut off the network | yes | ~30–33 s | [primary-partition.md](primary-partition.md) |
| `replica-loss` | A replica dies | no | 0 | [replica-loss.md](replica-loss.md) |
| `planned-switchover` | Operator moves the leader | yes | ~5–10 s | [planned-switchover.md](planned-switchover.md) |
| `haproxy-restart` | The proxy restarts | no | ~6–7 s | [haproxy-restart.md](haproxy-restart.md) |

## Advanced

| Scenario | What breaks | Failover | Headline | Page |
|---|---|---|---|---|
| `etcd-one-member-loss` | One etcd member dies | no | not one write failed | [etcd-one-member-loss.md](etcd-one-member-loss.md) |
| `etcd-quorum-loss` | Two etcd members die | no (until etcd returns) | primary fenced after 28.8 s | [etcd-quorum-loss.md](etcd-quorum-loss.md) |
| `primary-frozen` | Primary frozen longer than ttl, then thawed | yes | split brain for one Patroni loop; the watchdog limit | [primary-frozen.md](primary-frozen.md) |
| `sync-mode-primary-sigkill` | Primary dies, sync mode on | yes | 0 lost commits, guaranteed | [sync-mode-primary-sigkill.md](sync-mode-primary-sigkill.md) |
| `sync-replica-loss` (+ `-strict`) | The sync standby dies | no | 6.2 s commit stall, no errors | [sync-replica-loss.md](sync-replica-loss.md) |
| `fast-profile-primary-sigkill` | Primary dies, fast profile | yes | Patroni's ttl floor is 20 s | [fast-profile-primary-sigkill.md](fast-profile-primary-sigkill.md) |
| `lagging-replica-failover` | Replicas far behind, primary dies | no | leaderless until the primary is back | [lagging-replica-failover.md](lagging-replica-failover.md) |
| `lagging-replica-stale-optime` | Same, primary dies within one loop | yes | 109 acked commits lost | [lagging-replica-failover.md](lagging-replica-failover.md) |
| `double-fault` | Primary and a replica die | yes | reads fall back to the primary | [double-fault.md](double-fault.md) |
| `wal-disk-full` | Primary's WAL volume fills | yes | needs a bounded volume; not runnable here | [wal-disk-full.md](wal-disk-full.md) |
