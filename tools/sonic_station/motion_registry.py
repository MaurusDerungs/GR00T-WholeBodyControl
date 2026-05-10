#!/usr/bin/env python3
"""Discover SONIC reference motions for the local operator station."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_ROOT = REPO_ROOT / "gear_sonic_deploy" / "reference"


@dataclass(frozen=True)
class MotionInfo:
    id: str
    name: str
    source: str
    path: str
    timesteps: int | None
    duration_sec: float | None
    fps: float
    generated: bool
    valid: bool
    missing_files: list[str]


REQUIRED_FILES = (
    "joint_pos.csv",
    "joint_vel.csv",
    "body_pos.csv",
    "body_quat.csv",
    "metadata.txt",
)


def _count_csv_rows(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open("r", newline="") as handle:
        reader = csv.reader(handle)
        try:
            next(reader)
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def _iter_motion_dirs(base: Path) -> Iterable[Path]:
    if not base.exists():
        return []
    return sorted(path for path in base.iterdir() if path.is_dir())


def _motion_info(source: str, motion_dir: Path) -> MotionInfo:
    resolved = motion_dir.resolve()
    missing = [name for name in REQUIRED_FILES if not (resolved / name).exists()]
    timesteps = _count_csv_rows(resolved / "joint_pos.csv")
    fps = 50.0
    duration = None if timesteps is None else timesteps / fps
    relative_path = resolved.relative_to(REPO_ROOT) if resolved.is_relative_to(REPO_ROOT) else resolved
    motion_id = f"{source}:{motion_dir.name}"
    return MotionInfo(
        id=motion_id,
        name=motion_dir.name,
        source=source,
        path=str(relative_path),
        timesteps=timesteps,
        duration_sec=duration,
        fps=fps,
        generated=source == "generated",
        valid=not missing and timesteps is not None and timesteps > 0,
        missing_files=missing,
    )


def discover_motions() -> list[MotionInfo]:
    groups = (
        ("generated", REFERENCE_ROOT / "kimodo"),
        ("predefined", REFERENCE_ROOT / "example"),
    )
    motions: list[MotionInfo] = []
    for source, base in groups:
        motions.extend(_motion_info(source, path) for path in _iter_motion_dirs(base))
    return motions


def motions_payload() -> dict:
    motions = discover_motions()
    return {
        "count": len(motions),
        "motions": [asdict(motion) for motion in motions],
    }


def main() -> None:
    print(json.dumps(motions_payload(), indent=2))


if __name__ == "__main__":
    main()
