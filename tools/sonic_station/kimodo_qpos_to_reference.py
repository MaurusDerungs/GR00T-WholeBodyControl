#!/usr/bin/env python3
"""Convert Kimodo G1 MuJoCo qpos CSV into a SONIC reference-motion folder.

Kimodo-G1 exports one row per frame with 36 values:
  root xyz, root quaternion wxyz, then 29 MuJoCo-order joint values.

The deployment stack expects joint CSVs in IsaacLab order, plus root body
position/orientation files. This script produces the minimal reference format
accepted by MotionDataReader.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import shutil
from pathlib import Path

import numpy as np

try:
    from scipy.spatial.transform import Rotation, Slerp
except Exception:  # pragma: no cover - only used when scipy is absent.
    Rotation = None
    Slerp = None


MUJOCO_TO_ISAACLAB = np.array(
    [
        0,
        6,
        12,
        1,
        7,
        13,
        2,
        8,
        14,
        3,
        9,
        15,
        22,
        4,
        10,
        16,
        23,
        5,
        11,
        17,
        24,
        18,
        25,
        19,
        26,
        20,
        27,
        21,
        28,
    ],
    dtype=np.int64,
)


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("._-")
    return value or "kimodo_motion"


def read_qpos_csv(path: Path) -> np.ndarray:
    rows: list[list[float]] = []
    with path.open("r", newline="") as handle:
        for row in csv.reader(handle):
            if not row or all(not cell.strip() for cell in row):
                continue
            try:
                values = [float(cell) for cell in row]
            except ValueError:
                if rows:
                    raise ValueError(f"Non-numeric row found after data started in {path}")
                continue
            rows.append(values)

    if not rows:
        raise ValueError(f"No numeric qpos rows found in {path}")

    qpos = np.asarray(rows, dtype=np.float64)
    if qpos.ndim != 2 or qpos.shape[1] != 36:
        raise ValueError(f"Expected qpos shape [T, 36], got {qpos.shape}")
    if not np.isfinite(qpos).all():
        raise ValueError("qpos CSV contains NaN or Inf values")
    return qpos


def normalize_quaternions_wxyz(quat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(quat, axis=1, keepdims=True)
    norms[norms < 1e-8] = 1.0
    return quat / norms


def resample_linear(values: np.ndarray, src_fps: float, dst_fps: float) -> np.ndarray:
    if values.shape[0] <= 1 or math.isclose(src_fps, dst_fps):
        return values.copy()

    duration = (values.shape[0] - 1) / src_fps
    dst_frames = int(math.floor(duration * dst_fps)) + 1
    src_t = np.arange(values.shape[0], dtype=np.float64) / src_fps
    dst_t = np.arange(dst_frames, dtype=np.float64) / dst_fps
    dst_t = np.clip(dst_t, src_t[0], src_t[-1])

    out = np.empty((dst_frames, values.shape[1]), dtype=np.float64)
    for col in range(values.shape[1]):
        out[:, col] = np.interp(dst_t, src_t, values[:, col])
    return out


def resample_quat_wxyz(quat: np.ndarray, src_fps: float, dst_fps: float) -> np.ndarray:
    quat = normalize_quaternions_wxyz(quat)
    if quat.shape[0] <= 1 or math.isclose(src_fps, dst_fps):
        return quat.copy()

    duration = (quat.shape[0] - 1) / src_fps
    dst_frames = int(math.floor(duration * dst_fps)) + 1
    src_t = np.arange(quat.shape[0], dtype=np.float64) / src_fps
    dst_t = np.arange(dst_frames, dtype=np.float64) / dst_fps
    dst_t = np.clip(dst_t, src_t[0], src_t[-1])

    if Rotation is None or Slerp is None:
        resampled = resample_linear(quat, src_fps, dst_fps)
        return normalize_quaternions_wxyz(resampled)

    rotations_xyzw = Rotation.from_quat(quat[:, [1, 2, 3, 0]])
    resampled_xyzw = Slerp(src_t, rotations_xyzw)(dst_t).as_quat()
    return resampled_xyzw[:, [3, 0, 1, 2]]


def finite_difference(values: np.ndarray, fps: float) -> np.ndarray:
    if values.shape[0] <= 1:
        return np.zeros_like(values)
    return np.gradient(values, 1.0 / fps, axis=0, edge_order=1)


def write_csv(path: Path, header: list[str], values: np.ndarray) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(([f"{item:.6f}" for item in row] for row in values))


def convert_qpos_to_reference(
    qpos_csv: Path,
    output_root: Path,
    name: str | None,
    input_fps: float,
    output_fps: float,
    force: bool,
) -> Path:
    qpos = read_qpos_csv(qpos_csv)
    motion_name = slugify(name or qpos_csv.stem)
    motion_dir = output_root / motion_name

    if motion_dir.exists():
        if not force:
            raise FileExistsError(f"{motion_dir} already exists; pass --force to overwrite")
        shutil.rmtree(motion_dir)
    motion_dir.mkdir(parents=True, exist_ok=True)

    root_pos = resample_linear(qpos[:, :3], input_fps, output_fps)
    root_quat = resample_quat_wxyz(qpos[:, 3:7], input_fps, output_fps)
    joints_mujoco = resample_linear(qpos[:, 7:], input_fps, output_fps)
    joints_isaaclab = joints_mujoco[:, MUJOCO_TO_ISAACLAB]
    joint_vel = finite_difference(joints_isaaclab, output_fps)

    timesteps = joints_isaaclab.shape[0]
    body_lin_vel = finite_difference(root_pos, output_fps)
    body_ang_vel = np.zeros_like(root_pos)

    write_csv(motion_dir / "joint_pos.csv", [f"joint_{i}" for i in range(29)], joints_isaaclab)
    write_csv(motion_dir / "joint_vel.csv", [f"joint_vel_{i}" for i in range(29)], joint_vel)
    write_csv(motion_dir / "body_pos.csv", ["body_0_x", "body_0_y", "body_0_z"], root_pos)
    write_csv(motion_dir / "body_quat.csv", ["body_0_w", "body_0_x", "body_0_y", "body_0_z"], root_quat)
    write_csv(
        motion_dir / "body_lin_vel.csv",
        ["body_0_vel_x", "body_0_vel_y", "body_0_vel_z"],
        body_lin_vel,
    )
    write_csv(
        motion_dir / "body_ang_vel.csv",
        ["body_0_angvel_x", "body_0_angvel_y", "body_0_angvel_z"],
        body_ang_vel,
    )

    (motion_dir / "metadata.txt").write_text(
        f"""Metadata for: {motion_name}
