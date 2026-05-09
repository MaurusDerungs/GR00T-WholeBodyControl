"""ZMQ publisher that makes the Python Quest/WBC control loop speak the same
wire protocol as the C++ SONIC deploy binary.

Publishes two ZMQ PUB sockets:

  Port ``state_port`` (default 5557) — "g1_debug" topic
    msgpack map compatible with ZMQStateSubscriber in run_data_exporter.py.
    Contains: base_quat, body_q, hand_q, last_action, last_hand_actions,
    token_state, delta_heading, ros_timestamp.

  Port ``pose_port`` (default 5556) — "pose" topic (Protocol v4)
    Packed binary message compatible with ZMQEndpointInterface v4 in C++.
    Contains: token_state [64], left_hand_joints [7], right_hand_joints [7],
    body_quat_w [4], frame_index [1].
    Also publishes "manager_state" on the same socket to set stream_mode=6
    (Quest mode) so run_data_exporter.py knows how to interpret the data.

Usage::

    pub = ZMQSonicPublisher(state_port=5557, pose_port=5556)
    pub.publish(obs, wbc_action, token_state, left_hand_q, right_hand_q)
    pub.close()
"""

from __future__ import annotations

import json
import struct
import threading
import time

import msgpack
import msgpack_numpy as mnp
import numpy as np
import zmq

# Stream mode 6 = Quest/WBC (new mode alongside SONIC modes 1-5).
QUEST_STREAM_MODE: int = 6

# ZMQ topic strings
_TOPIC_STATE = b"g1_debug"
_TOPIC_POSE = b"pose"
_TOPIC_MANAGER_STATE = b"manager_state"
_TOPIC_ROBOT_CONFIG = b"robot_config"

# Packed message header size (must match C++ HEADER_SIZE = 1280)
_HEADER_SIZE = 1280

# Protocol v4 field spec
_V4_FIELDS = [
    {"name": "token_state",        "dtype": "f32", "shape": [64]},
    {"name": "left_hand_joints",   "dtype": "f32", "shape": [7]},
    {"name": "right_hand_joints",  "dtype": "f32", "shape": [7]},
    {"name": "body_quat_w",        "dtype": "f32", "shape": [4]},
    {"name": "frame_index",        "dtype": "i64", "shape": [1]},
]

# Manager-state field spec (minimal: just stream_mode + toggle flags)
_MANAGER_FIELDS = [
    {"name": "stream_mode",           "dtype": "f32", "shape": [1]},
    {"name": "toggle_data_collection","dtype": "f32", "shape": [1]},
    {"name": "toggle_data_abort",     "dtype": "f32", "shape": [1]},
]


def _pack_header(version: int, fields: list[dict]) -> bytes:
    """Return a null-padded 1280-byte JSON header."""
    header = json.dumps({"v": version, "endian": "le", "fields": fields})
    encoded = header.encode("utf-8")
    if len(encoded) >= _HEADER_SIZE:
        raise ValueError(f"Header too large: {len(encoded)} >= {_HEADER_SIZE}")
    return encoded + b"\x00" * (_HEADER_SIZE - len(encoded))


# Pre-build fixed headers (they don't change frame-to-frame)
_V4_HEADER: bytes = _pack_header(4, _V4_FIELDS)
_MANAGER_HEADER: bytes = _pack_header(1, _MANAGER_FIELDS)


def _pack_v4_pose(
    token_state: np.ndarray,
    left_hand: np.ndarray,
    right_hand: np.ndarray,
    body_quat: np.ndarray,
    frame_index: int,
) -> bytes:
    """Serialize a Protocol-v4 pose message."""
    payload = (
        token_state.astype(np.float32).tobytes()
        + left_hand.astype(np.float32).tobytes()
        + right_hand.astype(np.float32).tobytes()
        + body_quat.astype(np.float32).tobytes()
        + np.array([frame_index], dtype=np.int64).tobytes()
    )
    return _TOPIC_POSE + _V4_HEADER + payload


def _pack_manager_state(stream_mode: int, toggle_dc: bool, toggle_abort: bool) -> bytes:
    """Serialize a minimal manager-state message."""
    payload = (
        np.array([stream_mode], dtype=np.float32).tobytes()
        + np.array([1.0 if toggle_dc else 0.0], dtype=np.float32).tobytes()
        + np.array([1.0 if toggle_abort else 0.0], dtype=np.float32).tobytes()
    )
    return _TOPIC_MANAGER_STATE + _MANAGER_HEADER + payload


