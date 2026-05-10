#!/usr/bin/env bash
set -euo pipefail

# Start Sonic Station in sim or real-robot mode.
# Usage:
#   tools/sonic_station/launch_station.sh
#   tools/sonic_station/launch_station.sh sim
#   tools/sonic_station/launch_station.sh real eth0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/gear_sonic_deploy"

STATION_MODE="${1:-sim}"
if [[ $# -gt 0 ]]; then
  shift
fi

if [[ "$STATION_MODE" == "real" ]]; then
  STATION_ROBOT_INTERFACE="${1:-${STATION_ROBOT_INTERFACE:-}}"
  if [[ -z "$STATION_ROBOT_INTERFACE" ]]; then
    echo "Error: real mode requires the robot network interface name."
    echo "Example:"
    echo "  tools/sonic_station/launch_station.sh real eth0"
    echo ""
    echo "Tip: use 'ip -br addr' to find the Ethernet interface name."
    exit 1
  fi
  if [[ $# -gt 0 ]]; then
    shift
  fi
elif [[ "$STATION_MODE" == "sim" || "$STATION_MODE" == "127.0.0.1" || "$STATION_MODE" == "lo" || "$STATION_MODE" == "lo0" ]]; then
  STATION_MODE="sim"
  STATION_ROBOT_INTERFACE=""
else
  echo "Error: first argument must be 'sim' or 'real'."
  echo "For real mode, pass the network interface name, not an IP address."
  echo "Example: tools/sonic_station/launch_station.sh real eth0"
  exit 1
fi

SIM_DISABLE_ELASTIC_DELAY_SEC="${SIM_DISABLE_ELASTIC_DELAY_SEC:-4.5}"
STATION_MOTION_DATA="${STATION_MOTION_DATA:-reference/station_idle}"
STATION_IDLE_FRAMES="${STATION_IDLE_FRAMES:-300}"
STATION_IDLE_SOURCE="${STATION_IDLE_SOURCE:-$DEPLOY_DIR/reference/example/neutral_kick_R_001__A543}"
STATION_CAMERA_PORT="${STATION_CAMERA_PORT:-5560}"
STATION_CAMERA_PORT_MAX="${STATION_CAMERA_PORT_MAX:-5599}"
STATION_RESERVED_PORTS="${STATION_RESERVED_PORTS:-5556 5557 5558}"
STATION_ZMQ_PORT="${STATION_ZMQ_PORT:-5556}"
STATION_SIM_ZMQ_PORT="${STATION_SIM_ZMQ_PORT:-5561}"
STATION_SIM_ZMQ_OUT_PORT="${STATION_SIM_ZMQ_OUT_PORT:-5568}"
STATION_ROBOT_IP="${STATION_ROBOT_IP:-}"
if [[ "$STATION_MODE" == "real" && -z "$STATION_ROBOT_IP" ]]; then
  STATION_ROBOT_IP="192.168.123.164"
fi
STATION_SIM_PREVIEW="${STATION_SIM_PREVIEW:-true}"
STATION_SIM_DEPLOY="${STATION_SIM_DEPLOY:-true}"
STATION_ENABLE_ONSCREEN="${STATION_ENABLE_ONSCREEN:-false}"
STATION_IMAGE_DT="${STATION_IMAGE_DT:-0.016667}"
if [[ "$STATION_MODE" == "real" ]]; then
  STATION_SIM_LOG_TO_FILE="${STATION_SIM_LOG_TO_FILE:-true}"
else
  STATION_SIM_LOG_TO_FILE="${STATION_SIM_LOG_TO_FILE:-false}"
fi
STATION_START_UI="${STATION_START_UI:-true}"
STATION_UI_HOST="${STATION_UI_HOST:-127.0.0.1}"
STATION_UI_PORT="${STATION_UI_PORT:-8765}"
STATION_LOG_DIR="${STATION_LOG_DIR:-$REPO_ROOT/.sonic_station/logs}"
STATION_UI_PORT_MAX="${STATION_UI_PORT_MAX:-8799}"
STATION_CAMERA_VIEW_FILE="${STATION_CAMERA_VIEW_FILE:-$REPO_ROOT/.sonic_station/camera_view.json}"

cd "$REPO_ROOT"

if [[ -f .venv_sim/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv_sim/bin/activate
fi

mkdir -p "$STATION_LOG_DIR"
mkdir -p "$(dirname "$STATION_CAMERA_VIEW_FILE")"
if [[ ! -f "$STATION_CAMERA_VIEW_FILE" ]]; then
  printf '{"azimuth":135,"elevation":-18,"distance":3,"lookat":[0,0,0.75]}\n' >"$STATION_CAMERA_VIEW_FILE"
fi

port_is_free() {
  local host="$1"
  local port="$2"
  python - "$host" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(0.2)
try:
    sock.bind((host, port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

port_is_reserved() {
  local port="$1"
  if [[ "$port" == "$STATION_ZMQ_PORT" || "$port" == "$STATION_SIM_ZMQ_PORT" || "$port" == "$STATION_SIM_ZMQ_OUT_PORT" ]]; then
    return 0
  fi
  for reserved_port in $STATION_RESERVED_PORTS; do
    if [[ "$port" == "$reserved_port" ]]; then
      return 0
    fi
  done
  return 1
}

if [[ "$STATION_SIM_PREVIEW" == "true" ]]; then
  REQUESTED_CAMERA_PORT="$STATION_CAMERA_PORT"
  while port_is_reserved "$STATION_CAMERA_PORT" || ! port_is_free "0.0.0.0" "$STATION_CAMERA_PORT"; do
    if [[ "$STATION_CAMERA_PORT" -ge "$STATION_CAMERA_PORT_MAX" ]]; then
      echo "Error: no free MuJoCo camera port in range $REQUESTED_CAMERA_PORT-$STATION_CAMERA_PORT_MAX."
      echo "Stop the old camera publisher or set STATION_CAMERA_PORT manually."
      exit 1
    fi
    STATION_CAMERA_PORT=$((STATION_CAMERA_PORT + 1))
  done
  if [[ "$STATION_CAMERA_PORT" != "$REQUESTED_CAMERA_PORT" ]]; then
    echo "Camera port $REQUESTED_CAMERA_PORT is busy; using $STATION_CAMERA_PORT."
  fi
fi

ONSCREEN_ARG="--no-enable-onscreen"
if [[ "$STATION_ENABLE_ONSCREEN" == "true" ]]; then
  ONSCREEN_ARG="--enable-onscreen"
fi

BACKEND_PID=""
if [[ "$STATION_START_UI" == "true" ]]; then
  REQUESTED_UI_PORT="$STATION_UI_PORT"
  while ! port_is_free "$STATION_UI_HOST" "$STATION_UI_PORT"; do
    if [[ "$STATION_UI_PORT" -ge "$STATION_UI_PORT_MAX" ]]; then
      echo "Error: no free Sonic Station UI port in range $REQUESTED_UI_PORT-$STATION_UI_PORT_MAX."
      echo "Stop the old backend or set STATION_UI_PORT manually."
      exit 1
    fi
    STATION_UI_PORT=$((STATION_UI_PORT + 1))
  done
  if [[ "$STATION_UI_PORT" != "$REQUESTED_UI_PORT" ]]; then
    echo "Port $REQUESTED_UI_PORT is busy; using $STATION_UI_PORT for Sonic Station UI."
  fi

  python tools/sonic_station/server.py \
    --host "$STATION_UI_HOST" \
    --port "$STATION_UI_PORT" \
    --station-mode "$STATION_MODE" \
    --robot-interface "$STATION_ROBOT_INTERFACE" \
    --robot-ip "$STATION_ROBOT_IP" \
    --camera-host localhost \
    --camera-port "$STATION_CAMERA_PORT" \
    --camera-view-file "$STATION_CAMERA_VIEW_FILE" \
    --zmq-port "$STATION_ZMQ_PORT" \
    --sim-zmq-port "$([[ "$STATION_MODE" == "real" && "$STATION_SIM_PREVIEW" == "true" ]] && printf '%s' "$STATION_SIM_ZMQ_PORT" || printf '0')" \
    >"$STATION_LOG_DIR/backend.log" 2>&1 &
  BACKEND_PID=$!
  sleep 1
  if ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    echo "Error: Sonic Station backend failed to start."
    echo "Check log:"
    echo "  $STATION_LOG_DIR/backend.log"
    echo
    tail -n 40 "$STATION_LOG_DIR/backend.log" || true
    exit 1
  fi
  if [[ "$STATION_MODE" == "real" ]]; then
    echo "Sonic Station UI (REAL ROBOT armed on $STATION_ROBOT_INTERFACE, preview sim enabled):"
    echo "Robot reachability target:"
    echo "  $STATION_ROBOT_IP"
  else
    echo "Sonic Station UI:"
  fi
  echo "  http://$STATION_UI_HOST:$STATION_UI_PORT/"
  echo "Backend log:"
  echo "  $STATION_LOG_DIR/backend.log"
fi

python tools/sonic_station/create_station_idle_reference.py \
  --source "$STATION_IDLE_SOURCE" \
  --output-root "$DEPLOY_DIR/reference/station_idle" \
  --name 00_station_idle \
  --frame 0 \
  --frames "$STATION_IDLE_FRAMES" \
  --force

SIM_PID=""
if [[ "$STATION_SIM_PREVIEW" == "true" ]]; then
  SIM_COMMAND=(
    python gear_sonic/scripts/run_sim_loop.py
    --auto-disable-elastic-after-cmd-sec "$SIM_DISABLE_ELASTIC_DELAY_SEC"
    --enable-image-publish
    --enable-offscreen
    "$ONSCREEN_ARG"
    --image-dt "$STATION_IMAGE_DT"
    --camera-port "$STATION_CAMERA_PORT"
  )
  if [[ "$STATION_SIM_LOG_TO_FILE" == "true" ]]; then
    SONIC_STATION_CAMERA_VIEW_FILE="$STATION_CAMERA_VIEW_FILE" \
      "${SIM_COMMAND[@]}" >"$STATION_LOG_DIR/sim.log" 2>&1 &
    echo "MuJoCo preview log:"
    echo "  $STATION_LOG_DIR/sim.log"
  else
    SONIC_STATION_CAMERA_VIEW_FILE="$STATION_CAMERA_VIEW_FILE" \
      "${SIM_COMMAND[@]}" &
  fi
  SIM_PID=$!
fi

SIM_DEPLOY_PID=""
if [[ "$STATION_MODE" == "real" && "$STATION_SIM_PREVIEW" == "true" && "$STATION_SIM_DEPLOY" == "true" ]]; then
  (
    cd "$DEPLOY_DIR"
    set +e
    # shellcheck disable=SC1091
    source scripts/setup_env.sh
    set -e
    just build
  )
  (
    cd "$DEPLOY_DIR"
    set +e
    # shellcheck disable=SC1091
    source scripts/setup_env.sh
    set -e
    just run g1_deploy_onnx_ref lo policy/release/model_decoder.onnx "$STATION_MOTION_DATA" \
      --obs-config policy/release/observation_config.yaml \
      --encoder-file policy/release/model_encoder.onnx \
      --planner-file planner/target_vel/V2/planner_sonic.onnx \
      --input-type zmq_manager \
      --output-type zmq \
      --zmq-host localhost \
      --zmq-port "$STATION_SIM_ZMQ_PORT" \
      --zmq-out-port "$STATION_SIM_ZMQ_OUT_PORT" \
      --disable-crc-check \
      --auto-control-start
  ) >"$STATION_LOG_DIR/sim_deploy.log" 2>&1 &
  SIM_DEPLOY_PID=$!
  echo "Simulation playback controller:"
  echo "  port: $STATION_SIM_ZMQ_PORT"
  echo "  log:  $STATION_LOG_DIR/sim_deploy.log"
fi

cleanup() {
  if [[ -n "$SIM_DEPLOY_PID" ]] && kill -0 "$SIM_DEPLOY_PID" >/dev/null 2>&1; then
    kill "$SIM_DEPLOY_PID" >/dev/null 2>&1 || true
    wait "$SIM_DEPLOY_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$SIM_PID" ]] && kill -0 "$SIM_PID" >/dev/null 2>&1; then
    kill "$SIM_PID" >/dev/null 2>&1 || true
    wait "$SIM_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    kill "$BACKEND_PID" >/dev/null 2>&1 || true
    wait "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

sleep 2

cd "$DEPLOY_DIR"
DEPLOY_ARGS=(
  --motion-data "$STATION_MOTION_DATA"
  --input-type zmq_manager
  --zmq-port "$STATION_ZMQ_PORT"
)

if [[ "$STATION_MODE" == "sim" ]]; then
  DEPLOY_ARGS+=(--auto-control-start --yes sim)
else
  echo ""
  echo "REAL robot deploy is manual:"
  echo "  1. Press Y yourself at the deploy prompt."
  echo "  2. Press ] yourself in the terminal once the robot is ready."
  echo "No --yes or --auto-control-start is passed to the real robot deploy."
  echo ""
  DEPLOY_ARGS+=("$STATION_ROBOT_INTERFACE")
fi

bash deploy.sh "${DEPLOY_ARGS[@]}" "$@"
