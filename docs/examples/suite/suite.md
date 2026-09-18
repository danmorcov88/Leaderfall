# leaderfall suite `core,advanced`: FAIL

2026-09-18T12:07:48+00:00 → 2026-09-18T12:24:04+00:00 · 16 scenarios · 15 passed

| Scenario | Profile / sync | Result | Detection | RTO write | Commit stall | Lost acked | Unknown | Split brain | Fenced | Rejoin |
|---|---|---|---|---|---|---|---|---|---|---|
| `double-fault` | default / off | pass | 34.4 s | 38.0 s | 37.9 s | 0 | 0 | no | - | 7.9 s |
| `etcd-one-member-loss` | default / off | pass | - | 0.0 s | 0.1 s | 0 | 0 | no | - | - |
| `etcd-quorum-loss` | default / off | pass | - | 54.1 s | 30.1 s | 0 | 0 | no | 24.7 s | - |
| `fast-profile-primary-sigkill` | fast / off | FAIL | 25.2 s | 29.3 s | 29.2 s | 0 | 0 | no | - | 8.2 s |
| `haproxy-restart` | default / off | pass | - | 6.3 s | 1.2 s | 0 | 0 | no | - | - |
| `lagging-replica-failover` | default / off | pass | - | 81.9 s | 68.2 s | 0 | 1 | no | - | 65.3 s |
| `lagging-replica-stale-optime` | default / off | pass | 40.1 s | 42.7 s | 41.0 s | 8 | 0 | no | - | 0.1 s |
| `planned-switchover` | default / off | pass | 8.4 s | 10.6 s | 3.8 s | 0 | 0 | no | - | - |
| `primary-clean-stop` | default / off | pass | 1.9 s | 5.6 s | 5.2 s | 0 | 0 | no | - | 2.0 s |
| `primary-frozen` | default / off | pass | 30.7 s | 33.9 s | 33.8 s | 0 | 0 | yes | 44.6 s | 11.0 s |
| `primary-partition` | default / off | pass | 22.7 s | 26.4 s | 26.3 s | 0 | 0 | no | 8.7 s | 27.7 s |
| `primary-sigkill` | default / off | pass | 25.2 s | 29.4 s | 29.2 s | 0 | 1 | no | - | 7.6 s |
| `replica-loss` | default / off | pass | - | 0.0 s | 0.0 s | 0 | 0 | no | - | 3.1 s |
| `sync-mode-primary-sigkill` | default / on | pass | 34.8 s | 37.3 s | 37.2 s | 0 | 1 | no | - | 8.6 s |
| `sync-replica-loss-strict` | default / strict | pass | - | 0.0 s | 6.3 s | 0 | 0 | no | - | 2.0 s |
| `sync-replica-loss` | default / on | pass | - | 0.0 s | 6.2 s | 0 | 0 | no | - | 3.0 s |
