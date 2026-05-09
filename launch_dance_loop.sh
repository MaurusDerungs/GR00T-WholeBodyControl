#!/usr/bin/env bash
set -euo pipefail

# Launch a dance-only reference set and keep cycling through the motions.
# Usage:
#   ./launch_dance_loop.sh          # MuJoCo simulation + controller
#   ./launch_dance_loop.sh real     # real G1 robot
#   ./launch_dance_loop.sh eth0     # explicit robot network interface

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR/gear_sonic_deploy"
SIM_DISABLE_ELASTIC_DELAY_SEC="${SIM_DISABLE_ELASTIC_DELAY_SEC:-4.5}"
SIM_DANCE_START_DELAY_SEC="${SIM_DANCE_START_DELAY_SEC:-3.0}"

MODE="${1:-sim}"
if [[ $# -gt 0 ]]; then
  shift
fi

if [[ "$MODE" == "sim" || "$MODE" == "127.0.0.1" || "$MODE" == "lo" || "$MODE" == "lo0" ]]; then
  cd "$SCRIPT_DIR"

  if [[ -f .venv_sim/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv_sim/bin/activate
  fi

  python gear_sonic/scripts/run_sim_loop.py \
    --auto-disable-elastic-after-cmd-sec "$SIM_DISABLE_ELASTIC_DELAY_SEC" &
  SIM_PID=$!

  cleanup() {
    if kill -0 "$SIM_PID" >/dev/null 2>&1; then
      kill "$SIM_PID" >/dev/null 2>&1 || true
      wait "$SIM_PID" >/dev/null 2>&1 || true
    fi
  }
  trap cleanup EXIT INT TERM

  sleep 2

  cd "$DEPLOY_DIR"
  bash deploy.sh \
    --motion-data reference/dance_loop \
    --auto-motion-loop \
    --auto-motion-start-delay "$SIM_DANCE_START_DELAY_SEC" \
    --auto-motion-playback-start-index 1 \
    --yes \
    sim \
    "$@"
else
  cd "$DEPLOY_DIR"
  exec bash deploy.sh \
    --motion-data reference/dance_loop \
    --auto-motion-loop \
    "$MODE" \
    "$@"
fi
