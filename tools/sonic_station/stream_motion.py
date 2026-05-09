#!/usr/bin/env python3
"""Stream a SONIC reference motion to the deploy runtime over ZMQ."""

from __future__ import annotations

import argparse
import csv
import json
import struct
import time
from pathlib import Path

import numpy as np
import zmq


REPO_ROOT = Path(__file__).resolve().parents[2]
HEADER_SIZE = 1280


def _build_header(fields: list[dict], *, version: int = 1, count: int = 1) -> bytes:
    header = {
        "v": version,
        "endian": "le",
        "count": count,
        "fields": fields,
    }
    header_json = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(header_json) > HEADER_SIZE:
        raise ValueError(f"ZMQ header too large: {len(header_json)} > {HEADER_SIZE}")
    return header_json.ljust(HEADER_SIZE, b"\x00")


def build_command_message(
    *,
    start: bool,
    stop: bool,
    planner: bool,
    idle_reset: bool = False,
    motion_restart: bool = False,
) -> bytes:
    fields = [
        {"name": "start", "dtype": "u8", "shape": [1]},
        {"name": "stop", "dtype": "u8", "shape": [1]},
        {"name": "planner", "dtype": "u8", "shape": [1]},
        {"name": "idle_reset", "dtype": "u8", "shape": [1]},
        {"name": "motion_restart", "dtype": "u8", "shape": [1]},
    ]
    payload = b"".join(
        (
            struct.pack("B", 1 if start else 0),
            struct.pack("B", 1 if stop else 0),
            struct.pack("B", 1 if planner else 0),
            struct.pack("B", 1 if idle_reset else 0),
            struct.pack("B", 1 if motion_restart else 0),
        )
    )
    return b"command" + _build_header(fields) + payload


def build_pose_message(
    joint_pos: np.ndarray,
    joint_vel: np.ndarray,
    body_quat: np.ndarray,
    frame_indices: np.ndarray,
    *,
    catch_up: bool,
) -> bytes:
    frame_count, joint_count = joint_pos.shape
    fields = [
        {"name": "joint_pos", "dtype": "f32", "shape": [frame_count, joint_count]},
        {"name": "joint_vel", "dtype": "f32", "shape": [frame_count, joint_count]},
        {"name": "body_quat_w", "dtype": "f32", "shape": [frame_count, 4]},
        {"name": "frame_index", "dtype": "i64", "shape": [frame_count]},
        {"name": "catch_up", "dtype": "u8", "shape": [1]},
    ]
    payload = b"".join(
        (
            np.ascontiguousarray(joint_pos, dtype=np.float32).tobytes(),
            np.ascontiguousarray(joint_vel, dtype=np.float32).tobytes(),
            np.ascontiguousarray(body_quat, dtype=np.float32).tobytes(),
            np.ascontiguousarray(frame_indices, dtype=np.int64).tobytes(),
            struct.pack("B", 1 if catch_up else 0),
        )
    )
    return b"pose" + _build_header(fields, count=frame_count) + payload


def read_float_csv(path: Path) -> np.ndarray:
    with path.open("r", newline="") as handle:
        reader = csv.reader(handle)
        next(reader)
        rows = [[float(value) for value in row] for row in reader]
    if not rows:
        raise ValueError(f"{path} has no data rows")
    return np.asarray(rows, dtype=np.float32)


