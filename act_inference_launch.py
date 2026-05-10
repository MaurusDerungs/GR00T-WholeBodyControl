#!/usr/bin/env python3
"""
Unified G1 ACT inference launcher (tmux-based).

Runs a LeRobot-trained ACT policy on the real (or sim) G1 robot, pulling the
ego-view frame from the Jetson-mounted RealSense camera over GStreamer + ZMQ
— the exact same camera pipeline that ``teleoperation_launch.py`` uses so the
deployment matches training conditions.

Usage (from repo root, inside Docker):
  python act_inference_launch.py real
  python act_inference_launch.py real --task "pick up the cup"
  python act_inference_launch.py real --checkpoint outputs/act_checkpoints/checkpoints/050000
  python act_inference_launch.py real --jetson-camera-device /dev/video6
  python act_inference_launch.py sim --checkpoint ...

Layout (real mode, 3 panes — no data recording, no teleop):
  ┌─────────────────────┬──────────────────────┐
  │  ctrl + ACT policy  │  Jetson GStreamer    │
  │  (run_g1_act_infer) ├──────────────────────┤
  │                     │  camera ZMQ server   │
  └─────────────────────┴──────────────────────┘

Sim mode uses only one pane (the control loop drives the MuJoCo sim and the
camera is expected to come from the sim itself or a pre-recorded stream).

Ctrl+\\ in any pane kills the whole session. Ctrl+b d detaches and also kills
all processes (same as teleoperation_launch.py).
"""

import argparse
import os
import re
import subprocess
import sys
import time

# ── Constants (identical to teleoperation_launch.py) ──────────────────────────
ROBOT_IP     = "192.168.123.164"
ROBOT_SUBNET = "192.168.123"
PC_STATIC_IP = "192.168.123.99"
JETSON_IP    = "192.168.123.164"
JETSON_USER  = "unitree"
JETSON_PASS  = "123"
SESSION      = "g1_act"

PROJECT      = os.path.dirname(os.path.abspath(__file__))
# Camera / ZMQ publisher lives in the data-collection venv (has lerobot + zmq).
DATA_VENV    = os.path.join(PROJECT, ".venv_data_collection")
DATA_PYTHON  = os.path.join(DATA_VENV, "bin", "python")
# The control loop + ACT policy use the same venv as training (it has torch,
# lerobot, torchcodec and decoupled_wbc).  Fall back to /root/venv (docker).
CONTROL_VENV_CANDIDATES = [
    "/root/venv",
    os.path.join(PROJECT, ".venv_data_collection"),
]


