"""
Convert a GR00T-WholeBodyControl LeRobot v2.1 dataset (stream_mode=6) to a
format compatible with LeRobot's ACT training script.

What this script does:
  1. Renames  action.wbc  →  action  (ACT expects the key "action")
  2. Keeps only the columns ACT uses:
       observation.state, action, frame_index, episode_index, index,
       task_index, timestamp
  3. Updates meta/info.json to reflect the renamed feature
  4. Computes normalization statistics (mean/std/min/max) and writes
       meta/stats.json  and  meta/episodes_stats.jsonl
  5. Copies videos unchanged (they live at the same relative paths)

The output is a self-contained LeRobot v2.1 dataset that you can pass to
lerobot's train.py without pushing it to HuggingFace Hub.

Usage (run from GR00T-WholeBodyControl root, with the data-collection venv
active *or* via the Docker container that has lerobot installed):

    python gear_sonic/scripts/convert_to_lerobot_act.py \\
        --input  outputs/training_data_clean \\
        --output outputs/act_dataset

Then train with:

    python -m lerobot.scripts.train \\
        --policy.type act \\
        --dataset.repo_id g1_wbc \\
        --dataset.root outputs/act_dataset \\
        --policy.chunk_size 50 \\
        --policy.n_action_steps 50 \\
        --batch_size 8 \\
        --steps 50000 \\
        --output_dir outputs/act_checkpoints
"""

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Columns to keep in the output parquet (teleop.* columns are discarded)
# ---------------------------------------------------------------------------
KEEP_COLS = {
    "observation.state",
    "observation.eef_state",
    "action.wbc",          # → renamed to "action"
    "frame_index",
    "episode_index",
    "index",
    "task_index",
    "timestamp",
}

ACTION_SRC = "action.wbc"
ACTION_DST = "action"


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _to_matrix(series: pd.Series) -> np.ndarray:
    """Stack a pandas Series of arrays into shape (N, D)."""
    return np.vstack([np.asarray(x, dtype=np.float64) for x in series])


def compute_feature_stats(arr: np.ndarray) -> dict:
    """Compute mean/std/min/max along axis 0 (over all frames)."""
    return {
        "mean": arr.mean(axis=0).tolist(),
        "std":  arr.std(axis=0).tolist(),
        "min":  arr.min(axis=0).tolist(),
        "max":  arr.max(axis=0).tolist(),
    }


