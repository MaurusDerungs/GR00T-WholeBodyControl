"""
Convert a GR00T-WholeBodyControl LeRobot v2.1 dataset (stream_mode=6) to a
format that is fully compatible with LeRobot's ACT training pipeline.

What this script does
---------------------
1.  Renames  action.wbc  →  action  (ACT requires the key "action").
2.  Drops every column that ACT does not consume so the dataset stays minimal:
        observation.images.ego_view   (image, from existing video files)
        observation.state             (43-D joint positions)
        action                        (43-D targets, renamed from action.wbc)
        timestamp, frame_index, episode_index, index, task_index
    Everything starting with "teleop." or other "observation.*" columns is
    removed from both the parquets and info.json/features so lerobot does not
    try to normalize it.
3.  Computes per-episode stats (mean/std/min/max/count) for every numeric
    feature and for the image, then writes:
        meta/episodes_stats.jsonl   (one object per episode — primary file)
        meta/stats.json             (dataset-wide, count-weighted aggregate)
4.  Image stats are written in the [0, 1] range with shape (3, 1, 1), matching
    lerobot's own compute_stats output (uses ImageNet defaults — the actual
    values are overridden at training time by `use_imagenet_stats=True`).
5.  Validates the output against lerobot's `_assert_type_and_shape` rules
    before exiting.
6.  Copies videos unchanged (paths stay valid).

Usage (from repo root, with the data-collection venv active)::

    python gear_sonic/scripts/convert_to_lerobot_act.py \
        --input  outputs/training_data_clean \
        --output outputs/act_dataset

Then train with::

    python -m lerobot.scripts.train \
        --policy.type act \
        --dataset.repo_id g1_wbc \
        --dataset.root outputs/act_dataset \
        --policy.chunk_size 50 \
        --policy.n_action_steps 50 \
        --batch_size 8 \
        --steps 50000 \
        --output_dir outputs/act_checkpoints
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Configuration — which features we keep in the output dataset.
# Anything NOT in this set is removed from the parquets and from info.json.
# ---------------------------------------------------------------------------
ACTION_SRC_COL = "action.wbc"
ACTION_DST_COL = "action"

# Numeric features for which stats must be computed (shape: (D,)).
NUMERIC_FEATURES = ["observation.state", ACTION_DST_COL]

# Image features — stats are synthesized (ImageNet defaults in [0, 1], shape (3,1,1)).
IMAGE_FEATURES = ["observation.images.ego_view"]

# LeRobot meta columns that must be present in the parquet (no stats).
META_COLUMNS = {"timestamp", "frame_index", "episode_index", "index", "task_index"}

# Everything we keep — used to filter both parquet columns and info.json features.
# Deliberately excludes 'observation.eef_state' — ACT does not use it and lerobot
# would fail when trying to normalize it (missing from stats).
KEEP_FEATURE_KEYS = set(NUMERIC_FEATURES) | set(IMAGE_FEATURES) | META_COLUMNS

# ImageNet defaults, already in [0, 1] range, lerobot's expected shape for images is (3, 1, 1).
IMAGENET_MEAN = [[[0.485]], [[0.456]], [[0.406]]]
IMAGENET_STD = [[[0.229]], [[0.224]], [[0.225]]]
IMAGE_MIN = [[[0.0]], [[0.0]], [[0.0]]]
IMAGE_MAX = [[[1.0]], [[1.0]], [[1.0]]]


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _stack_vectors(series: pd.Series) -> np.ndarray:
    """Stack a pandas Series of 1-D arrays into a (N, D) matrix."""
    return np.vstack([np.asarray(x, dtype=np.float64) for x in series])


def compute_numeric_stats(arr: np.ndarray) -> dict:
    """Per-episode stats for a numeric (N, D) feature.

    Output shapes match lerobot's expectations:
        mean/std/min/max: list of length D  (ndim == 1 after np.array(...))
        count:            list of length 1
    """
    return {
        "mean":  arr.mean(axis=0).tolist(),
        "std":   arr.std(axis=0).tolist(),
        "min":   arr.min(axis=0).tolist(),
        "max":   arr.max(axis=0).tolist(),
        "count": [int(arr.shape[0])],
    }


def image_stats_for_episode(n_frames: int) -> dict:
    """Per-episode stats for the image feature using ImageNet defaults.

    `use_imagenet_stats=True` is the ACT default so the actual values below are
    overridden at training time — but we still need valid (3,1,1) stats on disk.
    """
    return {
        "mean":  IMAGENET_MEAN,
        "std":   IMAGENET_STD,
        "min":   IMAGE_MIN,
        "max":   IMAGE_MAX,
        "count": [int(n_frames)],
    }


def aggregate_stats_count_weighted(ep_stats_list: list[dict]) -> dict:
    """Aggregate per-episode stats into one dataset-wide stat dict.

    Replicates lerobot.common.datasets.compute_stats.aggregate_feature_stats:
      - min  = element-wise min of episode mins
      - max  = element-wise max of episode maxes
      - mean = count-weighted mean of episode means
      - std  = parallel variance formula using counts
    """
    keys = {k for ep in ep_stats_list for k in ep}
    agg: dict[str, dict] = {}

    for key in keys:
        rows = [ep[key] for ep in ep_stats_list if key in ep]
        means = np.stack([np.asarray(r["mean"]) for r in rows])           # (E, ...)
        stds  = np.stack([np.asarray(r["std"])  for r in rows])
        mins  = np.stack([np.asarray(r["min"])  for r in rows])
        maxs  = np.stack([np.asarray(r["max"])  for r in rows])
        counts = np.stack([np.asarray(r["count"]) for r in rows])          # (E, 1)

        total_count = counts.sum(axis=0)

        # Broadcast counts over feature dimensions.
        c = counts.astype(np.float64)
        while c.ndim < means.ndim:
            c = np.expand_dims(c, axis=-1)

        total_mean = (means * c).sum(axis=0) / total_count
        delta = means - total_mean
        total_var = ((stds ** 2 + delta ** 2) * c).sum(axis=0) / total_count

        agg[key] = {
            "mean":  total_mean.tolist(),
            "std":   np.sqrt(total_var).tolist(),
            "min":   mins.min(axis=0).tolist(),
            "max":   maxs.max(axis=0).tolist(),
            "count": total_count.tolist(),
        }
    return agg


def validate_stats(ep_stats_list: list[dict]) -> None:
    """Replicate lerobot's `_assert_type_and_shape` so we fail fast on bad output."""
    for i, ep in enumerate(ep_stats_list):
        for fkey, stats in ep.items():
            for k, v in stats.items():
                arr = np.asarray(v)
                if arr.ndim == 0:
                    raise ValueError(
                        f"Episode {i} / feature '{fkey}' / stat '{k}': "
                        f"ndim is 0 (must be >= 1)."
                    )
                if k == "count" and arr.shape != (1,):
                    raise ValueError(
                        f"Episode {i} / feature '{fkey}' / stat 'count': "
                        f"shape must be (1,), got {arr.shape}."
                    )
                if "image" in fkey and k != "count" and arr.shape != (3, 1, 1):
                    raise ValueError(
                        f"Episode {i} / feature '{fkey}' / stat '{k}': "
                        f"shape must be (3,1,1) for image features, got {arr.shape}."
                    )


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(input_paths: list[Path], output_path: Path, overwrite: bool = False) -> None:
    """Convert one or more input datasets into a single ACT-compatible dataset.

    When multiple input paths are given they are merged: episodes are
    re-indexed continuously (0, 1, 2, …) across all sources.  The first
    input's info.json / tasks.jsonl are used as the template.
    """
    if len(input_paths) == 1:
        return _convert_single(input_paths[0], output_path, overwrite)
    # Multiple inputs: convert each to a temp dir, then merge.
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_dirs = []
        for i, inp in enumerate(input_paths):
            tmp = Path(tmpdir) / f"part_{i:03d}"
            print(f"\n── Converting input {i+1}/{len(input_paths)}: {inp} ──")
            _convert_single(inp, tmp, overwrite=True)
            tmp_dirs.append(tmp)
        print(f"\n── Merging {len(tmp_dirs)} converted datasets → {output_path} ──")
        _merge_converted(tmp_dirs, output_path, overwrite)


