# Load test — public_03_load (12 workflows, ~650k dials/day)

Measured on 9 Nov 2026 on a Windows laptop (8 logical cores, Python 3.11). The simulator and the controller ran on the same machine. Command:

```
python -m sim serve --scenario scenarios/public_03_load.yaml --run-dir runs/mine_public_03_load --port 8713
python -m controller --url http://127.0.0.1:8713 --state-dir state/public_03_load --stop-after "D1 00:00" -v
python -m controller.view --state state/public_03_load health
```

Only the first simulated day (D0) after the 7-day history was run, because of time. The SRE budget is stated per simulated day.

| metric | value |
|---|---|
| History bootstrap (7 days, ~5.5M events: fetch, ingest, baselines) | ~200 s wall, once per cold start |
| D0 events ingested | 840,066 |
| Ingest throughput (parse + join + counters) | **~48,000 events/s** (17 s for the whole day) |
| Decide time for the day (96 ticks × 12 workflows) | 203 s; worst single tick 10.7 s |
| Simulator `advance` time for the day | 18 s |
| **Wall time per simulated day (controller + simulator)** | **~241 s ≈ 4.0 min** (budget: 10 min) |
| Decision lag | 1 tick (15 sim-min) + ≤ 11 s of compute |
| Peak RSS (controller) | ~1.96 GB |
| Decisions | 13 approval requests, 12 applied, 29 auto-applies (≈ 2.3/hour) |
| Credits | 0 used (no incidents in this scenario) |

## Where the time goes, and what breaks at 10×
- **Ingest is not the bottleneck.** 48k events/s is about 57× the average event rate of 850k/day.
- **Decide time (85% of controller wall) is dominated by fetching every workflow's full config every tick.** The config carries `lead_preferred_hour` with ~100k entries per workflow (several MB of JSON), and it's fetched 12 × 96 times a day.
  - At 10× workflows this grows linearly to about 35 min per simulated day, so **this is the first thing to break**.
  - Fix: cache the config by version and refetch only on a `config_changed` event. We already receive those events, so this cuts decide time by about 10×. Delivering call time through cohort `hour_weights` instead of per-person maps would shrink the payload too.
- **Memory:** about 1.9 GB peak with 12 workflows. That's mostly the lead → cell map for live campaigns (about 50k leads × 12 workflows) plus the numpy counters. At 10× workflows, shard by workflow (state is already per workflow): 4 processes × ~25 workflows fits 8 GB.
