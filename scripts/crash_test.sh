#!/usr/bin/env bash
# Crash-recovery evidence (C13): kill the controller twice mid-run (hard exit, no cleanup), restart it,
# and let it finish. Then grade and check the governance counters for double-applies.
# usage: scripts/crash_test.sh [scenario] [port]
set -e
S=${1:-public_02_incidents}; PORT=${2:-8720}
cd "$(dirname "$0")/.."
RUN=runs/crash_$S; ST=state/crash_$S; LOG=logs/crash_$S.log
rm -rf "$ST"; : > "$LOG"
python -m sim serve --scenario scenarios/$S.yaml --run-dir $RUN --force --port $PORT > logs/sim_crash_$S.log 2>&1 &
SIMPID=$!
for i in $(seq 1 600); do curl -s localhost:$PORT/health >/dev/null && break; sleep 1; done
for stop in "D2 11:15" "D3 13:45"; do
  echo "=== run until simulated crash at $stop" | tee -a $LOG
  python -m controller --url http://127.0.0.1:$PORT --state-dir $ST --stop-after "$stop" -v >> $LOG 2>&1 || echo "controller exited with $? (expected 3 = simulated crash)" | tee -a $LOG
  curl -s localhost:$PORT/sim/state | tee -a $LOG; echo | tee -a $LOG
done
echo "=== restart and run to completion" | tee -a $LOG
python -m controller --url http://127.0.0.1:$PORT --state-dir $ST -v >> $LOG 2>&1
kill $SIMPID
grep -E "restart|reconciled|finished" $LOG | tee logs/crash_${S}_summary.txt
PYTHONUTF8=1 python -m sim grade --run $RUN --baseline runs/base_$S --out reports/crash_$S.md > /dev/null
echo "grade -> reports/crash_$S.md"
