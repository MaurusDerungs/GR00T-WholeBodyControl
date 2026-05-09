#!/usr/bin/env python3
"""
Unified G1 teleoperation launcher (tmux-based).

Usage (from repo root, inside Docker):
  python teleoperation_launch.py real
  python teleoperation_launch.py real --record
  python teleoperation_launch.py real --record --task "pick up the cup"
  python teleoperation_launch.py sim
  python teleoperation_launch.py sim --record

Layout (3 panes, one tmux window):
  ┌─────────────────────┬──────────────────────┐
  │  ctrl (WBC loop)    │  teleop (Quest3S)    │
  │                     ├──────────────────────┤
  │                     │  data exporter       │
  └─────────────────────┴──────────────────────┘

Copy-paste tip: hold Shift and drag to select text (bypasses tmux),
or use mouse mode if `set -g mouse on` is in ~/.tmux.conf.

Ctrl+\\ in any pane kills the whole session.
"""

import argparse
import os
import re
import subprocess
import sys
import time

# ── Constants ──────────────────────────────────────────────────────────────────
ROBOT_IP     = "192.168.123.164"
ROBOT_SUBNET = "192.168.123"
PC_STATIC_IP = "192.168.123.99"
JETSON_IP    = "192.168.123.164"  # Jetson is on the same robot network
JETSON_USER  = "unitree"
JETSON_PASS  = "123"
SESSION      = "g1"

PROJECT    = os.path.dirname(os.path.abspath(__file__))
VENV       = os.path.join(PROJECT, ".venv_quest_wbc")
PYTHON     = os.path.join(VENV, "bin", "python")
DATA_VENV  = os.path.join(PROJECT, ".venv_data_collection")
DATA_PYTHON = os.path.join(DATA_VENV, "bin", "python")


# ── Network helpers ────────────────────────────────────────────────────────────

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
        capture_output=True, text=True
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


# ── tmux helpers ───────────────────────────────────────────────────────────────

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


def create_session(with_camera: bool = False):
    """
    Without camera (3 panes):
      pane 0 (left)         — control loop
      pane 1 (right-top)    — teleop policy
      pane 2 (right-bottom) — data exporter / idle

    With camera (6 panes):
      pane 0 (left)           — control loop
      pane 1 (right-top)      — teleop policy
      pane 2 (right-mid-top)  — Jetson GStreamer (SSH)
      pane 3 (right-mid-bot)  — camera ZMQ server
      pane 4 (right-mid-bot2) — data exporter
      pane 5 (right-bottom)   — recording controller (press Enter to toggle)
    """
    kill_session()
    tmux("new-session", "-d", "-s", SESSION, "-x", "220", "-y", "60")
    tmux("rename-window", "-t", f"{SESSION}:0", "g1")
    # Kill the session (and all processes) when the last client detaches (Ctrl+b d)
    tmux("set-hook", "-t", SESSION, "client-detached",
         f"run-shell 'tmux kill-session -t {SESSION} 2>/dev/null'")
    tmux("split-window", "-t", f"{SESSION}:0.0", "-h")   # left | right
    tmux("split-window", "-t", f"{SESSION}:0.1", "-v")   # right top | right bottom
    if with_camera:
        tmux("split-window", "-t", f"{SESSION}:0.2", "-v")  # 3 right panes
        tmux("split-window", "-t", f"{SESSION}:0.3", "-v")  # 4 right panes
        tmux("split-window", "-t", f"{SESSION}:0.4", "-v")  # 5 right panes (recording ctrl)
    tmux("select-pane", "-t", f"{SESSION}:0.0")


def send(pane: int, cmd: str, wait: float = 0.5):
    tmux("send-keys", "-t", f"{SESSION}:0.{pane}", cmd, "Enter")
    if wait:
        time.sleep(wait)


def activate():
    """Shell snippet: source ROS + quest_wbc venv."""
    return (
        "set +u; source /opt/ros/humble/setup.bash 2>/dev/null; set -u; "
        f"source {VENV}/bin/activate"
    )