def load_motion(motion_dir: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    motion_dir = motion_dir.resolve()
    joint_pos = read_float_csv(motion_dir / "joint_pos.csv")
    joint_vel = read_float_csv(motion_dir / "joint_vel.csv")
    body_quat_all = read_float_csv(motion_dir / "body_quat.csv")
    body_quat = body_quat_all[:, :4]

    frame_count = min(len(joint_pos), len(joint_vel), len(body_quat))
    if frame_count <= 0:
        raise ValueError(f"{motion_dir} does not contain aligned motion frames")
    return joint_pos[:frame_count], joint_vel[:frame_count], body_quat[:frame_count]


def resolve_motion(value: str) -> Path:
    path = Path(value)
    if path.exists():
        return path
    reference_root = REPO_ROOT / "gear_sonic_deploy" / "reference"
    candidates = [
        reference_root / "station_idle" / value,
        reference_root / "kimodo" / value,
        reference_root / "dance_loop" / value,
        reference_root / "example" / value,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"motion not found: {value}")


def publish_motion(
    motion_dir: Path,
    *,
    host: str,
    port: int,
    startup_delay: float,
    repeat_command: int,
    repeat_command_interval: float,
    repeat_send: int,
    repeat_interval: float,
    catch_up: bool,
    planner: bool,
    command_only: bool,
) -> None:
    joint_pos, joint_vel, body_quat = load_motion(motion_dir)
    frame_indices = np.arange(joint_pos.shape[0], dtype=np.int64)

    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    endpoint = f"tcp://{host}:{port}"
    socket.bind(endpoint)
    try:
        print(f"Streaming motion: {motion_dir}")
        print(f"  endpoint: {endpoint}")
        print(f"  frames:   {joint_pos.shape[0]}")
        print(f"  joints:   {joint_pos.shape[1]}")
        time.sleep(startup_delay)

        command = build_command_message(start=True, stop=False, planner=planner)
        for _ in range(repeat_command):
            socket.send(command)
            time.sleep(repeat_command_interval)

        if command_only:
            print("Command streamed.")
            return

        pose_message = build_pose_message(joint_pos, joint_vel, body_quat, frame_indices, catch_up=catch_up)
        for send_index in range(repeat_send):
            socket.send(pose_message)
            if send_index < repeat_send - 1:
                time.sleep(repeat_interval)
        print("Motion streamed.")
    finally:
        socket.close(linger=0)
        context.term()


def publish_control_command(
    *,
    action: str,
    host: str,
    port: int,
    startup_delay: float,
    repeat_command: int,
    repeat_command_interval: float,
) -> None:
    actions = {
        "emergency_stop": dict(start=False, stop=True, planner=True),
        "idle_reset": dict(start=False, stop=False, planner=True, idle_reset=True),
        "motion_restart": dict(start=False, stop=False, planner=False, motion_restart=True),
    }
    if action not in actions:
        raise ValueError(f"unknown control action: {action}")

    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    endpoint = f"tcp://{host}:{port}"
    socket.bind(endpoint)
    try:
        print(f"Streaming control action: {action}")
        print(f"  endpoint: {endpoint}")
        time.sleep(startup_delay)
        command = build_command_message(**actions[action])
        for _ in range(repeat_command):
            socket.send(command)
            time.sleep(repeat_command_interval)
        print("Control action streamed.")
    finally:
        socket.close(linger=0)
        context.term()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("motion", nargs="?", help="Motion folder path or motion name")
    parser.add_argument("--host", default="*")
    parser.add_argument("--port", type=int, default=5556)
    parser.add_argument("--startup-delay", type=float, default=1.0)
    parser.add_argument("--repeat-command", type=int, default=5)
    parser.add_argument("--repeat-command-interval", type=float, default=0.05)
    parser.add_argument("--repeat-send", type=int, default=1)
    parser.add_argument("--repeat-interval", type=float, default=1.0)
    parser.add_argument("--planner", action="store_true", help="Send the command in planner mode instead of streamed-motion mode")
    parser.add_argument("--command-only", action="store_true", help="Only send the start command; do not send pose frames")
    parser.add_argument(
        "--control-action",
        choices=("emergency_stop", "idle_reset", "motion_restart"),
        help="Send a control action without streaming motion frames",
    )
    parser.add_argument("--no-catch-up", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.control_action:
        publish_control_command(
            action=args.control_action,
            host=args.host,
            port=args.port,
            startup_delay=args.startup_delay,
            repeat_command=args.repeat_command,
            repeat_command_interval=args.repeat_command_interval,
        )
        return
    if not args.motion:
        raise SystemExit("motion is required unless --control-action is used")
    publish_motion(
        resolve_motion(args.motion),
        host=args.host,
        port=args.port,
        startup_delay=args.startup_delay,
        repeat_command=args.repeat_command,
        repeat_command_interval=args.repeat_command_interval,
        repeat_send=args.repeat_send,
        repeat_interval=args.repeat_interval,
        catch_up=not args.no_catch_up,
        planner=args.planner,
        command_only=args.command_only,
    )


if __name__ == "__main__":
    main()
