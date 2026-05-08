#!/usr/bin/env python3
"""
check_quest_wbc_env.py — Pre-flight environment check for Quest WBC teleop.

Usage:
    python scripts/check_quest_wbc_env.py              # full check
    python scripts/check_quest_wbc_env.py --mode bridge       # bridge only
    python scripts/check_quest_wbc_env.py --mode robot-teleop # ROS+WBC checks
    python scripts/check_quest_wbc_env.py --mode all          # same as default
"""

from __future__ import annotations

import argparse
import importlib
import os
import socket
import ssl
import sys
from pathlib import Path


# ── helpers ────────────────────────────────────────────────────────────────────

def _pass(name: str, detail: str = "") -> bool:
    print(f"  [+] {name}" + (f": {detail}" if detail else ""))
    return True


def _fail(name: str, detail: str = "") -> bool:
    print(f"  [X] {name}" + (f": {detail}" if detail else ""))
    return False


def check(name: str, ok: bool, pass_msg: str = "", fail_msg: str = "") -> bool:
    if ok:
        return _pass(name, pass_msg)
    return _fail(name, fail_msg)


def _import(module: str) -> tuple[bool, object | None]:
    try:
        return True, importlib.import_module(module)
    except ImportError as exc:
        return False, str(exc)


# ── individual checks ──────────────────────────────────────────────────────────

def check_python() -> bool:
    v = sys.version_info
    ok = v.major == 3 and v.minor == 10
    return check(
        "Python version",
        ok,
        pass_msg=f"{v.major}.{v.minor}.{v.micro}",
        fail_msg=f"{v.major}.{v.minor}.{v.micro} (need 3.10.x — Quest WBC requirement)",
    )


def check_venv() -> bool:
    in_venv = sys.prefix != sys.base_prefix
    name = Path(sys.prefix).name
    return check(
        "Virtual environment",
        in_venv,
        pass_msg=f"{name} ({sys.prefix})",
        fail_msg="no venv active — run: source .venv_quest_wbc/bin/activate",
    )


def check_package(pip_name: str, import_name: str | None = None, version_attr: str = "__version__") -> bool:
    import_name = import_name or pip_name
    ok, mod = _import(import_name)
    if not ok:
        return _fail(pip_name, f"not installed — pip install {pip_name}")
    version = getattr(mod, version_attr, None) if ok else None
    return _pass(pip_name, str(version) if version else "imported")


def check_rclpy() -> bool:
    ok, mod = _import("rclpy")
    if not ok:
        return _fail(
            "rclpy (ROS 2)",
            "not found — source your ROS 2 setup.bash before activating the venv\n"
            "         e.g.  source $CONDA_PREFIX/setup.bash\n"
            "               source .venv_quest_wbc/bin/activate",
        )
    return _pass("rclpy (ROS 2)", "imported")


def check_ros_msgs() -> bool:
    results = []
    for mod in ("sensor_msgs.msg", "std_msgs.msg", "std_srvs.srv"):
        ok, _ = _import(mod)
        results.append(ok)
        if not ok:
            _fail(mod, "missing — ROS 2 messages not on PYTHONPATH")
    return all(results)


def check_unitree() -> bool:
    ok, _ = _import("unitree_sdk2py")
    return check(
        "unitree_sdk2py",
        ok,
        pass_msg="imported",
        fail_msg="not installed — run install_quest_wbc.sh",
    )


def check_decoupled_wbc() -> bool:
    ok, _ = _import("decoupled_wbc")
    return check(
        "decoupled_wbc",
        ok,
        pass_msg="imported",
        fail_msg="not installed — pip install -e decoupled_wbc",
    )


def check_quest_bridge_cert() -> bool:
    cert = Path(".quest_bridge/cert.pem")
    key = Path(".quest_bridge/key.pem")
    if not cert.exists() or not key.exists():
        return _fail(
            "Quest bridge TLS cert",
            "missing — run install_quest_wbc.sh to generate",
        )
    # Try loading it
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(str(cert), str(key))
        return _pass("Quest bridge TLS cert", str(cert))
    except ssl.SSLError as exc:
        return _fail("Quest bridge TLS cert", f"invalid: {exc}")


def check_quest_device_files() -> bool:
    required = [
        "decoupled_wbc/control/teleop/device/quest/__init__.py",
        "decoupled_wbc/control/teleop/device/quest/quest_bridge.py",
        "decoupled_wbc/control/teleop/device/quest/quest_client.py",
        "decoupled_wbc/control/teleop/device/quest/web/index.html",
        "decoupled_wbc/control/teleop/streamers/quest_streamer.py",
        "decoupled_wbc/control/teleop/main/run_quest_bridge.py",
    ]
    all_ok = True
    for path in required:
        if Path(path).exists():
            _pass(f"  {path}")
        else:
            _fail(f"  {path}", "MISSING")
            all_ok = False
    return all_ok


