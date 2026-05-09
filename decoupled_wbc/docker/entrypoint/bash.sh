#!/bin/bash
set -e  # Exit on error

PROJ_DIR="${DECOUPLED_WBC_DIR:-$HOME/Projects/GR00T-WholeBodyControl}"
VENV="$PROJ_DIR/.venv_quest_wbc"

# ── Install the quest_wbc venv (first run or broken venv) ──────────────────
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c "" 2>/dev/null; then
    echo "[setup] quest_wbc venv missing or broken — running install_quest_wbc.sh ..."
    set +u
    source /opt/ros/humble/setup.bash 2>/dev/null || true
    set -u
    bash "$PROJ_DIR/install_scripts/install_quest_wbc.sh"
fi

# ── Sanity-check that optional packages are installed ───────────────────────
# oculus_reader: needed for --body_control_device oculus (Quest controller streaming)
if ! "$VENV/bin/python" -c "import oculus_reader" 2>/dev/null; then
    echo "[setup] oculus_reader not found — installing ..."
    # GIT_LFS_SKIP_SMUDGE=1 skips the Quest APK binary (not needed for Python)
    # --python ensures uv targets the correct venv, not UV_PYTHON from the image
    if command -v uv &>/dev/null; then
        GIT_LFS_SKIP_SMUDGE=1 uv pip install git+https://github.com/rail-berkeley/oculus_reader.git \
            --python "$VENV/bin/python" --quiet
    else
        GIT_LFS_SKIP_SMUDGE=1 "$VENV/bin/pip" install git+https://github.com/rail-berkeley/oculus_reader.git --quiet
    fi
fi

# motionbricks: needed for SONIC motion-token encoder during Quest data collection
if ! "$VENV/bin/python" -c "import motionbricks" 2>/dev/null; then
    echo "[setup] motionbricks not found — installing ..."
    "$VENV/bin/python" -m pip install -e "$PROJ_DIR/motionbricks" --quiet
fi

# ── Ensure sshpass is available (needed for Jetson SSH in teleoperation_launch.py) ──
if ! command -v sshpass &>/dev/null; then
    echo "[setup] Installing sshpass ..."
    apt-get install -y -qq sshpass 2>/dev/null || echo "[setup] Could not install sshpass — SSH to Jetson will prompt for password"
fi

# Fix SSH config permissions if needed
if [ -f "$HOME/.ssh/config" ]; then
    chmod 600 "$HOME/.ssh/config"
fi

# ── Install the data_collection venv (first run or broken venv) ────────────
DATA_VENV="$PROJ_DIR/.venv_data_collection"
if [ ! -x "$DATA_VENV/bin/python" ] || ! "$DATA_VENV/bin/python" -c "import numpy" 2>/dev/null; then
    echo "[setup] data_collection venv missing or incomplete — running install_data_collection.sh ..."
    bash "$PROJ_DIR/install_scripts/install_data_collection.sh"
fi

# ── Write ~/.bashrc activation block once ──────────────────────────────────
if ! grep -q 'quest_wbc' "$HOME/.bashrc" 2>/dev/null; then
    cat >> "$HOME/.bashrc" << 'BASHRC_EOF'

# ── Quest WBC environment (auto-added by bash.sh entrypoint) ──────────────────
set +u
source /opt/ros/humble/setup.bash 2>/dev/null || true
set -u
VENV_ACTIVATE="${DECOUPLED_WBC_DIR:-$HOME/Projects/GR00T-WholeBodyControl}/.venv_quest_wbc/bin/activate"
[ -f "$VENV_ACTIVATE" ] && source "$VENV_ACTIVATE"
unset VENV_ACTIVATE
# ──────────────────────────────────────────────────────────────────────────────
BASHRC_EOF
    echo "[setup] Activation lines written to ~/.bashrc"
fi

echo "Quest WBC environment ready. Starting interactive bash shell..."
exec /bin/bash
