#!/usr/bin/env bash
# install_quest_wbc.sh
# Sets up the .venv_quest_wbc venv for Meta Quest 3S teleoperation with the
# decoupled whole-body controller (WBC).
#
# What this installs:
#   .venv_quest_wbc  — decoupled_wbc[quest_wbc] + unitree_sdk2_python
#
# What must be installed separately BEFORE running this script:
#   ROS 2 Humble  — run:  bash install_scripts/install_ros.sh
#                 (installs ros-humble-desktop into the active conda env via
#                  conda robostack-staging)
#
# Usage:
#   bash install_scripts/install_quest_wbc.sh   (run from repo root)
#
# Three processes to launch (each in its own terminal):
#   1. Quest Bridge (quest-side)
#      source .venv_quest_wbc/bin/activate
#      python decoupled_wbc/control/teleop/main/run_quest_bridge.py \
#          --public-host <YOUR_LAN_IP> --port 8765
#
#   2. G1 Control Loop (robot-side)
#      source "$CONDA_PREFIX/setup.bash" && source .venv_quest_wbc/bin/activate
#      python decoupled_wbc/control/main/teleop/run_g1_control_loop.py \
#          --interface sim --enable_onscreen --no-enable_waist
#
#   3. Teleop Policy Loop (quest + robot glue)
#      source "$CONDA_PREFIX/setup.bash" && source .venv_quest_wbc/bin/activate
#      python decoupled_wbc/control/main/teleop/run_teleop_policy_loop.py \
#          --body_control_device quest --hand_control_device quest \
#          --quest_bridge_host 127.0.0.1 --quest_bridge_port 8765 \
#          --enable_real_device --no-enable_visualization

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── 0. Detect architecture ────────────────────────────────────────────────────
ARCH="$(uname -m)"
echo "[OK] Architecture: $ARCH"

# ── 1. Check ROS 2 is available ──────────────────────────────────────────────
# ROS 2 provides rclpy which is NOT pip-installable — it must come from either
# a robostack conda environment or a system /opt/ros/<distro> install.
ROS_SETUP_FILE=""

# Priority 1: current conda env (robostack installs setup.bash into $CONDA_PREFIX)
if [ -n "${CONDA_PREFIX:-}" ] && [ -f "$CONDA_PREFIX/setup.bash" ]; then
    ROS_SETUP_FILE="$CONDA_PREFIX/setup.bash"
    echo "[OK] Found ROS 2 conda setup: $ROS_SETUP_FILE"
# Priority 2: system-wide ROS 2 install
elif [ -f "/opt/ros/humble/setup.bash" ]; then
    ROS_SETUP_FILE="/opt/ros/humble/setup.bash"
    echo "[OK] Found ROS 2 system install: $ROS_SETUP_FILE"
else
    echo ""
    echo "[ERROR] ROS 2 Humble not found."
    echo "        The control loop and teleop policy loop both require rclpy."
    echo ""
    echo "  To install ROS 2 Humble via conda/robostack run:"
    echo "    bash install_scripts/install_ros.sh"
    echo ""
    echo "  Then re-run this script."
    exit 1
fi

# Source ROS 2 so we can introspect its Python site-packages below
# Temporarily disable nounset (-u) because ROS setup.bash references
# AMENT_TRACE_SETUP_FILES without a default value, which triggers "unbound
# variable" errors when the caller has `set -u` active.
set +u
# shellcheck disable=SC1090
source "$ROS_SETUP_FILE"
set -u

ROS_PYTHON_SITE="$(python3 -c 'import rclpy, os; print(os.path.dirname(os.path.dirname(rclpy.__file__)))' 2>/dev/null || true)"
if [ -z "$ROS_PYTHON_SITE" ]; then
    echo "[ERROR] rclpy not importable even after sourcing $ROS_SETUP_FILE"
    echo "        Check your ROS 2 installation and try again."
    exit 1
fi
echo "[OK] ROS 2 Python site-packages: $ROS_PYTHON_SITE"

# ── 2. Ensure uv is installed ─────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "[INFO] uv not found – installing via official installer …"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck disable=SC1091
        source "$HOME/.local/bin/env"
    elif [ -f "$HOME/.cargo/env" ]; then
        # shellcheck disable=SC1091
        source "$HOME/.cargo/env"
    else
        export PATH="$HOME/.local/bin:$PATH"
    fi
    if ! command -v uv &>/dev/null; then
        echo "[ERROR] uv installation succeeded but binary not found on PATH."
        echo "        Please add ~/.local/bin (or ~/.cargo/bin) to your PATH."
        exit 1
    fi
fi
echo "[OK] uv $(uv --version)"

# ── 3. Install a uv-managed Python 3.10 ──────────────────────────────────────
echo "[INFO] Installing uv-managed Python 3.10 …"
uv python install 3.10
MANAGED_PY="$(uv python find --no-project 3.10)"
echo "[OK] Using Python: $MANAGED_PY"

# ── 4. Remove stale venv ──────────────────────────────────────────────────────
cd "$REPO_ROOT"
echo "[INFO] Removing old .venv_quest_wbc (if present) …"
rm -rf .venv_quest_wbc

# ── 5. Create venv ────────────────────────────────────────────────────────────
echo "[INFO] Creating .venv_quest_wbc with uv-managed Python 3.10 …"
uv venv .venv_quest_wbc --python "$MANAGED_PY" --prompt quest_wbc
# shellcheck disable=SC1091
source .venv_quest_wbc/bin/activate
# UV_PYTHON may be set in the Docker image (ENV UV_PYTHON=/root/venv/bin/python)
# which would redirect all `uv pip install` calls to the wrong venv.  Unset it
# so uv respects the activated virtual environment instead.
unset UV_PYTHON

