#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  tools/sonic_station/generate_kimodo_reference.sh "prompt" [motion_name]

Environment overrides:
  KIMODO_MODEL=Kimodo-G1-RP-v1
  KIMODO_DURATION=6.0
  KIMODO_OUTPUT_DIR=kimodo_outputs
  KIMODO_INPUT_FPS=30
  SONIC_REFERENCE_ROOT=gear_sonic_deploy/reference/kimodo

Example:
  tools/sonic_station/generate_kimodo_reference.sh \
    "A Unitree G1 robot does a smooth disco dance with arm waves." disco_dance
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
    usage
    exit 0
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROMPT="$1"
MOTION_NAME="${2:-$(date +kimodo_%Y%m%d_%H%M%S)}"

KIMODO_MODEL="${KIMODO_MODEL:-Kimodo-G1-RP-v1}"
KIMODO_DURATION="${KIMODO_DURATION:-6.0}"
KIMODO_OUTPUT_DIR="${KIMODO_OUTPUT_DIR:-kimodo_outputs}"
KIMODO_INPUT_FPS="${KIMODO_INPUT_FPS:-30}"
SONIC_REFERENCE_ROOT="${SONIC_REFERENCE_ROOT:-gear_sonic_deploy/reference/kimodo}"
KIMODO_ENV_DIR="${KIMODO_ENV_DIR:-$ROOT_DIR/.venv_kimodo}"
TEXT_ENCODER_DEVICE="${TEXT_ENCODER_DEVICE:-cpu}"
PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
export TEXT_ENCODER_DEVICE PYTORCH_CUDA_ALLOC_CONF

if [[ -x "$KIMODO_ENV_DIR/bin/kimodo_gen" ]]; then
    KIMODO_GEN="$KIMODO_ENV_DIR/bin/kimodo_gen"
elif command -v kimodo_gen >/dev/null 2>&1; then
    KIMODO_GEN="$(command -v kimodo_gen)"
else
    echo "Error: kimodo_gen is not on PATH."
    echo
    echo "Install Kimodo in an isolated local environment first:"
    echo "  tools/sonic_station/install_kimodo_env.sh"
    echo "  hf auth login"
    echo
    echo "Or install Kimodo in your active environment:"
    echo '  pip install "kimodo[all] @ git+https://github.com/nv-tlabs/kimodo.git"'
    exit 1
fi

mkdir -p "$ROOT_DIR/$KIMODO_OUTPUT_DIR"
OUTPUT_STEM="$ROOT_DIR/$KIMODO_OUTPUT_DIR/$MOTION_NAME"

echo "Generating Kimodo motion:"
echo "  model:    $KIMODO_MODEL"
echo "  duration: $KIMODO_DURATION"
echo "  output:   $OUTPUT_STEM"
"$KIMODO_GEN" "$PROMPT" \
    --model "$KIMODO_MODEL" \
    --duration "$KIMODO_DURATION" \
    --output "$OUTPUT_STEM"

QPOS_CSV="$OUTPUT_STEM.csv"
if [[ ! -f "$QPOS_CSV" ]]; then
    echo "Error: expected Kimodo G1 CSV was not created: $QPOS_CSV"
    echo "Check that the selected model is a Kimodo-G1 model."
    exit 1
fi

echo "Converting Kimodo qpos CSV to SONIC reference format..."
python "$ROOT_DIR/tools/sonic_station/kimodo_qpos_to_reference.py" "$QPOS_CSV" \
    --output-root "$ROOT_DIR/$SONIC_REFERENCE_ROOT" \
    --name "$MOTION_NAME" \
    --input-fps "$KIMODO_INPUT_FPS" \
    --output-fps 50 \
    --force

echo
echo "Ready for SONIC deployment:"
echo "  reference dataset: $SONIC_REFERENCE_ROOT"
echo "  motion folder:     $MOTION_NAME"
