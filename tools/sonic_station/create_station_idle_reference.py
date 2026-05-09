#!/usr/bin/env python3
"""Create a local static idle SONIC reference from one stable reference frame."""

from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO_ROOT / "gear_sonic_deploy" / "reference" / "example" / "neutral_kick_R_001__A543"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "gear_sonic_deploy" / "reference" / "station_idle"

POSITION_FILES = ("joint_pos.csv", "body_pos.csv", "body_quat.csv")
VELOCITY_FILES = ("joint_vel.csv", "body_lin_vel.csv", "body_ang_vel.csv")


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise ValueError(f"{path} is empty")
    return rows[0], rows[1:]


def write_repeated_row(path: Path, header: list[str], row: list[str], frames: int) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for _ in range(frames):
            writer.writerow(row)


def write_zero_rows(path: Path, header: list[str], frames: int) -> None:
    zero_row = ["0.000000"] * len(header)
    write_repeated_row(path, header, zero_row, frames)


def create_idle_reference(
    source: Path,
    output_root: Path,
    name: str,
    frame: int,
    frames: int,
    force: bool,
) -> Path:
    source = source.resolve()
    output_dir = (output_root / name).resolve()
    if output_dir.exists():
        if not force:
            return output_dir
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    selected_index = frame
    for filename in POSITION_FILES:
        header, rows = read_csv(source / filename)
        if not rows:
            raise ValueError(f"{source / filename} has no data rows")
        row = rows[selected_index]
        write_repeated_row(output_dir / filename, header, row, frames)

    for filename in VELOCITY_FILES:
        header, _ = read_csv(source / filename)
        write_zero_rows(output_dir / filename, header, frames)

    (output_dir / "metadata.txt").write_text(
        "\n".join(
            [
                "source=sonic_station_static_idle",
                f"source_motion={source}",
                f"source_frame={frame}",
                f"frames={frames}",
                "fps=50",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (output_dir / "info.txt").write_text(
        "\n".join(
            [
                "Static idle reference for Sonic Station.",
                "Generated locally from a stable SONIC reference frame.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return output_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--name", default="00_station_idle")
    parser.add_argument("--frame", type=int, default=0)
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = create_idle_reference(
        source=args.source,
        output_root=args.output_root,
        name=args.name,
        frame=args.frame,
        frames=args.frames,
        force=args.force,
    )
    try:
        display_path = output_dir.relative_to(REPO_ROOT)
    except ValueError:
        display_path = output_dir
    print(display_path)


if __name__ == "__main__":
    main()
