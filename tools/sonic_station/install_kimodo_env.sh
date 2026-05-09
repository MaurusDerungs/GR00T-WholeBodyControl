#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_DIR="${KIMODO_ENV_DIR:-$ROOT_DIR/.venv_kimodo}"
PYTHON_BIN="${KIMODO_BOOTSTRAP_PYTHON:-python3.10}"

if [[ -x "$ENV_DIR/bin/python" ]]; then
    echo "Kimodo environment already exists:"
    echo "  $ENV_DIR"
else
    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        echo "Error: $PYTHON_BIN not found."
        echo "Kimodo is tested with Python 3.10. Install python3.10 or set KIMODO_BOOTSTRAP_PYTHON."
        echo "Example:"
        echo "  KIMODO_BOOTSTRAP_PYTHON=/path/to/python3.10 tools/sonic_station/install_kimodo_env.sh"
        exit 1
    fi

    "$PYTHON_BIN" -m venv "$ENV_DIR"
fi

# shellcheck disable=SC1091
source "$ENV_DIR/bin/activate"

python -m pip install --upgrade pip wheel setuptools
python -m pip install --upgrade huggingface_hub
python -m pip install "kimodo[all] @ git+https://github.com/nv-tlabs/kimodo.git"
python -m pip install "protobuf>=4,<6" sentencepiece
python "$ROOT_DIR/tools/sonic_station/patch_kimodo_text_encoder.py"

echo
echo "Kimodo environment ready:"
echo "  source $ENV_DIR/bin/activate"
echo
echo "Before generating motions, make sure Hugging Face auth is configured:"
echo "  hf auth login"
