"""Motion token encoder: converts G1 joint state → 64-D SONIC motion token.

The SONIC training pipeline uses a MotionBricks VQ-VAE to encode full-body
motion sequences into discrete latent tokens.  During data collection with
the PICO, the C++ binary runs a TensorRT ONNX encoder to produce
``token_state`` from the current robot observation.

For the Quest/WBC pipeline we need to produce an equivalent token from the
Python side.  This module provides two tiers:

  1. **MotionBricks checkpoint** (recommended):
     Load the trained VQ-VAE from a ``.ckpt`` file and run the encoder
     on the current joint state.  Produces tokens in the same latent space
     as the training data → directly compatible with GR00T fine-tuning.

  2. **Standing-pose fallback** (default when no checkpoint is given):
     Returns ``LATENT_INITIAL_MOTION_TOKEN`` (the standing-pose token
     hard-coded from the SONIC reference implementation).  This is correct
     for lower-body state during table-top manipulation.  Upper-arm motion
     is NOT encoded in the token space in this mode; the dataset will still
     contain the correct joint states and EEF poses so fine-tuning on
     wrist-pose-conditioned tasks is still possible.

     To produce fully-compatible SONIC tokens for arm manipulation, provide
     a MotionBricks checkpoint.

Usage::

    encoder = MotionTokenEncoder(checkpoint_path="path/to/vqvae.ckpt")
    token = encoder.encode(obs_q, robot_model)   # -> np.ndarray (64,) float32

Checkpoint requirements
-----------------------
The checkpoint must be a PyTorch Lightning ``.ckpt`` saved from
``MotionVQVAEModel``.  The companion hydra config directory (which contains
``config.yaml`` or ``model/config.yaml``) must either reside next to the
checkpoint or be passed via ``config_dir``.
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Fallback standing-pose token (from gear_sonic/utils/inference/initial_poses.py)
LATENT_INITIAL_MOTION_TOKEN = np.array(
    [
        -0.0625,  0.0000, -0.0625, -0.1250, -0.1875, -0.0625,  0.1875,
         0.2500,  0.1875, -0.1250,  0.0625, -0.0625, -0.2500, -0.2500,
        -0.3125, -0.0625,  0.0000, -0.0625, -0.1250, -0.1875,  0.0000,
        -0.2500,  0.0000, -0.2500, -0.0625,  0.0625,  0.1250, -0.1250,
         0.2500,  0.1875,  0.2500, -0.1250,  0.1250,  0.1875, -0.0625,
         0.0000, -0.1875, -0.1875,  0.2500,  0.0000,  0.0000, -0.1250,
         0.0625,  0.0000, -0.0625, -0.0625,  0.1875, -0.0625,  0.0000,
         0.0625,  0.1250,  0.0625,  0.1250,  0.0625,  0.1250,  0.0000,
         0.1250,  0.1875,  0.0000,  0.0000,  0.0625,  0.0625,  0.1875,
         0.0625,
    ],
    dtype=np.float32,
)

# Number of frames needed to compute finite-difference velocities
_VELOCITY_HISTORY_LEN = 3


class MotionTokenEncoder:
    """Encodes current G1 joint state into a 64-D SONIC motion latent token.

    Parameters
    ----------
    checkpoint_path:
        Path to a MotionBricks VQ-VAE ``.ckpt`` file.  When ``None`` the
        encoder falls back to ``LATENT_INITIAL_MOTION_TOKEN``.
    config_dir:
        Directory containing the Hydra ``config.yaml`` for the checkpoint.
        If ``None`` the code searches for it next to the checkpoint.
    control_frequency:
        Loop frequency in Hz (used for velocity finite differences).
    device:
        Torch device string, e.g. ``"cpu"`` or ``"cuda"``.
    """

    def __init__(
        self,
        checkpoint_path: Optional[str] = None,
        config_dir: Optional[str] = None,
        control_frequency: int = 50,
        device: str = "cpu",
    ):
        self._dt = 1.0 / control_frequency
        self._device = device
        self._model = None
        self._motion_rep = None

        # Rolling buffer for velocity computation (keeps last N joint configs)
        self._q_history: deque[np.ndarray] = deque(maxlen=_VELOCITY_HISTORY_LEN)

        if checkpoint_path is not None:
            self._load_motionbricks(checkpoint_path, config_dir)
        else:
            log.warning(
                "[MotionTokenEncoder] No checkpoint provided — using standing-pose "
                "fallback token.  Upper-body arm motion will NOT be encoded in the "
                "token space.  Provide --sonic_encoder_checkpoint to enable full "
                "MotionBricks encoding."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def encode(self, q: np.ndarray, robot_model=None) -> np.ndarray:
        """Encode joint state ``q`` into a 64-D motion token.

        Parameters
        ----------
        q:
            Full joint configuration in joint order (e.g. 43-D for G1 with
            hands: 29 body + 7 left + 7 right).
        robot_model:
            ``decoupled_wbc.control.robot_model.RobotModel`` instance.
            Required when a MotionBricks checkpoint is loaded; ignored
            in fallback mode.

        Returns
        -------
        np.ndarray of shape ``(64,)`` float32.
        """
        self._q_history.append(np.asarray(q, dtype=np.float32))

        if self._model is not None and robot_model is not None:
            try:
                return self._encode_motionbricks(robot_model)
            except Exception as exc:
                log.warning(f"[MotionTokenEncoder] Encoding failed ({exc}); using fallback.")

        return LATENT_INITIAL_MOTION_TOKEN.copy()

    # ------------------------------------------------------------------
    # MotionBricks checkpoint loading & encoding
    # ------------------------------------------------------------------

    def _load_motionbricks(self, checkpoint_path: str, config_dir: Optional[str]) -> None:
        """Load a MotionVQVAEModel from a Lightning checkpoint."""
        try:
            import torch
            from motionbricks.vqvae.models.motion_vqvae import MotionVQVAEModel

            ckpt = Path(checkpoint_path)
            if not ckpt.exists():
                raise FileNotFoundError(f"Checkpoint not found: {ckpt}")

            # Locate config.yaml alongside the checkpoint
            cfg_path = self._find_config(ckpt, config_dir)
            if cfg_path is None:
                raise FileNotFoundError(
                    f"Could not find config.yaml next to {ckpt}. "
                    "Pass config_dir= explicitly."
                )

            from omegaconf import OmegaConf
            import hydra

            cfg = OmegaConf.load(cfg_path)
            model = hydra.utils.instantiate(cfg.model)
            state = torch.load(checkpoint_path, map_location=self._device)
            model.load_state_dict(state["state_dict"], strict=False)
            model.eval()
            model.to(self._device)

            self._model = model
            self._motion_rep = model.local_motion_rep
            log.info(f"[MotionTokenEncoder] Loaded MotionBricks checkpoint: {ckpt}")

        except ImportError as exc:
            log.error(
                f"[MotionTokenEncoder] motionbricks not importable ({exc}). "
                "Install with: uv pip install -e motionbricks"
            )
        except Exception as exc:
            log.error(f"[MotionTokenEncoder] Failed to load checkpoint: {exc}")

    @staticmethod
    def _find_config(ckpt: Path, config_dir: Optional[str]) -> Optional[Path]:
        """Search for config.yaml near the checkpoint file."""
        candidates = []
        if config_dir is not None:
            candidates.append(Path(config_dir) / "config.yaml")
        # Common Lightning checkpoint layouts
        candidates += [
            ckpt.parent / "config.yaml",
            ckpt.parent.parent / "config.yaml",
            ckpt.parent / ".hydra" / "config.yaml",
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    def _encode_motionbricks(self, robot_model) -> np.ndarray:
        """Compute 64-D token via MotionBricks VQ-VAE encoder.

        Uses ``robot_model`` (pinocchio) FK to compute global joint positions
        and 6D rotations, then feeds those into the MotionBricks local
        motion representation and the VQ-VAE encoder.
        """
        import torch

        if len(self._q_history) < 2:
            return LATENT_INITIAL_MOTION_TOKEN.copy()

        q_curr = self._q_history[-1]
        q_prev = self._q_history[-2]

        # --- FK: get global joint positions & rotations -------------------
        robot_model.cache_forward_kinematics(q_curr.astype(np.float64))
        joint_positions = self._get_global_joint_positions(robot_model)  # (N_joints, 3)
        joint_rotations_6d = self._get_global_joint_rotations_6d(robot_model)  # (N_joints, 6)

        robot_model.cache_forward_kinematics(q_prev.astype(np.float64))
        joint_positions_prev = self._get_global_joint_positions(robot_model)

        # Finite-difference velocity
        joint_vel = (joint_positions - joint_positions_prev) / self._dt  # (N, 3)

        # Foot contacts: assume grounded (standing) unless legs moving
        foot_contacts = np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32)

        # Root position
        root_pos = joint_positions[0]

        # Build local motion rep feature vector
        # ric_data: (N-1)*3 — joint positions relative to root XZ
        ric_data = (joint_positions[1:] - np.array([root_pos[0], 0.0, root_pos[2]])).flatten()
        # global_rot_data: N*6
        rot_data = joint_rotations_6d.flatten()
        # local_vel: N*3
        vel_data = joint_vel.flatten()

        # Concatenate → local motion rep (413-D for G1-34)
        motion_feat = np.concatenate([ric_data, rot_data, vel_data, foot_contacts]).astype(np.float32)

        # Normalize using the model's motion rep stats if available
        if hasattr(self._motion_rep, "normalize"):
            motion_tensor = torch.from_numpy(motion_feat).unsqueeze(0).unsqueeze(0)  # [1,1,D]
            motion_tensor = self._motion_rep.normalize(motion_tensor)
        else:
            motion_tensor = torch.from_numpy(motion_feat).unsqueeze(0).unsqueeze(0)

        # Encoder forward pass (pre-quantization continuous embedding)
        with torch.no_grad():
            x_in = self._model.pose_net.extract_feature(
                motion_tensor, self._model.pose_net.encoder_input_feature_mode
            ).permute(0, 2, 1)  # [1, feat_dim, T]
            x_encoded = self._model.pose_net.encoder(x_in)  # [1, code_dim, T']
            # Use the raw encoder output (continuous latent) as the token
            token = x_encoded.squeeze().cpu().numpy().flatten()[:64]

        return token.astype(np.float32)

    # ------------------------------------------------------------------
    # FK helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_global_joint_positions(robot_model) -> np.ndarray:
        """Return (N_joints, 3) global joint positions using pinocchio FK."""
        positions = []
        for frame_name in robot_model.joint_names:
            try:
                placement = robot_model.frame_placement(frame_name)
                positions.append(placement.translation[:3].copy())
            except Exception:
                positions.append(np.zeros(3))
        return np.array(positions, dtype=np.float32)

    @staticmethod
    def _get_global_joint_rotations_6d(robot_model) -> np.ndarray:
        """Return (N_joints, 6) 6D continuous rotation for each joint."""
        from scipy.spatial.transform import Rotation as R

        rotations_6d = []
        for frame_name in robot_model.joint_names:
            try:
                placement = robot_model.frame_placement(frame_name)
                mat = placement.rotation[:3, :3]
                # 6D representation: first two columns of rotation matrix
                rot_6d = np.concatenate([mat[:, 0], mat[:, 1]], axis=0)
            except Exception:
                rot_6d = np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
            rotations_6d.append(rot_6d)
        return np.array(rotations_6d, dtype=np.float32)