def _find_control_python() -> str:
    """Pick the first Python env that has both lerobot and decoupled_wbc."""
    for base in CONTROL_VENV_CANDIDATES:
        py = os.path.join(base, "bin", "python")
        if not os.path.isfile(py):
            continue
        probe = subprocess.run(
            [py, "-c", "import lerobot, decoupled_wbc"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return py
    raise RuntimeError(
        "No Python environment found with both 'lerobot' and 'decoupled_wbc'.\n"
        "Tried: " + ", ".join(CONTROL_VENV_CANDIDATES)
    )


# ── Network helpers (lifted verbatim from teleoperation_launch.py) ────────────

def _ip_addrs():
    try:
        out = subprocess.check_output(["/sbin/ip", "addr", "show"], text=True)
    except Exception:
        return {}
    ifaces, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"^\d+:\s+(\S+?)[@:]", line)
        if m:
            cur = m.group(1)
            ifaces.setdefault(cur, [])
        m2 = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m2 and cur:
            ifaces[cur].append(m2.group(1))
    return ifaces


def find_robot_interface():
    ifaces = _ip_addrs()
    for iface, ips in ifaces.items():
        for ip in ips:
            if ip.startswith(ROBOT_SUBNET + ".") and ip != ROBOT_IP:
                return iface, ip, False
    skip = ("lo", "docker", "br-", "veth", "wl")
    candidates = [i for i in ifaces if not any(i.startswith(p) for p in skip)]
    if not candidates:
        candidates = [i for i in ifaces if not i.startswith("lo")]
    if candidates:
        return candidates[0], None, True
    return None, None, True


def configure_robot_interface(iface, current_ip):
    if current_ip == PC_STATIC_IP:
        print(f"[net] {iface} already has {PC_STATIC_IP} — nothing to do.")
        return
    print(f"[net] Assigning {PC_STATIC_IP}/24 to {iface} via sudo …")
    r = subprocess.run(
        ["sudo", "ip", "addr", "add", f"{PC_STATIC_IP}/24", "dev", iface],
        capture_output=True, text=True,
    )
    if r.returncode != 0 and "File exists" not in r.stderr:
        print(f"[net] WARNING: {r.stderr.strip()}")
        return
    subprocess.run(["sudo", "ip", "link", "set", iface, "up"], check=False)
    print(f"[net] {iface} → {PC_STATIC_IP}/24 ✓")


def check_robot_reachable():
    return subprocess.run(
        ["ping", "-c", "1", "-W", "2", ROBOT_IP], capture_output=True
    ).returncode == 0


# ── tmux helpers ──────────────────────────────────────────────────────────────

def tmux(*args):
    subprocess.run(["tmux"] + list(args), check=False)


def session_exists():
    return subprocess.run(
        ["tmux", "has-session", "-t", SESSION], capture_output=True
    ).returncode == 0


def kill_session():
    if session_exists():
        tmux("kill-session", "-t", SESSION)
        time.sleep(0.5)


def create_session(with_camera: bool):
    """
    With camera (3 panes — real mode):
      pane 0 (left)        — control loop + ACT policy
      pane 1 (right-top)   — Jetson GStreamer (SSH)
      pane 2 (right-bot)   — camera ZMQ server

    Without camera (1 pane — sim mode, no external camera):
      pane 0 — control loop + ACT policy (image source from sim)
    """
    kill_session()
    tmux("new-session", "-d", "-s", SESSION, "-x", "220", "-y", "60")
    tmux("rename-window", "-t", f"{SESSION}:0", SESSION)
    # Kill the whole session when the last client detaches (Ctrl+b d).
    tmux(
        "set-hook", "-t", SESSION, "client-detached",
        f"run-shell 'tmux kill-session -t {SESSION} 2>/dev/null'",
    )
    if with_camera:
        tmux("split-window", "-t", f"{SESSION}:0.0", "-h")  # left | right
        tmux("split-window", "-t", f"{SESSION}:0.1", "-v")  # right top | right bottom
    tmux("select-pane", "-t", f"{SESSION}:0.0")


def send(pane: int, cmd: str, wait: float = 0.5):
    tmux("send-keys", "-t", f"{SESSION}:0.{pane}", cmd, "Enter")
    if wait:
        time.sleep(wait)


def kill_hook():
    return f"trap 'tmux kill-session -t {SESSION}' QUIT"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Launch a trained ACT policy on the G1 robot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("mode", choices=["real", "sim"],
                        help="'real' for physical robot, 'sim' for MuJoCo simulation")
    parser.add_argument("--checkpoint", default=None, metavar="PATH",
                        help="Path to the ACT checkpoint directory.  Accepts either a "
                             "training checkpoint (e.g. outputs/act_checkpoints/checkpoints/last) "
                             "or the inner pretrained_model/ directory.  "
                             "Default: outputs/act_checkpoints/checkpoints/last")
    parser.add_argument("--task", default="demo", metavar="TEXT",
                        help="Task prompt (currently informational only; ACT is not "
                             "conditioned on language).  Default: 'demo'.")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"],
                        help="Torch device for ACT inference (default: cuda).")
    parser.add_argument("--control-freq", type=int, default=50, metavar="HZ",
                        help="Control-loop rate in Hz (default: 50, matching the "
                             "frequency at which the dataset was recorded).")
    parser.add_argument("--hold-pose-seconds", type=float, default=1.0, metavar="SEC",
                        help="How long to hold the initial pose before switching to "
                             "policy actions (default: 1.0s).  Gives the camera + "
                             "safety-monitor ramp time to settle.")
    parser.add_argument("--camera-udp-port", type=int, default=5000, metavar="PORT",
                        help="UDP port the Jetson streams H264 video to (default: 5000).")
    parser.add_argument("--camera-zmq-port", type=int, default=5555, metavar="PORT",
                        help="ZMQ port the camera server publishes on (default: 5555).")
    parser.add_argument("--jetson-ip", default=JETSON_IP, metavar="IP",
                        help=f"IP of the G1 Jetson (default: {JETSON_IP}).")
    parser.add_argument("--jetson-camera-device", default="/dev/video5", metavar="DEV",
                        help="V4L2 device on the Jetson (default: /dev/video5 = "
                             "RealSense color high-res).")
    parser.add_argument("--jetson-gst-format", default="auto",
                        choices=["auto", "mjpeg", "raw"],
                        help="Camera pixel format on the Jetson: "
                             "mjpeg=USB cameras outputting JPEG, raw=YUY2/BGR, "
                             "auto=try raw then mjpeg (default: auto).")
    args = parser.parse_args()

    # Default checkpoint (same convention as train_act.sh)
    if args.checkpoint is None:
        args.checkpoint = os.path.join(
            PROJECT, "outputs", "act_checkpoints", "checkpoints", "last"
        )

    print(f"\n{'='*60}")
    print(f"  G1 ACT Launcher  |  mode={args.mode}")
    print(f"  checkpoint       |  {args.checkpoint}")
    print(f"  device           |  {args.device}")
    print(f"  task (info)      |  {args.task}")
    print(f"{'='*60}\n")

    # ── Validate checkpoint early ────────────────────────────────────────────
    if not os.path.isdir(args.checkpoint):
        print(f"[ERROR] Checkpoint directory not found: {args.checkpoint}")
        sys.exit(1)
    cfg_candidates = [
        os.path.join(args.checkpoint, "config.json"),
        os.path.join(args.checkpoint, "pretrained_model", "config.json"),
    ]
    if not any(os.path.isfile(p) for p in cfg_candidates):
        print(f"[ERROR] No 'config.json' found under {args.checkpoint}.")
        print("        Expected a LeRobot pretrained_model directory.")
        sys.exit(1)

    # ── Resolve control/policy Python env ────────────────────────────────────
    try:
        control_python = _find_control_python()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
    print(f"[launch] Control Python: {control_python}")

    # ── Network setup (real only) ────────────────────────────────────────────
    if args.mode == "real":
        iface, current_ip, needs_config = find_robot_interface()
        if iface is None:
            print("[ERROR] No network interface found.")
            sys.exit(1)
        print(f"[net] Robot NIC: {iface}  (IP: {current_ip or 'none'})")
        if needs_config or not (current_ip or "").startswith(ROBOT_SUBNET):
            configure_robot_interface(iface, current_ip)
            time.sleep(1)
        if check_robot_reachable():
            print(f"[net] {ROBOT_IP} reachable ✓")
        else:
            print(f"[net] WARNING: {ROBOT_IP} not responding — is the robot on?")
        interface = iface
        with_camera = True
    else:
        interface = "sim"
        with_camera = False

    # ── Build the control + ACT command ──────────────────────────────────────
    infer_args = [
        control_python,
        "-m", "decoupled_wbc.control.main.teleop.run_g1_act_inference",
        "--interface", interface,
        "--no-with_hands",
        "--enable_onscreen" if args.mode == "sim" else "--no-enable_onscreen",
        "--act_checkpoint", args.checkpoint,
        "--act_device", args.device,
        "--act_hold_pose_on_startup_s", str(args.hold_pose_seconds),
        "--camera_host", "localhost",
        "--camera_port", str(args.camera_zmq_port),
        "--control_frequency", str(args.control_freq),
    ]
    infer_cmd = " ".join(infer_args)

    # ── Create tmux session ──────────────────────────────────────────────────
    create_session(with_camera=with_camera)
    hook = kill_hook()

    # Pane 0 — control loop + ACT policy.
    # Give the camera pipeline a head start when running on the real robot.
    print("[launch] Starting ACT inference loop …")
    if with_camera:
        # Wait for camera ZMQ server to come up before the control loop connects.
        send(0, f"{hook}; cd {PROJECT}; sleep 6; {infer_cmd}", wait=1)
    else:
        send(0, f"{hook}; cd {PROJECT}; {infer_cmd}", wait=1)

    # Panes 1/2 — camera pipeline (only for real robot).
    if with_camera:
        if not os.path.isfile(DATA_PYTHON):
            print(f"[launch] ERROR: data venv not found at {DATA_VENV}.")
            print("[launch] The camera server lives there; re-enter Docker to install it.")
            send(1, "echo 'Data venv missing — re-enter Docker to auto-install'", wait=0)
            send(2, "echo 'Data venv missing — re-enter Docker to auto-install'", wait=0)
        else:
            # Pane 1 — SSH into Jetson and start GStreamer (same as teleop).
            print("[launch] Starting GStreamer on Jetson via SSH …")
            dev = args.jetson_camera_device
            host = PC_STATIC_IP
            port = args.camera_udp_port
            _tail  = (
                f"x264enc tune=zerolatency ! h264parse config-interval=-1 "
                f"! mpegtsmux ! udpsink host={host} port={port}"
            )
            _raw   = (
                f"v4l2src device={dev} ! videoconvert ! videoscale "
                f"! video/x-raw,width=640,height=480,format=I420 ! {_tail}"
            )
            _mjpeg = (
                f"v4l2src device={dev} ! image/jpeg ! jpegdec ! videoconvert "
                f"! videoscale ! video/x-raw,width=640,height=480,format=I420 ! {_tail}"
            )
            if args.jetson_gst_format == "mjpeg":
                jetson_gst = f"gst-launch-1.0 {_mjpeg}"
            elif args.jetson_gst_format == "raw":
                jetson_gst = f"gst-launch-1.0 {_raw}"
            else:  # auto: raw first, mjpeg fallback
                jetson_gst = (
                    f"(gst-launch-1.0 {_raw}) || "
                    f"(echo '[camera] raw failed, trying mjpeg...' "
                    f"&& gst-launch-1.0 {_mjpeg})"
                )
            usb_reset = (
                "sudo modprobe -r uvcvideo 2>/dev/null; sleep 1; "
                "sudo modprobe uvcvideo; sleep 1; echo '[camera] uvcvideo reloaded'"
            )
            ssh_remote_cmd = (
                f'bash --norc --noprofile -c "{usb_reset}; {jetson_gst}"'
            )

            if subprocess.run(["which", "sshpass"], capture_output=True).returncode != 0:
                print("[launch] sshpass not found — installing now ...")
                subprocess.run(
                    ["apt-get", "install", "-y", "-qq", "sshpass"],
                    capture_output=True,
                )
            if subprocess.run(["which", "sshpass"], capture_output=True).returncode == 0:
                ssh_cmd = (
                    f"sshpass -p '{JETSON_PASS}' ssh "
                    f"-F /dev/null -o StrictHostKeyChecking=no "
                    f"{JETSON_USER}@{args.jetson_ip} '{ssh_remote_cmd}'"
                )
            else:
                print("[launch] sshpass unavailable — SSH will prompt for password (enter: 123)")
                ssh_cmd = (
                    f"ssh -F /dev/null -o StrictHostKeyChecking=no "
                    f"{JETSON_USER}@{args.jetson_ip} '{ssh_remote_cmd}'"
                )
            send(1, ssh_cmd, wait=2)

            # Pane 2 — camera ZMQ server (GStreamer UDP → ZMQ).  Same
            # composed-camera invocation as teleoperation_launch.py.
            print("[launch] Starting camera ZMQ server …")
            cam_cmd = (
                f"source {DATA_VENV}/bin/activate; cd {PROJECT}; "
                f"{DATA_PYTHON} -m gear_sonic.camera.composed_camera "
                f"--ego-view-camera gstreamer "
                f"--ego-view-device-id {args.camera_udp_port} "
                f"--port {args.camera_zmq_port}"
            )
            send(2, f"{hook}; {cam_cmd}", wait=3)

    # ── Attach ───────────────────────────────────────────────────────────────
    print(f"\n[launch] Session '{SESSION}' ready.")
    print("  Shift+drag     — copy text (bypasses tmux selection)")
    print("  Ctrl+b arrows  — switch panes")
    print("  Ctrl+b d       — detach AND kill all processes")
    print("  Ctrl+\\        — kills all panes and exits\n")
    subprocess.run(["tmux", "attach", "-t", SESSION])


if __name__ == "__main__":
    main()