def check_mujoco() -> bool:
    ok, mod = _import("mujoco")
    if not ok:
        return _fail("mujoco", "not installed — pip install mujoco")
    return _pass("mujoco", getattr(mod, "__version__", "imported"))


def check_pinocchio() -> bool:
    ok, _ = _import("pinocchio")
    return check("pinocchio (pin)", ok, pass_msg="imported", fail_msg="not installed — pip install pin")


def check_pink() -> bool:
    ok, _ = _import("pink")
    return check("pink (pin-pink)", ok, pass_msg="imported", fail_msg="not installed — pip install pin-pink")


def check_qpsolvers() -> bool:
    ok, _ = _import("qpsolvers")
    return check("qpsolvers", ok, pass_msg="imported", fail_msg="not installed — pip install 'qpsolvers[osqp,quadprog]'")


def check_gymnasium() -> bool:
    ok, _ = _import("gymnasium")
    return check("gymnasium", ok, pass_msg="imported", fail_msg="not installed — pip install gymnasium")


def check_network_port(host: str = "127.0.0.1", port: int = 8765) -> bool:
    """Check if the quest bridge port can be bound."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return _pass(f"Port {port} bindable", f"on {host}")
    except OSError as exc:
        return _fail(f"Port {port}", f"already in use or not bindable: {exc}")


# ── modes ──────────────────────────────────────────────────────────────────────

def run_bridge_checks() -> list[bool]:
    print("\n── Quest Bridge checks (Terminal 1) ─────────────────────────────")
    results = [
        check_python(),
        check_venv(),
        check_decoupled_wbc(),
        check_quest_bridge_cert(),
    ]
    print("\n  Quest device source files:")
    results.append(check_quest_device_files())
    results.append(check_network_port())
    return results


def run_robot_teleop_checks() -> list[bool]:
    print("\n── Robot-side teleop checks (Terminals 2 & 3) ──────────────────")
    results = [
        check_python(),
        check_venv(),
        check_rclpy(),
        check_ros_msgs(),
        check_unitree(),
        check_decoupled_wbc(),
        check_mujoco(),
        check_pinocchio(),
        check_pink(),
        check_qpsolvers(),
        check_gymnasium(),
    ]
    print("\n  Optional packages:")
    for pkg, imp in [
        ("meshcat-shapes", "meshcat_shapes"),
        ("rerun-sdk", "rerun"),
        ("loguru", "loguru"),
        ("termcolor", "termcolor"),
        ("sshkeyboard", "sshkeyboard"),
        ("opencv-python", "cv2"),
        ("onnxruntime", "onnxruntime"),
        ("pygame", "pygame"),
        ("glfw", "glfw"),
        ("msgpack", "msgpack"),
        ("msgpack-numpy", "msgpack_numpy"),
        ("pyzmq", "zmq"),
    ]:
        ok, _ = _import(imp)
        if ok:
            _pass(f"    {pkg}")
        else:
            _fail(f"    {pkg}", "not installed")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-flight check for Quest WBC teleop environment."
    )
    parser.add_argument(
        "--mode",
        choices=["bridge", "robot-teleop", "all"],
        default="all",
        help="Which subset of checks to run (default: all).",
    )
    args = parser.parse_args()

    # Make sure relative paths work even when the script is invoked from a
    # different directory — resolve against the repo root.
    repo_root = Path(__file__).resolve().parents[1]
    os.chdir(repo_root)

    print("═" * 60)
    print("  Quest WBC Teleop — Environment Check")
    print("═" * 60)
    print(f"  Python  : {sys.executable}")
    print(f"  Prefix  : {sys.prefix}")

    all_results: list[bool] = []

    if args.mode in ("bridge", "all"):
        all_results.extend(run_bridge_checks())

    if args.mode in ("robot-teleop", "all"):
        all_results.extend(run_robot_teleop_checks())

    passed = sum(1 for r in all_results if r)
    failed = sum(1 for r in all_results if not r)

    print("\n" + "═" * 60)
    if failed == 0:
        print(f"  All {passed} checks passed ✓")
    else:
        print(f"  {passed} passed  |  {failed} FAILED")
        print("  Fix the [X] items above before running the teleop pipeline.")
    print("═" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
