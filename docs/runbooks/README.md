# Runbooks

One page per scenario. Each page says what breaks, what you see in Patroni, HAProxy and the logs, what the cluster does by itself, what the harness measured, and what a DBA should do when it happens in production.

The numbers are from real runs on one laptop (12 CPUs, Docker Desktop), `default` profile (ttl 30 / loop_wait 10 / retry_timeout 10), async replication, 50 writes/s. They move by a few seconds between runs; the ranges are in the text.

| Scenario | What breaks | Failover | RTO write | Page |
|---|---|---|---|---|
| `primary-sigkill` | Primary process dies hard | yes | ~29–37 s | [primary-sigkill.md](primary-sigkill.md) |
| `primary-clean-stop` | Primary is stopped cleanly | yes | ~5–7 s | [primary-clean-stop.md](primary-clean-stop.md) |
| `primary-partition` | Primary cut off the network | yes | ~30–33 s | [primary-partition.md](primary-partition.md) |
| `replica-loss` | A replica dies | no | 0 | [replica-loss.md](replica-loss.md) |
| `planned-switchover` | Operator moves the leader | yes | ~5–10 s | [planned-switchover.md](planned-switchover.md) |
| `haproxy-restart` | The proxy restarts | no | ~6–7 s | [haproxy-restart.md](haproxy-restart.md) |
