# wal-disk-full

**What breaks:** the filesystem holding the primary's `pg_wal` fills up. PostgreSQL cannot write the next WAL record, panics, and the postmaster restarts into crash recovery, which also needs to write WAL and fails again. Patroni sees a primary that cannot run; the lease expires; a replica is promoted. This is one of the most common real-world outages: an archive command that stopped working, a replication slot for a dead replica, a runaway bulk load.

**Scenario file:** [`scenarios/wal-disk-full.yml`](../../scenarios/wal-disk-full.yml)

## Status: not runnable in this lab as shipped

This scenario is written and wired (`fill_disk` and `free_disk` primitives, the YAML, the SLO), but the `fill_disk` fault **refuses to run** unless `pg_wal` sits on a filesystem smaller than 4 GB. It is tagged `bounded-wal`, so `make chaos-all` does not run it. Running it on this lab's hosts prints:

```
refusing to fill pg_wal on pg-2: its filesystem is 1006 GB, which is the shared Docker host disk, not a bounded WAL volume
```

Why: in Docker Compose the data volume of every node is a directory on the Docker host's disk (the WSL2 VM disk on Docker Desktop, the runner's root disk in CI). Filling it would fill the host, take etcd, HAProxy and the other two nodes down with it, and prove nothing about PostgreSQL. A bounded `pg_wal` needs one of:

- a volume driver with size limits (not available in Docker Desktop or on GitHub runners);
- a size-limited `tmpfs` mounted at `pg_wal`, which loses the WAL every time the container restarts and would break every other scenario's rejoin path (`docker start` after a kill would find an empty `pg_wal`);
- mounting a loop-backed filesystem inside the container, which needs `CAP_SYS_ADMIN` on the PostgreSQL containers, and the lab does not grant its database containers any capability at all.

Faking the failure (for example by making `pg_wal` read-only) would exercise a different error path than `ENOSPC`. The lab does not do that.

## How to run it on a host of your own

On a Linux host with a real disk you control, give each node a small dedicated volume for WAL. One way: create a 512 MB file-backed ext4 image per node, mount it on the host, and bind-mount it into the container at a path you point Patroni's `initdb` at with `--waldir`. Then `leaderfall run wal-disk-full` will find a bounded filesystem and fill it with `fallocate`. Expect: the primary panics within seconds, the replicas elect a new leader after the lease expires, the old node rejoins (with `pg_rewind`) once `free_disk` removes the file and Patroni restarts it.

## What a DBA should do in production

- **Put `pg_wal` on its own volume** and alert at 80 %. A full data volume is a crash; a full WAL volume is a crash with a clear cause.
- **Know what holds WAL back:** a stuck `archive_command` (check `pg_stat_archiver`), an inactive replication slot (`pg_replication_slots` with `active = false`), a very long transaction, `wal_keep_size`. Patroni's `use_slots: true` keeps WAL for a dead replica indefinitely unless the member is removed.
- **Never delete files from `pg_wal` by hand.** Free space elsewhere or fix the cause, then let PostgreSQL recycle. If you must, `pg_archivecleanup` and a backup are the tools.
- **Expect the failover to be the same as a hard kill:** a PANIC is a crash. RTO is the lease plus promotion, and in async mode the last commits before the panic may be lost.
