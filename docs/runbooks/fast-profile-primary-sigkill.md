# fast-profile-primary-sigkill

**What breaks:** the primary dies hard, as in `primary-sigkill`, with the `fast` Patroni profile: `ttl 20 / loop_wait 5 / retry_timeout 5` instead of `30 / 10 / 10`.

**Scenario file:** [`scenarios/fast-profile-primary-sigkill.yml`](../../scenarios/fast-profile-primary-sigkill.yml)

## The profile is not the one that was planned

The plan for this lab said `ttl 15`. Patroni does not allow it. `patroni/config.py` validates the timeouts and silently raises `ttl` to a minimum of **20 s**:

```python
ttl = self.__get_and_maybe_adjust_int_value(config, 'ttl', 20)
```

The first run of this scenario was made with `ttl: 15` in the DCS. `GET /config` said 15. The etcd leases said 20. The failover took 25.2 s, right where a 20 s lease predicts and outside anything a 15 s lease allows. The profile now says 20 so that the configuration describes what runs. (The rule Patroni enforces on top of that: `loop_wait + 2 * retry_timeout <= ttl`.)

## What you see

The same as `primary-sigkill`, faster. The replicas notice the expired lease 15–20 s after the kill instead of 20–30 s, and act within 5 s instead of 10.

## Measured

Three runs:

| Metric | `default` (30/10/10) | `fast` (20/5/5) |
|---|---|---|
| Detection | 23–30 s | 19.2 / 21.7 / 25.2 s |
| RTO write | 29–37 s | 22.0 / 29.2 / 29.3 s |
| Rejoin | 5–8 s | 5.7–6.8 s |
| Lost acked commits | 0 | 0 |

Detection is bounded by `ttl + loop_wait`: 40 s for `default`, 25 s for `fast`. The two RTOs above 25 s came from the read-only window after promotion (`wal_retrieve_retry_interval`, see `primary-sigkill`), which the profile does not change.

## What a DBA should do in production

- **Shorter timeouts mean more false failovers.** With `retry_timeout 5`, a 5 s hiccup in the DCS network demotes a healthy primary. `ttl 20` is the floor for a reason; go there only with a fast, quiet network to the DCS.
- **Keep the invariant** `loop_wait + 2 * retry_timeout <= ttl`. Patroni adjusts the values if you break it, and the values it picks may not be the ones you meant.
- **Tune the promotion, not only the lease.** For this workload, `wal_retrieve_retry_interval = 1s` would cut more RTO than the `fast` profile does, at no risk of false failovers.
