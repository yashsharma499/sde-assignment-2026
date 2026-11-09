#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> starts the controller and runs until the scenario finishes. No Docker needed:
# Python 3.11 + requirements.txt (numpy, httpx). State goes to $STATE_DIR (SQLite + snapshot). The controller
# wipes it automatically when it sees a different/restarted simulator world, and resumes from it after a
# controller crash (see scripts/crash_test.sh).
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
STATE_DIR="${STATE_DIR:-state/run}"
exec python -m controller --url "$SIM_URL" --state-dir "$STATE_DIR" -v
