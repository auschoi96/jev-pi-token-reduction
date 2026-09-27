#!/usr/bin/env bash
# With/without-Jev benchmark: run one prompt headless through Pi in several Jev modes, alternating modes
# so drift over time (gateway latency, rate limits) hits each mode equally. Each run is what `make pi
# MODE=<mode>` starts, plus `-p "<prompt>"`, and is traced to MLflow when jev.mlflow.env exists.
#
#   QUESTION="<prompt>" N=50 MODES="arrival off" LOGDIR=results/my-benchmark bash run_bench.sh
#   SCHEDULE="arrival:1 arrival:2 off:1 ..." QUESTION="<prompt>" bash run_bench.sh   # explicit order
#
# Writes <LOGDIR>/<mode>_<NNN>.log per run and appends mode,run,rc,duration_s,timestamp to summary.csv.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

QUESTION="${QUESTION:?set QUESTION to the prompt every run gets}"
N="${N:-50}"
START="${START:-1}"   # first run index, to resume a batch; summary.csv is appended to
PER_RUN_TIMEOUT="${PER_RUN_TIMEOUT:-1200}"   # seconds; watchdog kills a run past this
LOGDIR="${LOGDIR:-$ROOT/results/bench}"
SUMMARY="$LOGDIR/summary.csv"
mkdir -p "$LOGDIR"

# MLflow tracing settings from `make mlflow-setup`; without them runs are simply not traced.
if [ -f "$ROOT/jev.mlflow.env" ]; then set -a; source "$ROOT/jev.mlflow.env"; set +a; fi
# Same wiring `make pi` sets; the router needs the interpreter where this package (and MLflow) is installed.
export PI_CODING_AGENT_DIR="$ROOT/pi-agent"
export JEV_ROUTER_POLICY="$ROOT/jev.config.json"
export JEV_ROUTER_PYTHON="${JEV_ROUTER_PYTHON:-$ROOT/.venv/bin/python}"
# Must exceed result_budget_seconds in jev.config.json, or the extension kills the router mid-wait.
export JEV_ROUTER_BRIDGE_TIMEOUT_MS="${JEV_ROUTER_BRIDGE_TIMEOUT_MS:-130000}"
# A Databricks config holding only the profile the agent may use. Given an open-ended task, an agent will
# otherwise explore every workspace in ~/.databrickscfg, and with allow_commands that output goes to Jev.
export DATABRICKS_CONFIG_FILE="${DATABRICKS_CONFIG_FILE:-$ROOT/pi-agent/databrickscfg}"
[ -f "$DATABRICKS_CONFIG_FILE" ] || {
  echo "missing $DATABRICKS_CONFIG_FILE: create it with just the [profile] section the agent may use" >&2; exit 1; }
# TypeSafe only (no fallback provider on the Vercel gateway).
export JEV_GATEWAY_ONLY="${JEV_GATEWAY_ONLY:-typesafe-ai}"

[ -f "$SUMMARY" ] || echo "mode,run,rc,duration_s,timestamp" > "$SUMMARY"

run_one() {  # $1=mode $2=index
  local mode="$1" i="$2"
  local log; log="$LOGDIR/${mode}_$(printf '%03d' "$i").log"
  local start end rc; start=$(date +%s)
  pi -e "$ROOT/adapters/pi/extension.ts" --jev "$mode" -p "$QUESTION" >"$log" 2>&1 &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 2; waited=$((waited+2))
    if [ "$waited" -ge "$PER_RUN_TIMEOUT" ]; then
      kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; rc=124
      break
    fi
  done
  if [ -z "${rc:-}" ]; then wait "$pid"; rc=$?; fi
  end=$(date +%s)
  echo "$mode,$i,$rc,$((end-start)),$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$SUMMARY"
  echo "[$(date '+%H:%M:%S')] $mode run $i rc=$rc dur=$((end-start))s"
  unset rc
}

MODES="${MODES:-arrival off}"
PLAN=$([ -n "${SCHEDULE:-}" ] && echo "schedule of $(echo $SCHEDULE | wc -w | tr -d ' ') runs" || echo "modes=($MODES) runs $START-$N each, interleaved")
echo "===== [$(date)] START $PLAN, mlflow experiment=${JEV_MLFLOW_EXPERIMENT_ID:-none} ====="
# SCHEDULE="arrival:1 arrival:2 off:1 ..." runs an explicit order instead (for unequal run counts per mode).
if [ -n "${SCHEDULE:-}" ]; then
  for item in $SCHEDULE; do run_one "${item%%:*}" "${item##*:}"; done
else
  for i in $(seq "$START" "$N"); do
    for m in $MODES; do run_one "$m" "$i"; done
  done
fi
echo "===== [$(date)] ALL DONE. Summary: $SUMMARY ====="
