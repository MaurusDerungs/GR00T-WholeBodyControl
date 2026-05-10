#!/usr/bin/env bash
# ============================================================================
# Train ACT policy on G1 WBC data using LeRobot
# ============================================================================
# Prerequisites:
#   1. Run process_dataset.py to clean your recorded data
#   2. Run convert_to_lerobot_act.py to produce the ACT-compatible dataset
#   3. Have lerobot installed (it lives in .venv_data_collection / Docker)
#
# Usage:
#   bash gear_sonic/scripts/train_act.sh [ACT_DATASET_PATH] [OUTPUT_DIR]
#
# Defaults:
#   ACT_DATASET_PATH = outputs/act_dataset
#   OUTPUT_DIR       = outputs/act_checkpoints
# ============================================================================
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ACT_DATASET="${1:-$REPO_ROOT/outputs/act_dataset}"
OUTPUT_DIR="${2:-$REPO_ROOT/outputs/act_checkpoints}"

# ── Step 1: Locate Python with lerobot ──────────────────────────────────────
# Try the data_collection venv first (needs Docker/root), then fall back.
LEROBOT_PYTHON=""
CANDIDATES=(
    "/root/venv/bin/python3"
    "$REPO_ROOT/.venv_data_collection/bin/python3"
    "$REPO_ROOT/.venv_data_collection/bin/python"
)
for py in "${CANDIDATES[@]}"; do
    if "$py" -c "import lerobot" 2>/dev/null; then
        LEROBOT_PYTHON="$py"
        break
    fi
done

if [ -z "$LEROBOT_PYTHON" ]; then
    echo "ERROR: Could not find Python with lerobot installed."
    echo "Run this script inside the Docker container:"
    echo "  cd gear_sonic_deploy && ./docker/run_docker.sh"
    echo "  bash /root/Projects/GR00T-WholeBodyControl/gear_sonic/scripts/train_act.sh"
    exit 1
fi
echo "Using Python: $LEROBOT_PYTHON"

# ── Step 2: Validate dataset ────────────────────────────────────────────────
if [ ! -d "$ACT_DATASET" ]; then
    echo "ERROR: ACT dataset not found at $ACT_DATASET"
    echo ""
    echo "First run the conversion:"
    echo "  python gear_sonic/scripts/convert_to_lerobot_act.py \\"
    echo "      --input outputs/training_data_clean \\"
    echo "      --output outputs/act_dataset"
    exit 1
fi

# Count episodes
N_EPISODES=$(grep -c '"episode_index"' "$ACT_DATASET/meta/episodes.jsonl" || echo 0)
echo "Dataset: $ACT_DATASET ($N_EPISODES episodes)"

if [ "$N_EPISODES" -lt 10 ]; then
    echo ""
    echo "WARNING: Only $N_EPISODES episodes found."
    echo "  ACT typically needs 50-100+ episodes for reliable results."
    echo "  Continuing anyway..."
    echo ""
fi

# ── Step 3: Train ───────────────────────────────────────────────────────────

echo ""
echo "Starting ACT training..."
echo "  Dataset:    $ACT_DATASET"
echo "  Output:     $OUTPUT_DIR"
echo ""

"$LEROBOT_PYTHON" -m lerobot.scripts.train \
    --policy.type act \
    --dataset.repo_id g1_wbc \
    --dataset.root "$ACT_DATASET" \
    --policy.chunk_size 50 \
    --policy.n_action_steps 50 \
    --policy.dim_model 512 \
    --policy.n_encoder_layers 4 \
    --policy.n_heads 8 \
    --policy.use_vae false \
    --policy.kl_weight 10.0 \
    --batch_size 8 \
    --steps 50000 \
    --log_freq 100 \
    --save_freq 5000 \
    --num_workers 4 \
    --output_dir "$OUTPUT_DIR"