# ── 6. Inject ROS 2 site-packages into the venv via a .pth file ──────────────
# This lets Python find rclpy, sensor_msgs, std_msgs, etc. without needing to
# source the ROS setup.bash in every terminal (sourcing is still required for
# RMW / ament env vars, but at least the imports resolve correctly).
VENV_SITE="$(python -c 'import site; print(site.getsitepackages()[0])')"
PTH_FILE="$VENV_SITE/ros2_quest_wbc.pth"
echo "$ROS_PYTHON_SITE" > "$PTH_FILE"
echo "[OK] ROS 2 site-packages linked via $PTH_FILE"

# Verify the linkage
python -c "import rclpy; print('[OK] rclpy', rclpy.__version__ if hasattr(rclpy,'__version__') else 'imported')" 2>/dev/null || {
    echo "[WARN] rclpy still not importable inside the venv."
    echo "       You will need to source $ROS_SETUP_FILE before running scripts."
}

# ── 7. Install decoupled_wbc[quest_wbc] ──────────────────────────────────────
echo "[INFO] Installing decoupled_wbc[quest_wbc] …"
uv pip install -e "decoupled_wbc[quest_wbc]"

# ── 8. Install unitree_sdk2_python ───────────────────────────────────────────
echo "[INFO] Installing unitree_sdk2_python …"
uv pip install -e external_dependencies/unitree_sdk2_python

# ── 9. Install decoupled_wbc itself (editable, no extras) ────────────────────
# (already installed as part of quest_wbc, but make sure it is editable)
echo "[INFO] Verifying decoupled_wbc editable install …"
uv pip install -e decoupled_wbc --no-deps

# ── 9b. Install oculus_reader (USB/ADB direct Quest streaming) ────────────────
# oculus_reader lets Python read Quest controller poses via ADB over USB without
# a Wi-Fi bridge. Needed only if you use --body_control_device oculus.
echo "[INFO] Installing oculus_reader …"
# GIT_LFS_SKIP_SMUDGE=1 skips the Quest APK binary (not needed for Python reader)
# --python targets the active venv explicitly, ignoring UV_PYTHON from the Docker image
GIT_LFS_SKIP_SMUDGE=1 uv pip install git+https://github.com/rail-berkeley/oculus_reader.git \
    --python "$(which python)"

# ── 9c. Install motionbricks (SONIC motion-token encoder) ────────────────────
# Required for encoding G1 joint state → 64-D SONIC motion tokens during
# Quest data collection (--enable_sonic_data_collection).
echo "[INFO] Installing motionbricks …"
uv pip install -e motionbricks

# Install ADB tools (required by oculus_reader at runtime)
if ! command -v adb &>/dev/null; then
    echo "[INFO] adb not found – installing android-tools-adb …"
    sudo apt-get install -y android-tools-adb 2>/dev/null || \
        echo "[WARN] Could not auto-install adb. Install manually: sudo apt install android-tools-adb"
else
    echo "[OK] adb $(adb --version | head -1)"
fi

# ── 10. Generate self-signed certificate for the Quest HTTPS bridge ───────────
echo "[INFO] Generating self-signed TLS certificate for Quest bridge …"
CERT_DIR="$REPO_ROOT/.quest_bridge"
mkdir -p "$CERT_DIR"
if [ ! -f "$CERT_DIR/cert.pem" ] || [ ! -f "$CERT_DIR/key.pem" ]; then
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$CERT_DIR/key.pem" \
        -out   "$CERT_DIR/cert.pem" \
        -subj "/CN=quest-teleop-bridge" \
        -addext "subjectAltName=IP:127.0.0.1" 2>/dev/null \
    || openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$CERT_DIR/key.pem" \
        -out   "$CERT_DIR/cert.pem" \
        -subj "/CN=quest-teleop-bridge" 2>/dev/null
    echo "[OK] Certificate generated: $CERT_DIR/cert.pem"
else
    echo "[OK] Certificate already present: $CERT_DIR/cert.pem"
fi

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  IMPORTANT: The control loop and teleop policy loop require"
echo "  both ROS 2 env vars AND the venv. Activate with:"
echo ""
echo "    source \"$ROS_SETUP_FILE\""
echo "    source .venv_quest_wbc/bin/activate"
echo ""
echo "  The quest bridge only needs the venv (no ROS 2 required):"
echo "    source .venv_quest_wbc/bin/activate"
echo ""
echo "  Verify the environment:"
echo "    python scripts/check_quest_wbc_env.py"
echo ""
echo "  ── Terminal 1 (Quest Bridge) ──────────────────────────────"
echo "  source .venv_quest_wbc/bin/activate"
echo "  python decoupled_wbc/control/teleop/main/run_quest_bridge.py \\"
echo "    --public-host <YOUR_LAN_IP> --port 8765"
echo ""
echo "  ── Terminal 2 (G1 Control Loop) ───────────────────────────"
echo "  source \"$ROS_SETUP_FILE\""
echo "  source .venv_quest_wbc/bin/activate"
echo "  python decoupled_wbc/control/main/teleop/run_g1_control_loop.py \\"
echo "    --interface sim --enable_onscreen --no-enable_waist"
echo ""
echo "  ── Terminal 3 (Teleop Policy Loop) ────────────────────────"
echo "  source \"$ROS_SETUP_FILE\""
echo "  source .venv_quest_wbc/bin/activate"
echo "  python decoupled_wbc/control/main/teleop/run_teleop_policy_loop.py \\"
echo "    --body_control_device quest --hand_control_device quest \\"
echo "    --quest_bridge_host 127.0.0.1 --quest_bridge_port 8765 \\"
echo "    --enable_real_device --no-enable_visualization"
echo "══════════════════════════════════════════════════════════════"
