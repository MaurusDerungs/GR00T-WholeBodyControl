import numpy as np
from scipy.spatial.transform import Rotation as R

from decoupled_wbc.control.teleop.device.quest.quest_client import QuestBridgeClient
from decoupled_wbc.control.teleop.streamers.base_streamer import BaseStreamer, StreamerOutput


R_HEADSET_TO_WORLD = np.array(
    [
        [0, 0, -1],
        [-1, 0, 0],
        [0, 1, 0],
    ]
)


class QuestStreamer(BaseStreamer):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        request_timeout: float = 0.05,
        max_stale_seconds: float = 0.5,
    ):
        self.quest_client = QuestBridgeClient(host=host, port=port, timeout=request_timeout)
        self.max_stale_seconds = max_stale_seconds
        self.reset_status()

    def reset_status(self):
        self.current_base_height = 0.74
        self.toggle_policy_action_last = False
        self.toggle_activation_last = False
        self.toggle_data_collection_last = False
        self.toggle_data_abort_last = False

    def start_streaming(self):
        pass

    def stop_streaming(self):
        pass

    def get(self) -> StreamerOutput:
        quest_data = self.quest_client.get_state()

        if quest_data is None:
            return self._safe_idle_output()

        if quest_data.get("stale", True):
            if float(quest_data.get("received_age_sec", 999.0)) > self.max_stale_seconds:
                return self._safe_idle_output()

        if not quest_data.get("session", {}).get("active", False):
            return self._safe_idle_output()

        return self._generate_unified_raw_data(quest_data)

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
            source="quest",
        )

    def _button_value(self, controller: dict, index: int) -> float:
        buttons = controller.get("buttons", [])
        if 0 <= index < len(buttons):
            return float(buttons[index].get("value", 0.0))
        return 0.0

    def _button_pressed(self, controller: dict, *indices: int) -> bool:
        buttons = controller.get("buttons", [])
        for index in indices:
            if 0 <= index < len(buttons) and bool(buttons[index].get("pressed", False)):
                return True
        return False

    def _pose_to_vector(self, pose: dict | None) -> np.ndarray | None:
        if not pose:
            return None
        position = np.array(pose["position"], dtype=np.float64)
        orientation = np.array(pose["orientation"], dtype=np.float64)
        return np.concatenate([position, orientation])

    def _process_xr_pose(self, controller_pose: np.ndarray, headset_pose: np.ndarray) -> np.ndarray:
        xr_pose_xyz = controller_pose[:3]
        xr_pose_quat = controller_pose[3:]

        if np.allclose(xr_pose_quat, 0):
            xr_pose_quat = np.array([0, 0, 0, 1], dtype=np.float64)

        xr_pose_xyz = R_HEADSET_TO_WORLD @ xr_pose_xyz
        xr_pose_rotation = R.from_quat(xr_pose_quat).as_matrix()
        xr_pose_rotation = R_HEADSET_TO_WORLD @ xr_pose_rotation @ R_HEADSET_TO_WORLD.T

        headset_pose_xyz = headset_pose[:3]
        headset_pose_quat = headset_pose[3:]

        if np.allclose(headset_pose_quat, 0):
            headset_pose_quat = np.array([0, 0, 0, 1], dtype=np.float64)

        headset_pose_xyz = R_HEADSET_TO_WORLD @ headset_pose_xyz
        headset_pose_rotation = R.from_quat(headset_pose_quat).as_matrix()
        headset_pose_rotation = R_HEADSET_TO_WORLD @ headset_pose_rotation @ R_HEADSET_TO_WORLD.T

        xr_pose_xyz_delta = xr_pose_xyz - headset_pose_xyz

        headset_pose_yaw = R.from_matrix(headset_pose_rotation).as_euler("xyz")[2]
        inverse_yaw_rotation = R.from_euler("z", -headset_pose_yaw).as_matrix()

        xr_pose_xyz_delta_compensated = inverse_yaw_rotation @ xr_pose_xyz_delta
        xr_pose_rotation_compensated = inverse_yaw_rotation @ xr_pose_rotation

        xr_pose_T = np.eye(4)
        xr_pose_T[:3, :3] = xr_pose_rotation_compensated
        xr_pose_T[:3, 3] = xr_pose_xyz_delta_compensated
        return xr_pose_T

    def _apply_dead_zone(self, value: float, dead_zone: float) -> float:
        if abs(value) < dead_zone:
            return 0.0
        sign = 1 if value > 0 else -1
        return sign * (abs(value) - dead_zone) / (1.0 - dead_zone)

    def _generate_finger_data(self, trigger_value: float, grip_value: float) -> np.ndarray:
        fingertips = np.zeros((25, 4, 4), dtype=np.float64)

        thumb = 0
        index = 5
        middle = 10
        ring = 15

        fingertips[4 + thumb, 0, 3] = 1.0
        if trigger_value > 0.5 and grip_value <= 0.5:
            fingertips[4 + index, 0, 3] = 1.0
        elif trigger_value > 0.5 and grip_value > 0.5:
            fingertips[4 + index, 0, 3] = 1.0
            fingertips[4 + middle, 0, 3] = 1.0
        elif trigger_value <= 0.5 and grip_value > 0.5:
            fingertips[4 + middle, 0, 3] = 1.0
            fingertips[4 + ring, 0, 3] = 1.0
        return fingertips

    def _edge_toggle(self, current_value: bool, last_attr: str) -> bool:
        previous_value = getattr(self, last_attr)
        edge = current_value and not previous_value
        setattr(self, last_attr, current_value)
        return edge

    def _generate_unified_raw_data(self, quest_data: dict) -> StreamerOutput:
        left_controller = quest_data.get("controllers", {}).get("left", {})
        right_controller = quest_data.get("controllers", {}).get("right", {})
        headset_pose = self._pose_to_vector(quest_data.get("headset"))

        if headset_pose is None:
            return self._safe_idle_output()

        ik_data = {}

        left_pose = self._pose_to_vector(left_controller.get("pose"))
        right_pose = self._pose_to_vector(right_controller.get("pose"))

        left_trigger = self._button_value(left_controller, 0)
        right_trigger = self._button_value(right_controller, 0)
        left_grip = self._button_value(left_controller, 1)
        right_grip = self._button_value(right_controller, 1)

        left_axis_click = self._button_pressed(left_controller, 3, 2)
        right_axis_click = self._button_pressed(right_controller, 3, 2)
        left_primary = self._button_pressed(left_controller, 4)
        left_secondary = self._button_pressed(left_controller, 5)
        right_primary = self._button_pressed(right_controller, 4)
        right_secondary = self._button_pressed(right_controller, 5)

        left_axes = left_controller.get("axes", [0.0, 0.0, 0.0, 0.0])
        right_axes = right_controller.get("axes", [0.0, 0.0, 0.0, 0.0])

        if left_pose is not None:
            ik_data["left_wrist"] = self._process_xr_pose(left_pose, headset_pose)
            ik_data["left_fingers"] = {"position": self._generate_finger_data(left_trigger, left_grip)}
        if right_pose is not None:
            ik_data["right_wrist"] = self._process_xr_pose(right_pose, headset_pose)
            ik_data["right_fingers"] = {
                "position": self._generate_finger_data(right_trigger, right_grip)
            }

        dead_zone = 0.1
        max_linear_vel = 0.5
        max_angular_vel = 1.0

        fwd_bwd_input = float(left_axes[1]) if len(left_axes) > 1 else 0.0
        strafe_input = -float(left_axes[0]) if len(left_axes) > 0 else 0.0
        yaw_input = -float(right_axes[0]) if len(right_axes) > 0 else 0.0

        lin_vel_x = self._apply_dead_zone(fwd_bwd_input, dead_zone) * max_linear_vel
        lin_vel_y = self._apply_dead_zone(strafe_input, dead_zone) * max_linear_vel
        ang_vel_z = self._apply_dead_zone(yaw_input, dead_zone) * max_angular_vel

        height_increment = 0.01
        if left_secondary:
            self.current_base_height += height_increment
        elif left_primary:
            self.current_base_height -= height_increment
        self.current_base_height = np.clip(self.current_base_height, 0.2, 0.74)

        toggle_policy_action = self._edge_toggle(
            left_axis_click and left_trigger > 0.75,
            "toggle_policy_action_last",
        )
        toggle_activation = self._edge_toggle(
            right_axis_click and right_trigger > 0.75,
            "toggle_activation_last",
        )
        toggle_data_collection = self._edge_toggle(
            right_primary,
            "toggle_data_collection_last",
        )
        toggle_data_abort = self._edge_toggle(
            right_secondary,
            "toggle_data_abort_last",
        )

        return StreamerOutput(
            ik_data=ik_data,
            control_data={
                "base_height_command": float(self.current_base_height),
                "navigate_cmd": [lin_vel_x, lin_vel_y, ang_vel_z],
                "toggle_policy_action": toggle_policy_action,
            },
            teleop_data={
                "toggle_activation": toggle_activation,
            },
            data_collection_data={
                "toggle_data_collection": toggle_data_collection,
                "toggle_data_abort": toggle_data_abort,
            },
            source="quest",
        )
