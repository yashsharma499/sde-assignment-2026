# Controller runbook

For the engineer who owns or gets paged for the controller. The design rationale is in `SUBMISSION.md`.

## Run it
```bash
pip install -r requirements.txt
python -m sim baseline --scenario scenarios/public_02_incidents.yaml --run-dir runs/base_public_02_incidents
scripts/run_scenario.sh public_02_incidents 8712      # serve + ./run.sh + grade -> reports/public_02_incidents.md
scripts/crash_test.sh public_02_incidents 8720        # kill twice mid-run, restart, finish, grade
python -m pytest -q tests                              # simulator contract + controller unit tests
```
To run the controller by hand against a simulator that is already serving, use `./run.sh http://127.0.0.1:8700`. State goes to `$STATE_DIR`, which defaults to `state/run`.

## Code map
| file | what it owns |
|---|---|
| `controller/params.py` | **every threshold**, each with the reason for its value. Change behaviour here first. |
| `controller/aggregates.py` | Event ingestion → numpy counters per workflow; the lead → feature-cell join; the `CodeBook` (shared cohort basis) |
| `controller/policy.py` | Pure decision functions: caller-ID health, call-time profiles, the experiment state machine, the cohort compiler, dry runs, the hallucination detector |
| `controller/loop.py` | Tick loop, governance client (intent → submit → poll → apply), incident workflow, crash reconciliation |
| `controller/store.py` | SQLite schema (proposals, audit, wf_state, configs, kpi_hourly, loop_metrics) and the atomic snapshot |
| `controller/view.py` | Supervisor view, `why` query, `health`, `audit` CLI |

## Answering questions from the store
```bash
python -m controller.view --state state/public_02_incidents                 # one screen per workflow
python -m controller.view --state state/public_02_incidents why ks_cancel "D3 17:00"
python -m controller.view --state state/public_02_incidents audit ks_cancel  # full decision log
python -m controller.view --state state/public_02_incidents health           # ingest rate, decide time, RSS, credits
sqlite3 state/public_02_incidents/controller.db "select proposal_id,kind,status,approval_id from proposals"
```

## When paged
| alert | look at | usual fix |
|---|---|---|
| ingest stalled | `view health`, the simulator's `/health`, `loop_metrics` for the last tick | The simulator is down: the client retries 8× and then exits. Restarting is safe. |
| `incident_no_fix` | `view audit <wf>`: validation runs and LLM suggestions | No prompt passed validation. Escalate to the client; don't lower `HAL_MAX_FAILURE_RATE`. |
| credits below floor | `view health` | Top up, or accept that incidents will only try the last-known-good prompt. |
| quarantine not filed (budget) | `view audit <wf>` | The approver budget is exhausted. Ask the client approver to act manually. |
| crash loop | stderr | A bug. The state is safe: fix it, restart, and it reconciles. |

## Restart semantics
- Safe at any point. On start the controller:
  1. Loads `aggregates.pkl` (counters plus the last chunk folded in).
  2. Replays newer chunks.
  3. Reconciles every `intent`/`applying` proposal against the simulator by `proposal_id`.
  4. Continues.
- A new or restarted simulator world (different scenario, or a clock that went backwards) wipes the state dir automatically.

## Extending
- **A new lever:** add a pure planner in `policy.py` that returns the config change plus `decisions` entries. Call it from `step_routine` (approval path) or from its own `step_*` (auto path). The governance client, audit log and view pick it up from the `decisions` list.
- **A new stakeholder preference:** `params.TENANT_PREFS`.
