# sync-replica-loss and sync-replica-loss-strict

**What breaks:** synchronous mode is on and the sync standby dies (`docker kill`). The primary is fine, but by definition it may not acknowledge a commit until the sync standby has flushed it. There is no sync standby.

Two scenarios: [`sync-replica-loss`](../../scenarios/sync-replica-loss.yml) with `synchronous_mode: on`, and [`sync-replica-loss-strict`](../../scenarios/sync-replica-loss-strict.yml) with `synchronous_mode_strict: true` as well.

## What you see

**Clients.** No errors. Every `COMMIT` in flight simply waits. The writer's ledger shows one write that took 6.2 s and then everything continues. An application with a statement timeout shorter than that would see timeouts instead.

**Patroni** on the primary, on its next loop:

```
INFO: Updating synchronous privilege temporarily from ['pg-3'] to []
INFO: Assigning synchronous standby status to ['pg-2']
LOG:  parameter "synchronous_standby_names" changed to ""pg-2""
LOG:  standby "pg-2" is now a synchronous standby with priority 1
INFO: Synchronous standby status assigned to ['pg-2']
```

The other replica becomes the sync standby, `synchronous_standby_names` is changed on the primary, and the waiting commits complete.

## What the cluster does by itself

Patroni moves the sync role to the surviving replica. The stall is the time until Patroni notices (up to one `loop_wait`) plus the reconfiguration. No failover; the primary never lost its lock.

**`on` versus `strict`** makes no difference *in this scenario*, because a healthy replica is available. The difference appears when there is nobody to promote to sync standby:

- `synchronous_mode: on` — Patroni sets `synchronous_standby_names` to empty and the primary continues **without** synchronous replication. Writes flow; a subsequent primary failure can lose acked commits.
- `synchronous_mode_strict: true` — Patroni sets `synchronous_standby_names` to `*` ... nobody, and the primary **blocks all writes** until a standby is available. Availability is sacrificed for the guarantee.

With three nodes and one replica down there is always a candidate, so both modes recovered identically here. With two nodes, or with both replicas down, they diverge, and that is the setting to test before choosing.

## Measured

| Metric | `on` | `strict` |
|---|---|---|
| Failover | none | none |
| Write errors | 0 | 0 |
| Longest gap between two acked writes (commit stall) | **6.2 s** | **6.2 s** |
| Slowest single commit | 6.2 s | 6.2 s |
| Lost acked commits | 0 | 0 |
| Rejoin of the killed replica | 2.0 s | 2.0 s |

The harness only sees this stall because it measures the gap between acks, not just failed writes. A dashboard that counts errors would show a flat line.

## What a DBA should do in production

- **Set client timeouts with the stall in mind.** A 6 s commit is normal here. `statement_timeout` below `loop_wait` turns every sync-standby loss into a burst of client errors.
- **Choose `strict` deliberately.** It is the right setting when a lost commit is unacceptable *and* an outage is acceptable. Most teams want `on`, plus alerting on `synchronous_standby_names` becoming empty.
- **Run at least two replicas** in sync mode, so that losing one leaves a candidate. With one replica, `strict` means any replica maintenance is a write outage.
- **Consider `synchronous_mode: quorum`** (Patroni 4): `ANY 1 (pg-2, pg-3)` lets either replica acknowledge, so losing one costs nothing. Not tested in this lab yet.
