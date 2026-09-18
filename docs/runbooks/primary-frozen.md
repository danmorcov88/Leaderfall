# primary-frozen

**What breaks:** every process on the primary is frozen (`docker pause`) for longer than `ttl`, then thawed. Nothing is killed; PostgreSQL and Patroni simply stop getting CPU. This is a VM paused by its hypervisor, a host swapping itself to death, a kernel stuck in a long stop-the-world, or a `SIGSTOP` by mistake.

This is the scenario the watchdog exists for. Read the last section.

**Scenario file:** [`scenarios/primary-frozen.yml`](../../scenarios/primary-frozen.yml)

## What you see

**During the freeze.** The frozen node answers nothing, but its TCP connections stay open: the container's kernel still ACKs keepalives while its processes are stopped. Clients with an open connection hang instead of failing. HAProxy's health check times out, marks the node DOWN and (`on-marked-down shutdown-sessions`) cuts those hanging client sessions. The replicas see the leader lease expire and elect a new leader exactly as in `primary-sigkill`.

**At the thaw.** The old primary wakes up with its memory intact: PostgreSQL is a primary on the old timeline, Patroni still believes it holds the lock. Its Patroni loop, whose timer expired during the freeze, runs at once:

```
INFO: Lock owner: pg-2; I am pg-2          <- stale view from before the freeze
ERROR: failed to update leader lock
INFO: Demoting self (immediate-nolock)
LOG:  received immediate shutdown request
INFO: demoted self because failed to update leader lock in DCS
INFO: Lock owner: pg-3; I am pg-2
INFO: starting after demotion in progress
```

Then crash recovery, `pg_rewind` against the new leader, and it rejoins as a replica.

## What the harness measured

| Metric | Value |
|---|---|
| Detection (new leader) | 24.9 s |
| RTO write | 26.8 s |
| Old primary accepted a write after the thaw | **yes**: the poller's hung query ran the moment the node thawed and its probe `INSERT` committed on timeline 28 while pg-3 was leader on timeline 29 |
| Split brain (two nodes committing in the same 0.5 s poll round) | **1 round** |
| Thaw → fenced | ~0.3 s (Patroni's loop was due; it demoted on its first DCS call) |
| Lost acked commits | 0: HAProxy had marked the node DOWN and did not route client writes to it |
| Rejoin after thaw | 8.7 s (immediate shutdown, crash recovery, `pg_rewind`) |

So: for a few hundred milliseconds there were two PostgreSQL primaries, both accepting commits. The harness got one in. HAProxy did not, because `rise 2` needs two good checks (4 s) and Patroni fenced the node in 0.3 s. A client connected directly to the old primary, or a proxy with a faster `rise`, would have written into the void: those rows are on a timeline `pg_rewind` throws away.

The split-brain check for this scenario is **reported, not enforced** (`split_brain: null` in the YAML). Enforcing it would fail every nightly run, and the failure is not a Patroni bug: it is the absence of a watchdog.

## Why a watchdog, and why this lab cannot have one

Patroni supports a kernel watchdog (`/dev/watchdog`, usually the `softdog` module). Patroni "pets" it on every loop. If Patroni stops petting it for `ttl` (minus a safety margin), the kernel resets the whole machine. A frozen primary therefore never wakes up as a primary: it reboots and comes back as a replica. That closes exactly the window measured above.

Containers cannot do this. `/dev/watchdog` belongs to the host kernel; giving a container the device means letting it reboot the Docker host. Docker Desktop's VM does not even load `softdog`. So this lab proves the software fence (Patroni demotes on its first loop after the thaw) and measures the window that the hardware fence would close. It cannot demonstrate the hardware fence itself.

What that means for reading the numbers: every "no split brain" result in this repository holds for faults where the old primary is dead or cut off. For a freeze, the honest statement is: **split brain for about one Patroni loop, unless you run a watchdog**.

## What a DBA should do in production

- **Enable the watchdog.** `modprobe softdog`, give the Patroni user access to `/dev/watchdog`, and set `watchdog: mode: required` in `patroni.yml`. With `required`, Patroni refuses to become leader if it cannot open the device; that is the setting you want.
- **Keep `ttl` honest.** The watchdog timeout is derived from `ttl`; a longer `ttl` is a longer window before the reset. The default 30 s is fine; do not raise it "for stability".
- **Do not let clients bypass the proxy.** A client with a direct connection to a node is the one that writes into the void after a thaw.
- **After a thaw, check for a rewind.** `pg_rewind: servers diverged at WAL location ...` in the node's Patroni log tells you it had data the cluster no longer has. If it was frozen for long and had no watchdog, that data was written by clients during the freeze or right after it.
