# replica-loss

**What breaks:** one of the two replicas dies (`docker kill -s SIGKILL`). The primary is untouched.

**Scenario file:** [`scenarios/replica-loss.yml`](../../scenarios/replica-loss.yml)

## What you see

**Clients.** Writes do not notice. Reads on `:5001` lose one of two backends; HAProxy marks it DOWN after two failed checks and sends everything to the other replica. A read in flight on the dead node fails once.

**Patroni.** The primary logs the lost replication connection. `patronictl list` shows the member as missing once its member key expires. Nothing else happens: no election, no timeline change.

**HAProxy.** `Server replicas/pg-1 is DOWN`; `1 active and 0 backup servers left` on the read backend.

## What the cluster does by itself

Nothing that touches writes. When the node is started again, it reconnects to the primary and streams from where it left off. No `pg_rewind`: its timeline never diverged. The replication slot on the primary kept the WAL it needs (`use_slots: true`).

## Measured

| Metric | Value |
|---|---|
| Failover | none (the SLO requires none) |
| RTO write | 0.0 s: not one write failed |
| RTO read | 0.1–0.2 s |
| Lost acked commits | 0 |
| Rejoin | 2.7–3.2 s |

## What a DBA should do in production

- **Check replication slots.** With `use_slots`, the primary keeps WAL for the missing replica. If the replica is gone for hours, `pg_wal` grows. Watch disk; drop the slot if the node is not coming back (Patroni does it when the member is removed).
- **Mind synchronous mode.** In async mode this is a non-event. In sync mode, losing the *synchronous* replica stalls or blocks writes; see `sync-replica-loss` (Phase 4).
- **Capacity.** With one replica left, a second failure means a failover with no replica to fail over to. Replace the node before that.