class ZMQSonicPublisher:
    """Publishes robot state and motion tokens over ZMQ for data collection.

    Compatible with ``run_data_exporter.py`` without any C++ binary running.
    """

    def __init__(
        self,
        state_host: str = "localhost",
        state_port: int = 5557,
        pose_port: int = 5556,
    ):
        mnp.patch()

        self._ctx = zmq.Context()

        # Socket for g1_debug robot-state messages (port 5557)
        self._state_sock = self._ctx.socket(zmq.PUB)
        self._state_sock.bind(f"tcp://{state_host}:{state_port}")

        # Socket for pose-v4 and manager_state messages (port 5556)
        self._pose_sock = self._ctx.socket(zmq.PUB)
        self._pose_sock.bind(f"tcp://{state_host}:{pose_port}")

        self._frame_index: int = 0

        # Give subscribers time to connect before first publish
        time.sleep(0.2)
        print(
            f"[ZMQSonicPublisher] g1_debug on :{state_port}, "
            f"pose/manager_state on :{pose_port}"
        )

        # Publish robot_config immediately so run_data_exporter.py unblocks,
        # then repeat every 2s in a background thread (C++ deploy does the same).
        self._robot_config = {
            "stream_mode": QUEST_STREAM_MODE,
            "robot": "g1",
            "control_frequency": 50,
        }
        self._config_thread = threading.Thread(
            target=self._robot_config_loop, daemon=True
        )
        self._config_thread.start()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _robot_config_loop(self) -> None:
        """Publish robot_config every 2s so run_data_exporter.py unblocks."""
        while True:
            try:
                payload = msgpack.packb(self._robot_config, default=mnp.encode)
                # Topic prefix with no separator — subscriber strips len("robot_config") bytes
                self._state_sock.send(_TOPIC_ROBOT_CONFIG + payload)
            except Exception:
                pass
            time.sleep(2.0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def publish(
        self,
        obs: dict,
        wbc_action: dict,
        token_state: np.ndarray,
        left_hand_q: np.ndarray,
        right_hand_q: np.ndarray,
        toggle_data_collection: bool = False,
        toggle_data_abort: bool = False,
    ) -> None:
        """Publish one control-loop tick over both sockets.

        Args:
            obs: Output of ``G1Env.observe()``
                Required keys: ``q``, ``dq``, ``floating_base_pose``.
            wbc_action: Output of ``WBCPolicy.get_action()``
                Required key: ``q``.
            token_state: (64,) float32 — SONIC motion latent token.
            left_hand_q:  (7,) float32 — left Dex3 joint positions (actuator order).
            right_hand_q: (7,) float32 — right Dex3 joint positions (actuator order).
            toggle_data_collection: Rising-edge flag to start/stop recording.
            toggle_data_abort:      Rising-edge flag to discard current episode.
        """
        base_pose = np.asarray(obs.get("floating_base_pose", np.zeros(7)), dtype=np.float64)
        # floating_base_pose: [tx, ty, tz, qx, qy, qz, qw] (pinocchio convention)
        # g1_debug base_quat: [qw, qx, qy, qz]
        if len(base_pose) >= 7:
            qx, qy, qz, qw = base_pose[3], base_pose[4], base_pose[5], base_pose[6]
        else:
            qx, qy, qz, qw = 0.0, 0.0, 0.0, 1.0
        base_quat = np.array([qw, qx, qy, qz], dtype=np.float64)

        q: np.ndarray = np.asarray(obs["q"], dtype=np.float64)
        dq: np.ndarray = np.asarray(obs.get("dq", np.zeros_like(q)), dtype=np.float64)
        action_q: np.ndarray = np.asarray(wbc_action.get("q", np.zeros_like(q)), dtype=np.float64)

        body_q = q[:29]
        body_dq = dq[:29]
        body_action = action_q[:29]

        # Ensure hand arrays are 7-D
        left_hand_q = np.asarray(left_hand_q, dtype=np.float64).flatten()[:7]
        right_hand_q = np.asarray(right_hand_q, dtype=np.float64).flatten()[:7]
        left_hand_action = np.asarray(left_hand_q, dtype=np.float64)
        right_hand_action = np.asarray(right_hand_q, dtype=np.float64)

        token_f64 = np.asarray(token_state, dtype=np.float64).flatten()[:64]

        # ── g1_debug msgpack ───────────────────────────────────────────────
        state_msg: dict = {
            "control_loop_type": "quest_wbc",
            "index":             self._frame_index,
            "ros_timestamp":     time.time(),
            # IMU
            "base_quat":         base_quat.tolist(),
            "base_ang_vel":      obs.get("floating_base_vel", np.zeros(6))[:3].tolist(),
            # Body joints
            "body_q":            body_q.tolist(),
            "body_dq":           body_dq.tolist(),
            # Hand joints
            "left_hand_q":       left_hand_q.tolist(),
            "left_hand_dq":      np.zeros(7).tolist(),
            "right_hand_q":      right_hand_q.tolist(),
            "right_hand_dq":     np.zeros(7).tolist(),
            # Last actions
            "last_action":          body_action.tolist(),
            "last_left_hand_action": left_hand_action.tolist(),
            "last_right_hand_action": right_hand_action.tolist(),
            # SONIC motion token (64-D)
            "token_state":       token_f64.tolist(),
            # Heading (identity for Quest; teleop loop can override)
            "delta_heading":     0.0,
            "init_base_quat":    base_quat.tolist(),
        }
        self._state_sock.send(_TOPIC_STATE + msgpack.packb(state_msg, use_bin_type=True))

        # ── Protocol v4 pose message ───────────────────────────────────────
        # body_quat_w in pose topic: [qw, qx, qy, qz] as float32
        body_quat_f32 = base_quat.astype(np.float32)
        pose_msg = _pack_v4_pose(
            token_state=token_f64.astype(np.float32),
            left_hand=left_hand_q.astype(np.float32),
            right_hand=right_hand_q.astype(np.float32),
            body_quat=body_quat_f32,
            frame_index=self._frame_index,
        )
        self._pose_sock.send(pose_msg)

        # ── Manager state (stream mode + toggle flags) ─────────────────────
        mgr_msg = _pack_manager_state(
            stream_mode=QUEST_STREAM_MODE,
            toggle_dc=toggle_data_collection,
            toggle_abort=toggle_data_abort,
        )
        self._pose_sock.send(mgr_msg)

        self._frame_index += 1

    def close(self) -> None:
        """Shut down ZMQ sockets and context."""
        self._state_sock.close()
        self._pose_sock.close()
        self._ctx.term()
