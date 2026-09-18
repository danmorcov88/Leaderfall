# haproxy-restart

**What breaks:** the proxy in front of the cluster restarts (`docker restart`, SIGTERM then SIGKILL after 5 s). The database is not involved at all. This is a config reload gone wrong, an upgrade, or the proxy host rebooting.

**Scenario file:** [`scenarios/haproxy-restart.yml`](../../scenarios/haproxy-restart.yml)

## What you see

**Clients** keep working for a while after the restart is requested. HAProxy does a *soft stop* on SIGTERM: it closes its listening sockets but keeps existing sessions alive until they end. The writer's long-lived connection kept committing for the whole 5 s grace period and only failed when Docker killed the process. Then "connection refused" for about a second, until the new process is up and its first health checks pass.

**Patroni** logs nothing. **HAProxy** logs its startup and `Server primary/pg-3 is UP` once the first check on each backend passes.

## What the cluster does by itself

Nothing. There is no failover; the timeline does not change.

## Measured

| Metric | Value |
|---|---|
| Failover | none |
| RTO write (from the restart request) | 6–7 s |
| Write gap felt by a connected client | 1–1.6 s |
| Lost acked commits | 0 |

The two numbers differ because the outage starts when Docker gives up waiting, not when the restart is requested.

**A finding that changed the config.** In the first run, one write right after the restart failed with `cannot execute INSERT in a read-only transaction`: HAProxy had sent it to a replica. Before the first health check, HAProxy assumes every server is UP, so for a moment `:5000` round-robined across all three nodes. The fix is `init-state down` on the servers (HAProxy 3.x): a server is DOWN until its first check passes. With it, zero writes reach a replica.

## What a DBA should do in production

- **Reload, do not restart.** `haproxy -sf <pid>` / `systemctl reload haproxy` starts a new process that takes over the sockets; sessions are not dropped.
- **Run two proxies.** A single HAProxy is a single point of failure for the whole cluster. Two with keepalived and a VIP, or a load balancer in front, is the normal setup. (Listed as future work for this lab.)
- **Set `init-state down`** (or the equivalent in your proxy) so a fresh proxy never routes writes to a standby.
- **Keep client timeouts short.** A client that hangs on a dead proxy socket for 30 s turns a 1 s blip into a 30 s outage. `connect_timeout=2`, TCP keepalives and a statement timeout are the minimum.