def aggregate_stats(per_episode: list[dict]) -> dict:
    """Merge per-episode stats into a global stat dict."""
    global_stats: dict[str, dict] = {}
    for ep_stats in per_episode:
        for key, s in ep_stats.items():
            if key not in global_stats:
                global_stats[key] = {k: [] for k in s}
            for metric, val in s.items():
                global_stats[key][metric].append(np.asarray(val))

    merged: dict[str, dict] = {}
    for key, accum in global_stats.items():
        merged[key] = {
            "mean": np.mean(accum["mean"], axis=0).tolist(),
            "std":  np.mean(accum["std"],  axis=0).tolist(),
            "min":  np.min(accum["min"],   axis=0).tolist(),
            "max":  np.max(accum["max"],   axis=0).tolist(),
        }
    return merged


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(input_path: Path, output_path: Path) -> None:
    if output_path.exists():
        raise FileExistsError(
            f"Output path already exists: {output_path}\n"
            "Delete it or choose a different --output path."
        )

    # ── 1. Load and patch info.json ─────────────────────────────────────────
    with open(input_path / "meta" / "info.json") as f:
        info = json.load(f)

    features = info["features"]

    # Rename action.wbc → action
    if ACTION_SRC not in features:
        raise KeyError(
            f"Expected feature '{ACTION_SRC}' not found in info.json. "
            "Is this a stream_mode=6 GR00T-WBC dataset?"
        )
    features[ACTION_DST] = features.pop(ACTION_SRC)
    features[ACTION_DST]["names"] = [
        f"joint_{i}" for i in range(features[ACTION_DST]["shape"][0])
    ]

    # Drop all teleop.* and other non-essential features from info so that
    # lerobot only sees the keys ACT cares about.
    keep_feature_keys = {
        "observation.images.ego_view",
        "observation.state",
        "observation.eef_state",
        ACTION_DST,
        "timestamp",
        "frame_index",
        "episode_index",
        "index",
        "task_index",
    }
    info["features"] = {k: v for k, v in features.items() if k in keep_feature_keys}
    info["robot_type"] = "g1"

    # ── 2. Create output directory tree ─────────────────────────────────────
    output_path.mkdir(parents=True)
    (output_path / "meta").mkdir()
    (output_path / "data").mkdir()

    # ── 3. Convert each episode's parquet ───────────────────────────────────
    ep_stats_list: list[dict] = []
    episodes_meta = []

    with open(input_path / "meta" / "episodes.jsonl") as f:
        episodes = [json.loads(l) for l in f if l.strip()]

    data_src_dir = input_path / "data"
    data_dst_dir = output_path / "data"

    for ep in episodes:
        ep_idx = ep["episode_index"]
        chunk = ep_idx // info["chunks_size"]
        src_parquet = (
            data_src_dir
            / f"chunk-{chunk:03d}"
            / f"episode_{ep_idx:06d}.parquet"
        )
        dst_chunk_dir = data_dst_dir / f"chunk-{chunk:03d}"
        dst_chunk_dir.mkdir(parents=True, exist_ok=True)
        dst_parquet = dst_chunk_dir / f"episode_{ep_idx:06d}.parquet"

        df = pd.read_parquet(src_parquet)

        # Rename action column
        df = df.rename(columns={ACTION_SRC: ACTION_DST})

        # Keep only relevant columns
        keep = [c for c in df.columns if c in keep_feature_keys]
        df = df[keep]

        df.to_parquet(dst_parquet, index=False)

        # Per-episode stats (only numeric array features)
        ep_stat: dict[str, dict] = {}
        for col in [ACTION_DST, "observation.state"]:
            if col in df.columns:
                arr = _to_matrix(df[col])
                ep_stat[col] = compute_feature_stats(arr)
        ep_stats_list.append(ep_stat)

        episodes_meta.append(ep)
        print(f"  Episode {ep_idx}: {len(df)} frames → {dst_parquet.name}")

    # ── 4. Copy videos (unchanged) ──────────────────────────────────────────
    videos_src = input_path / "videos"
    videos_dst = output_path / "videos"
    if videos_src.exists():
        shutil.copytree(videos_src, videos_dst)
        print(f"  Copied videos: {videos_src} → {videos_dst}")

    # ── 5. Write meta files ─────────────────────────────────────────────────
    # info.json
    with open(output_path / "meta" / "info.json", "w") as f:
        json.dump(info, f, indent=4)

    # tasks.jsonl
    shutil.copy(input_path / "meta" / "tasks.jsonl",
                output_path / "meta" / "tasks.jsonl")

    # episodes.jsonl
    with open(output_path / "meta" / "episodes.jsonl", "w") as f:
        for ep in episodes_meta:
            f.write(json.dumps(ep) + "\n")

    # ── 6. Compute and write stats ──────────────────────────────────────────
    global_stats = aggregate_stats(ep_stats_list)

    # ImageNet stats for the camera (lerobot uses these by default anyway)
    global_stats["observation.images.ego_view"] = {
        "mean": [0.485, 0.456, 0.406],
        "std":  [0.229, 0.224, 0.225],
        "min":  [0.0, 0.0, 0.0],
        "max":  [1.0, 1.0, 1.0],
    }

    with open(output_path / "meta" / "stats.json", "w") as f:
        json.dump(global_stats, f, indent=2)

    # episodes_stats.jsonl (one JSON object per episode)
    with open(output_path / "meta" / "episodes_stats.jsonl", "w") as f:
        for i, ep_stat in enumerate(ep_stats_list):
            ep_stat_entry = {"episode_index": i, **ep_stat}
            f.write(json.dumps(ep_stat_entry) + "\n")

    print("\n✓ Conversion complete.")
    print(f"  Output: {output_path.resolve()}")
    print(f"  Episodes: {len(episodes_meta)}")
    print("\nTo train ACT:")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input",  required=True, type=Path,
                        help="Path to training_data_clean (output of process_dataset.py)")
    parser.add_argument("--output", required=True, type=Path,
                        help="Where to write the ACT-compatible dataset")
    args = parser.parse_args()
    convert(args.input, args.output)


if __name__ == "__main__":
    main()