def _merge_converted(src_dirs: list[Path], output_path: Path, overwrite: bool) -> None:
    """Merge already-converted ACT datasets (each a valid lerobot dir) into one."""
    if output_path.exists():
        if not overwrite:
            raise FileExistsError(f"Output path already exists: {output_path}\nRe-run with --overwrite.")
        shutil.rmtree(output_path)

    output_path.mkdir(parents=True)
    (output_path / "meta").mkdir()
    (output_path / "data").mkdir()

    # Use info.json from the first source as template.
    with (src_dirs[0] / "meta" / "info.json").open() as f:
        info = json.load(f)

    chunks_size = info.get("chunks_size", 1000)
    ep_stats_list: list[dict] = []
    episodes_out: list[dict] = []
    total_frames = 0
    new_ep_idx = 0

    for src in src_dirs:
        with (src / "meta" / "episodes.jsonl").open() as f:
            eps = [json.loads(l) for l in f if l.strip()]
        ep_stats_raw = {}
        with (src / "meta" / "episodes_stats.jsonl").open() as f:
            for l in f:
                if l.strip():
                    d = json.loads(l)
                    ep_stats_raw[d["episode_index"]] = d["stats"]

        for ep in eps:
            old_idx = ep["episode_index"]
            n_frames = ep["length"]
            chunk = old_idx // chunks_size
            new_chunk = new_ep_idx // chunks_size

            # Copy parquet with updated indices.
            src_pq = src / "data" / f"chunk-{chunk:03d}" / f"episode_{old_idx:06d}.parquet"
            dst_pq_dir = output_path / "data" / f"chunk-{new_chunk:03d}"
            dst_pq_dir.mkdir(parents=True, exist_ok=True)
            dst_pq = dst_pq_dir / f"episode_{new_ep_idx:06d}.parquet"
            df = pd.read_parquet(src_pq)
            df["episode_index"] = new_ep_idx
            df["index"] = range(total_frames, total_frames + n_frames)
            df["frame_index"] = range(n_frames)
            fps = info.get("fps", 50)
            df["timestamp"] = [j / fps for j in range(n_frames)]
            df.to_parquet(dst_pq, index=False)

            # Copy video(s).
            for vid_key in [k for k, v in info["features"].items() if v.get("dtype") in ("video", "image")]:
                vid_src = src / "videos" / vid_key / f"episode_{old_idx:06d}.mp4"
                vid_dst_dir = output_path / "videos" / vid_key
                vid_dst_dir.mkdir(parents=True, exist_ok=True)
                if vid_src.exists():
                    shutil.copy2(vid_src, vid_dst_dir / f"episode_{new_ep_idx:06d}.mp4")

            ep_stats_list.append(ep_stats_raw.get(old_idx, {}))
            episodes_out.append({"episode_index": new_ep_idx, "tasks": ep["tasks"], "length": n_frames})
            total_frames += n_frames
            new_ep_idx += 1

    # Write meta files.
    info["total_episodes"] = new_ep_idx
    info["total_frames"] = total_frames
    info["total_videos"] = new_ep_idx * len(IMAGE_FEATURES)
    info["splits"] = {"train": f"0:{new_ep_idx}"}
    with (output_path / "meta" / "info.json").open("w") as f:
        json.dump(info, f, indent=4)
    shutil.copy(src_dirs[0] / "meta" / "tasks.jsonl", output_path / "meta" / "tasks.jsonl")
    with (output_path / "meta" / "episodes.jsonl").open("w") as f:
        for ep in episodes_out:
            f.write(json.dumps(ep) + "\n")
    with (output_path / "meta" / "episodes_stats.jsonl").open("w") as f:
        for ep, ep_stat in zip(episodes_out, ep_stats_list):
            f.write(json.dumps({"episode_index": ep["episode_index"], "stats": ep_stat}) + "\n")
    with (output_path / "meta" / "stats.json").open("w") as f:
        json.dump(aggregate_stats_count_weighted(ep_stats_list), f, indent=2)

    print(f"\n✓ Merge complete: {new_ep_idx} episodes, {total_frames} frames → {output_path.resolve()}")