def kill_hook():
    """Ctrl+\\ kills the whole tmux session from any pane."""
    return f"trap 'tmux kill-session -t {SESSION}' QUIT"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Launch G1 teleoperation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("mode", choices=["real", "sim"],
                        help="'real' for physical robot, 'sim' for MuJoCo simulation")
    parser.add_argument("--record", action="store_true", default=False,
                        help="Enable ZMQ data recording for GR00T VLA training")
    parser.add_argument("--task", default="demo", metavar="TEXT",
                        help="Task language prompt for the dataset (default: 'demo')")
    parser.add_argument("--camera-udp-port", type=int, default=5000, metavar="PORT",
                        help="UDP port the G1 Jetson streams H264 video to (default: 5000)")
    parser.add_argument("--camera-zmq-port", type=int, default=5555, metavar="PORT",
                        help="ZMQ port the camera server publishes on (default: 5555)")
    parser.add_argument("--jetson-ip", default=JETSON_IP, metavar="IP",
                        help=f"IP of the G1 Jetson (default: {JETSON_IP})")
    parser.add_argument("--jetson-camera-device", default="/dev/video5", metavar="DEV",
                        help="V4L2 device on the Jetson (default: /dev/video5 = RealSense color high-res). "
                             "Run 'ls /dev/video*' on Jetson to find the right one.")
    parser.add_argument("--jetson-gst-format", default="auto",
                        choices=["auto", "mjpeg", "raw"],
                        help="Camera pixel format on the Jetson: "
                             "mjpeg=USB cameras outputting JPEG, raw=YUY2/BGR, "
                             "auto=try mjpeg then raw (default: auto)")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  G1 Launcher  |  mode={args.mode}  |  record={args.record}")
    print(f"{'='*60}\n")

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
    else:
        interface = "sim"

    # ── Build commands ───────────────────────────────────────────────────────
    ctrl_args = [
        PYTHON,
        "decoupled_wbc/control/main/teleop/run_g1_control_loop.py",
        "--interface", interface,
        "--no-with_hands",
        "--enable_onscreen" if args.mode == "sim" else "--no-enable_onscreen",
    ]
    if args.record:
        ctrl_args.append("--enable_sonic_data_collection")

    teleop_args = [
        PYTHON,
        "decoupled_wbc/control/main/teleop/run_teleop_policy_loop.py",
        "--body_control_device", "oculus",
        "--hand_control_device", "oculus",
        "--no-enable_visualization",
        "--enable_real_device" if args.mode == "real" else "--no-enable_real_device",
    ]

    ctrl_cmd   = " ".join(ctrl_args)
    teleop_cmd = " ".join(teleop_args)

    # ── Create tmux session ──────────────────────────────────────────────────
    create_session(with_camera=args.record)
    hook = kill_hook()
    act  = activate()

    # Pane 0 — control loop
    print("[launch] Starting control loop …")
    send(0, f"{hook}; {act}; cd {PROJECT}; {ctrl_cmd}",
         wait=3 if args.mode == "real" else 1)

    # Pane 1 — teleop policy
    print("[launch] Starting teleop policy …")
    send(1, f"{hook}; {act}; cd {PROJECT}; {teleop_cmd}", wait=1)

    # Panes 2/3/4 — Jetson camera stream + camera ZMQ server + data exporter
    if args.record:
        if not os.path.isfile(DATA_PYTHON):
            print(f"[launch] ERROR: data venv not found at {DATA_VENV}.")
            print("[launch] It should be auto-installed on Docker entry. Re-enter Docker to trigger setup.")
            send(2, "echo 'Data venv missing — re-enter Docker to auto-install'", wait=0)
            args.record = False
        else:
            # Pane 2 — SSH into Jetson and start GStreamer
            print("[launch] Starting GStreamer on Jetson via SSH …")
            # Build GStreamer pipeline based on camera format.
            dev = args.jetson_camera_device
            host = PC_STATIC_IP
            port = args.camera_udp_port
            # config-interval=-1: resend SPS/PPS before every keyframe so receivers
            # joining mid-stream can decode immediately without waiting for stream start.
            _tail = f"x264enc tune=zerolatency ! h264parse config-interval=-1 ! mpegtsmux ! udpsink host={host} port={port}"
            # raw path works for RealSense color (/dev/video4) and most USB cameras
            _raw   = f"v4l2src device={dev} ! videoconvert ! videoscale ! video/x-raw,width=640,height=480,format=I420 ! {_tail}"
            _mjpeg = f"v4l2src device={dev} ! image/jpeg ! jpegdec ! videoconvert ! videoscale ! video/x-raw,width=640,height=480,format=I420 ! {_tail}"

            if args.jetson_gst_format == "mjpeg":
                jetson_gst = f"gst-launch-1.0 {_mjpeg}"
            elif args.jetson_gst_format == "raw":
                jetson_gst = f"gst-launch-1.0 {_raw}"
            else:  # auto: try raw first (works for RealSense), fall back to mjpeg
                jetson_gst = (
                    f"(gst-launch-1.0 {_raw}) || "
                    f"(echo '[camera] raw failed, trying mjpeg...' && gst-launch-1.0 {_mjpeg})"
                )

            # Reload uvcvideo kernel module to recover from stuck RealSense driver state.
            # Safer than USB authorized sysfs toggle which can brick the device path.
            usb_reset = (
                "sudo modprobe -r uvcvideo 2>/dev/null; sleep 1; sudo modprobe uvcvideo; sleep 1; "
                "echo '[camera] uvcvideo reloaded'"
            )

            # inner: double-quoted so it nests safely inside the outer single-quoted ssh arg
            ssh_remote_cmd = f'bash --norc --noprofile -c "{usb_reset}; {jetson_gst}"'
            # -F /dev/null: skip /root/.ssh/config (bad permissions in Docker)
            # Install sshpass inline if missing, then use it
            if subprocess.run(["which", "sshpass"], capture_output=True).returncode != 0:
                print("[launch] sshpass not found — installing now ...")
                subprocess.run(["apt-get", "install", "-y", "-qq", "sshpass"],
                               capture_output=True)
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
            send(2, ssh_cmd, wait=2)

            # Pane 3 — camera ZMQ server (GStreamer UDP → ZMQ)
            print("[launch] Starting camera ZMQ server …")
            cam_cmd = (
                f"source {DATA_VENV}/bin/activate; cd {PROJECT}; "
                f"{DATA_PYTHON} -m gear_sonic.camera.composed_camera "
                f"--ego-view-camera gstreamer "
                f"--ego-view-device-id {args.camera_udp_port} "
                f"--port {args.camera_zmq_port}"
            )
            send(3, f"{hook}; {cam_cmd}", wait=3)

            # Pane 4 — data exporter
            print("[launch] Starting data exporter …")
            data_cmd = (
                f"source {DATA_VENV}/bin/activate; cd {PROJECT}; "
                f"{DATA_PYTHON} gear_sonic/scripts/run_data_exporter.py "
                f"--task-prompt \"{args.task}\" "
                f"--camera-port {args.camera_zmq_port}"
            )
            send(4, f"{hook}; {data_cmd}", wait=1)

            # Pane 5 — recording controller
            print("[launch] Starting recording controller …")
            rec_ctrl_cmd = (
                f"source {DATA_VENV}/bin/activate; cd {PROJECT}; "
                f"{DATA_PYTHON} -c \""
                f"import zmq, time; "
                f"ctx = zmq.Context(); "
                f"s = ctx.socket(zmq.PUB); "
                f"s.bind('tcp://*:5580'); "
                f"time.sleep(1.0); "
                f"print('\\n  === RECORDING CONTROLLER ==='); "
                f"print('  Press Enter → start/stop episode'); "
                f"print('  Type x + Enter → discard episode'); "
                f"print('  Type q + Enter → quit\\n'); "
                f"[(__import__('time').sleep(0.1), s.send_string('x' if (k:=input('rec> ')) == 'x' else 'q' if k == 'q' else 'c') or (k == 'q' and __import__('sys').exit(0))) for _ in iter(int, 1)]"
                f"\""
            )
            send(5, rec_ctrl_cmd, wait=1)
    else:
        send(2, "echo 'Recording disabled — pass --record to enable'", wait=0)

    # ── Attach ───────────────────────────────────────────────────────────────
    print(f"\n[launch] Session '{SESSION}' ready.")
    print("  Shift+drag     — copy text (bypasses tmux selection)")
    print("  Ctrl+b arrows  — switch panes")
    print("  Ctrl+b d       — detach AND kill all processes")
    print("  Ctrl+\\        — also kills all panes and exits\n")
    subprocess.run(["tmux", "attach", "-t", SESSION])


if __name__ == "__main__":
    main()
