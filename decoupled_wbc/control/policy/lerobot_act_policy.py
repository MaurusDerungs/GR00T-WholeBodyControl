"""
LeRobot ACT policy wrapper for live inference on the G1 robot.

This class loads a LeRobot-trained ACT checkpoint and exposes it through the
`decoupled_wbc.control.base.Policy` interface so it can be plugged into the
standard control loop in place of the usual teleop / WBC policy.

Inputs consumed per tick:
  - observation.state      : 43-D joint positions (from `env.observe()["q"]`)
  - observation.images.ego_view : 480x640 RGB image from the composed camera

Output produced per tick:
  - {"q": np.ndarray(43,)}  — full joint target, ready for `env.queue_action`.

Because ACT is trained on the full 43-D WBC action (action.wbc), there is no
need to go through the decoupled WBC wrapper at inference time: the policy
has already absorbed the lower-body RL behavior into its predictions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from decoupled_wbc.control.base.policy import Policy


# Feature names used in the LeRobot ACT dataset (see
# gear_sonic/scripts/convert_to_lerobot_act.py).
STATE_KEY = "observation.state"
IMAGE_KEY = "observation.images.ego_view"
ACTION_KEY = "action"


class LerobotActPolicy(Policy):
    """Drop-in policy that runs a trained LeRobot ACT checkpoint.

    Args:
        checkpoint_dir: Path to the ``pretrained_model/`` directory (the one
            containing ``config.json`` and ``model.safetensors``).  Accepts
            either the ``pretrained_model/`` directory directly or a training
            checkpoint (e.g. ``outputs/act_checkpoints/checkpoints/last``),
            in which case ``pretrained_model/`` is appended automatically.
        camera_client: A running :class:`ComposedCameraClientSensor`.  Must
            expose images under the ``ego_view`` key.
        device: Torch device to run inference on (``"cuda"`` or ``"cpu"``).
        image_key_in_camera: Key under which the ego view frame is stored in
            the message returned by ``camera_client.read()["images"]``.
            Defaults to ``"ego_view"`` which matches the default camera mount
            position.
    """

    def __init__(
        self,
        checkpoint_dir: str | Path,
        camera_client,
        device: str = "cuda",
        image_key_in_camera: str = "ego_view",
    ):
        # Imports are local so that users who do not need inference do not pay
        # the cost of pulling in torch + lerobot at import time.
        import torch
        from lerobot.common.policies.act.modeling_act import ACTPolicy

        self._torch = torch
        self._ACTPolicy = ACTPolicy

        # Resolve checkpoint directory.  Both styles accepted:
        #   .../outputs/act_checkpoints/checkpoints/last
        #   .../outputs/act_checkpoints/checkpoints/last/pretrained_model
        ckpt = Path(checkpoint_dir).expanduser().resolve()
        if not (ckpt / "config.json").exists():
            if (ckpt / "pretrained_model" / "config.json").exists():
                ckpt = ckpt / "pretrained_model"
            else:
                raise FileNotFoundError(
                    f"Could not find 'config.json' under {ckpt}. "
                    f"Expected a LeRobot pretrained_model directory."
                )
        self.checkpoint_dir = ckpt
        self.device = torch.device(device)
        self.image_key_in_camera = image_key_in_camera
        self.camera_client = camera_client

        print(f"[ACT] Loading policy from {self.checkpoint_dir}")
        self.policy = self._ACTPolicy.from_pretrained(str(self.checkpoint_dir))
        self.policy.to(self.device)
        self.policy.eval()
        self.policy.reset()
        print(f"[ACT] Loaded. Device={self.device}, "
              f"chunk_size={self.policy.config.chunk_size}, "
              f"n_action_steps={self.policy.config.n_action_steps}")

        # Cached latest values.  These are updated by set_observation / get_action.
        self._latest_q: Optional[np.ndarray] = None
        self._latest_image: Optional[np.ndarray] = None  # HxWx3 uint8
        self._last_action: Optional[np.ndarray] = None
        # Camera debug counters.
        self._dbg_read_count = 0
        self._dbg_none_count = 0

    # ------------------------------------------------------------------
    # Policy interface
    # ------------------------------------------------------------------

    def set_observation(self, observation: dict):
        """Cache the latest joint-state observation from the env."""
        q = np.asarray(observation["q"], dtype=np.float64)
        self._latest_q = q

    def set_goal(self, goal: dict):
        # ACT is goal-conditioned only through the input image + state.
        # Nothing to do here.
        pass

    def get_action(self, time: Optional[float] = None) -> dict:
        """Run one ACT inference step and return the target joint vector."""
        if self._latest_q is None:
            raise RuntimeError(
                "LerobotActPolicy.get_action called before set_observation. "
                "Call set_observation(env.observe()) first."
            )

        # 1. Pull the latest camera frame.  If none is available yet (policy
        #    warming up), fall back to holding the last action or the current
        #    joint position.  Never crash the control loop.
        image = self._read_latest_image()
        if image is None:
            if self._last_action is not None:
                return {"q": self._last_action.copy()}
            # Very first tick: hold current pose.
            self._last_action = self._latest_q.copy()
            return {"q": self._last_action.copy()}

        # 2. Assemble the batch in the exact shape lerobot expects.
        batch = self._build_batch(self._latest_q, image)

        # 3. Run ACT inference (returns one action; internal queue manages
        #    the action chunk under the hood).
        with self._torch.no_grad():
            action_tensor = self.policy.select_action(batch)
        action = action_tensor.squeeze(0).detach().to("cpu").numpy().astype(np.float64)

        if action.shape[0] != self._latest_q.shape[0]:
            raise ValueError(
                f"ACT returned action of dim {action.shape[0]} but env expects "
                f"{self._latest_q.shape[0]}. Did training use a different dataset?"
            )

        self._last_action = action
        return {"q": action.copy()}

    def reset(self):
        """Reset the policy's internal action queue (call between episodes)."""
        self.policy.reset()
        self._last_action = None

    def close(self):
        # Nothing special to clean up — the camera client lifecycle is owned
        # by the caller, and the ACT policy does not hold external resources.
        pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_latest_image(self) -> Optional[np.ndarray]:
        """Read the most recent camera frame (non-blocking). Returns HxWx3 uint8."""
        # Try a blocking-with-short-timeout read the first few times so the
        # ZMQ subscriber has a chance to actually receive its first message
        # (non-blocking reads can starve if the loop ticks slightly faster
        # than frames arrive).
        message = None
        # For the first few reads, wait for a message to give the ZMQ SUB
        # socket a chance to warm up (slow-joiner race).  After that, fall
        # back to fully non-blocking polling to stay at 50 Hz.
        if self._latest_image is None:
            # Block up to one control-loop period waiting for the first frame.
            try:
                message = self.camera_client.read(blocking=True)
            except TypeError:
                message = self.camera_client.read()
        else:
            try:
                message = self.camera_client.read(blocking=False)
            except TypeError:
                message = self.camera_client.read()

        self._dbg_read_count += 1
        if message is None:
            self._dbg_none_count += 1
            if self._dbg_read_count <= 5 or self._dbg_read_count % 250 == 0:
                print(
                    f"[ACT-cam] read #{self._dbg_read_count}: client.read() "
                    f"returned None ({self._dbg_none_count} nones total)",
                    flush=True,
                )
            return self._latest_image

        images = message.get("images", {}) if isinstance(message, dict) else {}
        # Log the message schema the first few times we see one.
        if self._dbg_read_count <= 3 or self._latest_image is None:
            print(
                f"[ACT-cam] read #{self._dbg_read_count}: got message with "
                f"image keys={list(images.keys())} "
                f"(looking for '{self.image_key_in_camera}')",
                flush=True,
            )

        frame = images.get(self.image_key_in_camera)
        if frame is None and images:
            # Fall back to whatever single image key the message has so the
            # policy still runs if the naming diverges.
            fallback_key = next(iter(images))
            print(
                f"[ACT-cam] key '{self.image_key_in_camera}' not present, "
                f"falling back to '{fallback_key}'",
                flush=True,
            )
            self.image_key_in_camera = fallback_key
            frame = images.get(fallback_key)

        if frame is None:
            return self._latest_image

        self._latest_image = np.asarray(frame)
        return self._latest_image

    def _build_batch(self, q: np.ndarray, image: np.ndarray) -> dict:
        """Turn (q, image) into the dict of batched torch tensors ACT expects."""
        torch = self._torch

        # observation.state: (1, 43) float32 on device
        state_tensor = torch.from_numpy(q.astype(np.float32))[None].to(self.device)

        # observation.images.ego_view: (1, 3, H, W) float32 in [0, 1]
        if image.dtype == np.uint8:
            img_f = image.astype(np.float32) / 255.0
        else:
            img_f = image.astype(np.float32)
        # HWC → CHW
        img_f = np.transpose(img_f, (2, 0, 1))
        img_tensor = torch.from_numpy(np.ascontiguousarray(img_f))[None].to(self.device)

        return {
            STATE_KEY: state_tensor,
            IMAGE_KEY: img_tensor,
        }


__all__ = ["LerobotActPolicy", "STATE_KEY", "IMAGE_KEY", "ACTION_KEY"]
