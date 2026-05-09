import threading
import time
from typing import Any

import numpy as np

from decoupled_wbc.control.teleop.streamers.base_streamer import BaseStreamer, StreamerOutput

# OpenXR (Quest): +X right, +Y up, +Z backward
# Robot body:     +X forward, +Y left, +Z up
# Operator stands BEHIND the robot (same facing direction).
R_QUEST_TO_ROBOT = np.array(
    [
        [0, 0, -1],
        [-1, 0, 0],
        [0, 1, 0],
    ]
)


class OculusStreamer(BaseStreamer):
    """
    Streams wrist poses from a Meta Quest headset via OculusReader (USB/ADB).

    Reads 4x4 controller pose matrices from OculusReader in a background thread,
    converts them from OpenXR frame to robot-body frame, and exposes them as a
    StreamerOutput compatible with the rest of the GR00T teleop pipeline.

    IK is intentionally left to TeleopRetargetingIK downstream; this class only
    handles raw pose streaming and button parsing.

    Button mapping (OculusReader convention):
        A           → toggle_activation    (right primary)
        B           → toggle_policy_action (right secondary)
        RightThumb  → toggle_data_collection
        LeftThumb   → toggle_data_abort
        X / Y       → raise / lower base height
        LeftJS      → forward/strafe navigation
        RightJS     → yaw navigation
        RightTrig / RightGrip → right finger proxy
        LeftTrig  / LeftGrip  → left finger proxy
    """

    def __init__(self, max_stale_seconds: float = 0.1):
        self.max_stale_seconds = max_stale_seconds
        self._lock = threading.Lock()
        self._left_T: np.ndarray | None = None
        self._right_T: np.ndarray | None = None
        self._buttons: dict[str, Any] = {}
        self._last_update: float = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self.reset_status()

    # ------------------------------------------------------------------
    # BaseStreamer interface
    # ------------------------------------------------------------------

    def reset_status(self):
        self.current_base_height = 0.74
        self.toggle_policy_action_last = False
        self.toggle_activation_last = False
        self.toggle_data_collection_last = False
        self.toggle_data_abort_last = False

    def start_streaming(self):
        from oculus_reader.reader import OculusReader  # imported lazily to avoid hard dependency

        self._reader = OculusReader()
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop_streaming(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def get(self) -> StreamerOutput:
        with self._lock:
            left_T = self._left_T.copy() if self._left_T is not None else None
            right_T = self._right_T.copy() if self._right_T is not None else None
            buttons = dict(self._buttons)
            age = time.monotonic() - self._last_update

        if age > self.max_stale_seconds or (left_T is None and right_T is None):
            return self._safe_idle_output()

        return self._build_output(left_T, right_T, buttons)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_loop(self):
        import concurrent.futures

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _READ_TIMEOUT = 1.5  # seconds — longer than the WBC watchdog won't help, so keep short

        while self._running:
            try:
                future = executor.submit(self._reader.get_transformations_and_buttons)
                try:
                    poses, buttons = future.result(timeout=_READ_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    print("[OculusStreamer] read timeout — Quest may be sleeping or USB disconnected")
                    time.sleep(0.1)
                    continue

                with self._lock:
                    T_r = poses.get("r")
                    T_l = poses.get("l")
                    if T_r is not None:
                        self._right_T = self._to_robot_frame(np.asarray(T_r, dtype=np.float64))
                    if T_l is not None:
                        self._left_T = self._to_robot_frame(np.asarray(T_l, dtype=np.float64))
                    self._buttons = buttons if buttons else {}
                    self._last_update = time.monotonic()
            except Exception as e:
                print(f"[OculusStreamer] read error: {e}")
            time.sleep(0.01)

    def _to_robot_frame(self, T: np.ndarray) -> np.ndarray:
        """Convert a 4x4 OpenXR transform to robot-body frame."""
        T_robot = np.eye(4)
        T_robot[:3, :3] = R_QUEST_TO_ROBOT @ T[:3, :3] @ R_QUEST_TO_ROBOT.T
        T_robot[:3, 3] = R_QUEST_TO_ROBOT @ T[:3, 3]
        return T_robot

    def _safe_idle_output(self) -> StreamerOutput:
        return StreamerOutput(
            ik_data={},
            control_data={
                "navigate_cmd": [0.0, 0.0, 0.0],
                "base_height_command": self.current_base_height,
                "toggle_policy_action": False,
            },
            teleop_data={"toggle_activation": False},
            data_collection_data={
                "toggle_data_collection": False,
                "toggle_data_abort": False,
            },
            source="oculus",
        )

    def _edge_toggle(self, current_value: bool, last_attr: str) -> bool:
        previous = getattr(self, last_attr)
        edge = current_value and not previous
        setattr(self, last_attr, current_value)
        return edge

    def _btn_float(self, buttons: dict, key: str) -> float:
        val = buttons.get(key, [0.0])
        return float(val[0]) if hasattr(val, "__len__") else float(val)

    def _btn_bool(self, buttons: dict, key: str) -> bool:
        val = buttons.get(key, False)
        return bool(val[0]) if hasattr(val, "__len__") else bool(val)

    def _generate_finger_data(self, trigger: float, grip: float) -> np.ndarray:
        """Encode trigger/grip values as a (25, 4, 4) fingertip transform array."""
        fingertips = np.zeros((25, 4, 4), dtype=np.float64)
        thumb, index, middle, ring = 0, 5, 10, 15
        fingertips[4 + thumb, 0, 3] = 1.0
        if trigger > 0.5 and grip <= 0.5:
            fingertips[4 + index, 0, 3] = 1.0
        elif trigger > 0.5 and grip > 0.5:
            fingertips[4 + index, 0, 3] = 1.0
            fingertips[4 + middle, 0, 3] = 1.0
        elif trigger <= 0.5 and grip > 0.5:
            fingertips[4 + middle, 0, 3] = 1.0
            fingertips[4 + ring, 0, 3] = 1.0
        return fingertips

    def _apply_dead_zone(self, value: float, dead_zone: float) -> float:
        if abs(value) < dead_zone:
            return 0.0
        return (1 if value > 0 else -1) * (abs(value) - dead_zone) / (1.0 - dead_zone)

    def _build_output(
        self,
        left_T: np.ndarray | None,
        right_T: np.ndarray | None,
        buttons: dict,
    ) -> StreamerOutput:
        ik_data = {}
        if left_T is not None:
            ik_data["left_wrist"] = left_T
            left_trig = self._btn_float(buttons, "LeftTrig")
            left_grip = self._btn_float(buttons, "LeftGrip")
            ik_data["left_fingers"] = {"position": self._generate_finger_data(left_trig, left_grip)}
        if right_T is not None:
            ik_data["right_wrist"] = right_T
            right_trig = self._btn_float(buttons, "RightTrig")
            right_grip = self._btn_float(buttons, "RightGrip")
            ik_data["right_fingers"] = {
                "position": self._generate_finger_data(right_trig, right_grip)
            }

        # Thumbstick navigation
        left_js = buttons.get("LeftJS", [0.0, 0.0])
        right_js = buttons.get("RightJS", [0.0, 0.0])
        dead_zone, max_lin, max_ang = 0.1, 0.5, 1.0
        lin_vel_x = self._apply_dead_zone(
            float(left_js[1]) if len(left_js) > 1 else 0.0, dead_zone
        ) * max_lin
        lin_vel_y = self._apply_dead_zone(
            -float(left_js[0]) if len(left_js) > 0 else 0.0, dead_zone
        ) * max_lin
        ang_vel_z = self._apply_dead_zone(
            -float(right_js[0]) if len(right_js) > 0 else 0.0, dead_zone
        ) * max_ang

        # Base height (X raises, Y lowers)
        height_increment = 0.01
        if self._btn_bool(buttons, "X"):
            self.current_base_height += height_increment
        elif self._btn_bool(buttons, "Y"):
            self.current_base_height -= height_increment
        self.current_base_height = float(np.clip(self.current_base_height, 0.2, 0.74))

        toggle_activation = self._edge_toggle(
            self._btn_bool(buttons, "A"), "toggle_activation_last"
        )
        toggle_policy_action = self._edge_toggle(
            self._btn_bool(buttons, "B"), "toggle_policy_action_last"
        )
        toggle_data_collection = self._edge_toggle(
            self._btn_bool(buttons, "RightThumb"), "toggle_data_collection_last"
        )
        toggle_data_abort = self._edge_toggle(
            self._btn_bool(buttons, "LeftThumb"), "toggle_data_abort_last"
        )

        return StreamerOutput(
            ik_data=ik_data,
            control_data={
                "base_height_command": self.current_base_height,
                "navigate_cmd": [lin_vel_x, lin_vel_y, ang_vel_z],
                "toggle_policy_action": toggle_policy_action,
            },
            teleop_data={"toggle_activation": toggle_activation},
            data_collection_data={
                "toggle_data_collection": toggle_data_collection,
                "toggle_data_abort": toggle_data_abort,
            },
            source="oculus",
        )