==============================

Body part indexes:
[0]

Total timesteps: {timesteps}
""",
        encoding="utf-8",
    )
    (motion_dir / "info.txt").write_text(
        f"""Generated from Kimodo G1 qpos CSV
Source: {qpos_csv}
Source shape: {qpos.shape[0]} x {qpos.shape[1]}
Input FPS: {input_fps}
Output FPS: {output_fps}
Output timesteps: {timesteps}
Joint order: IsaacLab
Body data: root-only
""",
        encoding="utf-8",
    )

    return motion_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qpos_csv", type=Path, help="Kimodo-G1 MuJoCo qpos CSV, shape [T, 36]")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("gear_sonic_deploy/reference/kimodo"),
        help="Reference dataset directory to write into",
    )
    parser.add_argument("--name", help="Motion folder name. Defaults to qpos CSV stem.")
    parser.add_argument("--input-fps", type=float, default=30.0, help="Kimodo CSV frame rate")
    parser.add_argument("--output-fps", type=float, default=50.0, help="SONIC reference frame rate")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing motion folder")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    motion_dir = convert_qpos_to_reference(
        args.qpos_csv,
        args.output_root,
        args.name,
        args.input_fps,
        args.output_fps,
        args.force,
    )
    print(motion_dir)


if __name__ == "__main__":
    main()