def _convert_single(input_path: Path, output_path: Path, overwrite: bool = False) -> None:
    if output_path.exists():
        if not overwrite:
            raise FileExistsError(
                f"Output path already exists: {output_path}\n"
                "Re-run with --overwrite or delete it manually."
            )
        print(f"  Removing existing output directory: {output_path}")
        shutil.rmtree(output_path)

    # ── 1. Load input info.json ─────────────────────────────────────────────
    with (input_path / "meta" / "info.json").open() as f:
        info = json.load(f)

    in_features = info["features"]
    if ACTION_SRC_COL not in in_features:
        raise KeyError(
            f"Expected feature '{ACTION_SRC_COL}' not found in input info.json. "
            "Is this a stream_mode=6 GR00T-WBC dataset?"
        )

    # Rename action.wbc → action in the feature dict.
    in_features[ACTION_DST_COL] = in_features.pop(ACTION_SRC_COL)
    in_features[ACTION_DST_COL]["names"] = [
        f"joint_{i}" for i in range(in_features[ACTION_DST_COL]["shape"][0])
    ]

    # Keep only the features ACT consumes.
    info["features"] = {k: v for k, v in in_features.items() if k in KEEP_FEATURE_KEYS}
    info["robot_type"] = info.get("robot_type") or "g1"

    missing = KEEP_FEATURE_KEYS - set(info["features"].keys())
    if missing:
        raise KeyError(
            f"Input info.json is missing required features: {sorted(missing)}"
        )

    # ── 2. Create output tree ───────────────────────────────────────────────
    output_path.mkdir(parents=True)
    (output_path / "meta").mkdir()
    (output_path / "data").mkdir()

    # ── 3. Read input episodes index ────────────────────────────────────────
    with (input_path / "meta" / "episodes.jsonl").open() as f:
        episodes = [json.loads(line) for line in f if line.strip()]

    # ── 4. Convert each episode parquet + compute stats ─────────────────────
    ep_stats_list: list[dict] = []
    total_frames = 0
    chunks_size = info.get("chunks_size", 1000)

    for ep in episodes:
        ep_idx = ep["episode_index"]
        chunk = ep_idx // chunks_size
        src_parquet = (
            input_path / "data" / f"chunk-{chunk:03d}" / f"episode_{ep_idx:06d}.parquet"
        )
        if not src_parquet.exists():
            raise FileNotFoundError(f"Missing input parquet: {src_parquet}")

        dst_chunk_dir = output_path / "data" / f"chunk-{chunk:03d}"
        dst_chunk_dir.mkdir(parents=True, exist_ok=True)
        dst_parquet = dst_chunk_dir / f"episode_{ep_idx:06d}.parquet"

        df = pd.read_parquet(src_parquet)
        df = df.rename(columns={ACTION_SRC_COL: ACTION_DST_COL})

        # Keep only columns ACT expects.
        keep = [c for c in df.columns if c in KEEP_FEATURE_KEYS]
        missing_cols = KEEP_FEATURE_KEYS - set(keep) - set(IMAGE_FEATURES)  # image comes from videos
        if missing_cols:
            raise KeyError(
                f"Episode {ep_idx}: missing required columns in parquet: {sorted(missing_cols)}"
            )
        df = df[keep]
        df.to_parquet(dst_parquet, index=False)

        # Per-episode stats for numeric features.
        n = len(df)
        ep_stat: dict[str, dict] = {}
        for col in NUMERIC_FEATURES:
            ep_stat[col] = compute_numeric_stats(_stack_vectors(df[col]))

        # Per-episode stats for image features (synthesized, ImageNet defaults).
        for col in IMAGE_FEATURES:
            ep_stat[col] = image_stats_for_episode(n)

        ep_stats_list.append(ep_stat)
        total_frames += n
        print(f"  Episode {ep_idx:3d}: {n:5d} frames  →  {dst_parquet.name}")

    validate_stats(ep_stats_list)

    # ── 5. Copy videos ──────────────────────────────────────────────────────
    videos_src = input_path / "videos"
    videos_dst = output_path / "videos"
    if videos_src.exists():
        shutil.copytree(videos_src, videos_dst)
        print(f"  Copied videos: {videos_src} → {videos_dst}")
    else:
        raise FileNotFoundError(
            f"Input dataset has no 'videos/' directory — ACT needs video frames."
        )

    # ── 6. Update & write info.json ─────────────────────────────────────────
    info["total_episodes"] = len(episodes)
    info["total_frames"] = total_frames
    info["total_videos"] = len(episodes) * len(IMAGE_FEATURES)
    info["splits"] = {"train": f"0:{len(episodes)}"}
    # Drop per-script metadata that lerobot does not use.
    info.pop("script_config", None)
    info.pop("discarded_episode_indices", None)

    with (output_path / "meta" / "info.json").open("w") as f:
        json.dump(info, f, indent=4)

    # ── 7. tasks.jsonl & episodes.jsonl ─────────────────────────────────────
    shutil.copy(
        input_path / "meta" / "tasks.jsonl",
        output_path / "meta" / "tasks.jsonl",
    )

    with (output_path / "meta" / "episodes.jsonl").open("w") as f:
        for ep in episodes:
            # Only keep the fields lerobot expects.
            f.write(json.dumps({
                "episode_index": ep["episode_index"],
                "tasks": ep["tasks"],
                "length": ep["length"],
            }) + "\n")

    # ── 8. episodes_stats.jsonl (primary stats file) ────────────────────────
    with (output_path / "meta" / "episodes_stats.jsonl").open("w") as f:
        for ep, ep_stat in zip(episodes, ep_stats_list):
            f.write(json.dumps({
                "episode_index": ep["episode_index"],
                "stats": ep_stat,
            }) + "\n")

    # ── 9. stats.json (aggregate, count-weighted) ───────────────────────────
    with (output_path / "meta" / "stats.json").open("w") as f:
        json.dump(aggregate_stats_count_weighted(ep_stats_list), f, indent=2)

    # ── 10. Final summary ───────────────────────────────────────────────────
    print("\n✓ Conversion complete.")
    print(f"  Output:   {output_path.resolve()}")
    print(f"  Episodes: {len(episodes)}")
    print(f"  Frames:   {total_frames}")
    print(f"  Features: {sorted(info['features'].keys())}")
    print("\nTrain with:")
    print(f"""
  python -m lerobot.scripts.train \\
      --policy.type act \\
      --dataset.repo_id g1_wbc \\
      --dataset.root {output_path.resolve()} \\
      --policy.chunk_size 50 \\
      --policy.n_action_steps 50 \\
      --batch_size 8 \\
      --steps 50000 \\
      --output_dir outputs/act_checkpoints
""")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input", required=True, nargs="+", type=Path,
        help=(
            "One or more input dataset paths (output of process_dataset.py). "
            "When multiple paths are given they are merged into a single output dataset "
            "with continuously re-indexed episodes."
        ),
    )
    parser.add_argument("--output", required=True, type=Path,
                        help="Where to write the ACT-compatible dataset")
    parser.add_argument("--overwrite", action="store_true",
                        help="Delete --output if it already exists")
    args = parser.parse_args()
    convert(args.input, args.output, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
