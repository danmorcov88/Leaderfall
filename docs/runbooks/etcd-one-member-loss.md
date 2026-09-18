# etcd-one-member-loss

**What breaks:** one of the three etcd members dies (`docker kill -s SIGKILL etcd-1`) and stays dead for 45 s, longer than `ttl`. The other two keep quorum.

**Scenario file:** [`scenarios/etcd-one-member-loss.yml`](../../scenarios/etcd-one-member-loss.yml)

## What you see

**Patroni.** Nodes that were talking to the dead member log one failed request and move to another:

```
ERROR: Request to server http://etcd-1:2379 failed: MaxRetryError(...)
INFO: Reconnection allowed, looking for another server.
```

That is all. Leases are renewed against the surviving members. No node loses its lock.

**Clients** notice nothing. **HAProxy** notices nothing; it never talks to etcd.

**etcd.** The two survivors elect a new Raft leader if the dead one was it; the client API keeps answering.

## What the cluster does by itself

Nothing that touches the database. When the member comes back it rejoins the Raft group from its own data directory and catches up. Patroni's etcd client refreshes its member list and starts using it again.

## Measured

| Metric | Value |
|---|---|
| Failover | none (SLO: none) |
| RTO write | 0.0 s: not one write failed in 45 s without the member |
| Lost acked commits | 0 |
| Split brain | no |

## What a DBA should do in production

- **Nothing urgent.** Replace or restart the member before a second one fails; two members down is quorum loss (see `etcd-quorum-loss`), and that one stops writes.
- **Keep etcd data on disk.** A member that restarts with an empty data directory must be removed from and re-added to the cluster (`etcdctl member remove` / `member add`); one that keeps its data just rejoins.
- **Point Patroni at all members.** `etcd3.hosts` lists all three; Patroni also learns the member list from etcd itself. Never configure a single endpoint.
