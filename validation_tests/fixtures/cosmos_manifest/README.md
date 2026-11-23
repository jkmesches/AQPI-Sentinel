# CoSMoS cycle manifests

<!-- prose-check: mode=tech-informal -->

Real manifests as the CoSMoS driver wrote them, byte for byte, captured on the model host by a
read-only observer. They feed `validation_tests/test_cosmos_cycle.py`.

| file | outcome | wrote_at |
|---|---|---|
| `exhaustion-01-ran-20261122_2000.json` | `ran` | 2026-11-22 22:23:20Z |
| `exhaustion-02-no_forecast_exit1-20261122_2100.json` | `no_forecast_exit1` | 2026-11-22 23:46:36Z |
| `exhaustion-03-ran-20261122_2200.json` | `ran` | 2026-11-23 00:23:10Z |
| `standdown-01-skipped_incomplete_hrrr-20261122_2300.json` | `skipped_incomplete_hrrr` | 2026-11-23 01:00:12Z |
| `standdown-02-skipped_warmstart_running-20261123_0000.json` | `skipped_warmstart_running` | 2026-11-23 02:00:44Z |

`standdown-timeline.txt` holds the observer's 42 samples from 01:09Z to 02:35Z on 2026-11-23,
one row each, with the manifest in place and the four products' newest frames and their mtimes.
That night the 01:00Z cycle skipped on a short t23z HRRR set (5 of 19 files), the scheduled
warmstart rebuild took the model lock at 01:25:01Z, and the 02:00Z cycle stood down. The
products went 184 min without a publish, from 00:21:30Z until the 03:00Z cycle published at
03:25:30Z.

The stand-down reason gives the lock time in the model host's local time with its offset
(`2026-11-22 18:25:01.785167311 -0700`), and `standdown-02` reads `file_count: 20` against 19
expected, from an extended HRRR run. Both are cases the parser has to handle.

The originals and their capture notes live with the AQPI backend tooling. They passed the same
leak scan as this repository before they were copied.
