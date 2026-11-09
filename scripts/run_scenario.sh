#!/usr/bin/env bash
# usage: scripts/run_scenario.sh <scenario_name> <port>   (expects runs/base_<scenario> from `make baseline`)
set -e
S=$1; PORT=$2
cd "$(dirname "$0")/.."
python -m sim serve --scenario scenarios/$S.yaml --run-dir runs/mine_$S --force --port $PORT > logs/sim_$S.log 2>&1 &
SIMPID=$!
for i in $(seq 1 900); do curl -s localhost:$PORT/health >/dev/null && break; sleep 1; done
start=$(date +%s)
STATE_DIR=state/$S ./run.sh http://127.0.0.1:$PORT > logs/ctl_$S.log 2>&1
echo "controller wall $(( $(date +%s) - start ))s" >> logs/ctl_$S.log
kill $SIMPID
mkdir -p reports
PYTHONUTF8=1 python -m sim grade --run runs/mine_$S --baseline runs/base_$S --out reports/$S.md > /dev/null
