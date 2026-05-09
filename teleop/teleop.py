import sys
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'oculus_reader'))
import pinocchio as pin
import numpy as np
import threading
import time
from oculus_reader.reader import OculusReader

URDF_PATH = os.path.join(_HERE, 'unitree.urdf')
WORKSPACE_RADIUS = 0.35
DAMPING = 0.05

# Quest (OpenXR): +X right, +Y up, +Z backward
# Robot body:     +X forward, +Y left, +Z up
# Assumes operator stands BEHIND the robot (same facing direction).
QUEST_TO_ROBOT = np.array([
    [ 0,  0, -1],   # robot +X (fwd)  = -Quest Z
    [-1,  0,  0],   # robot +Y (left) = -Quest X
    [ 0,  1,  0],   # robot +Z (up)   =  Quest Y
])

RIGHT_ARM_JOINTS = [
    "right_shoulder_pitch_joint", "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint",   "right_elbow_joint",
    "right_wrist_roll_joint",     "right_wrist_pitch_joint",
    "right_wrist_yaw_joint",
]
LEFT_ARM_JOINTS = [
    "left_shoulder_pitch_joint",  "left_shoulder_roll_joint",
    "left_shoulder_yaw_joint",    "left_elbow_joint",
    "left_wrist_roll_joint",      "left_wrist_pitch_joint",
    "left_wrist_yaw_joint",
]

# Robot wrist positions when arms are at rest (tune these if needed)
RIGHT_HOME = np.array([ 0.30, -0.20, 0.10])
LEFT_HOME  = np.array([ 0.30,  0.20, 0.10])


class TeleopInterface:
    def __init__(self):
        self.model = pin.buildModelFromUrdf(URDF_PATH)
        self.data  = self.model.createData()

        self._right_q_ids = self._get_q_ids(RIGHT_ARM_JOINTS)
        self._left_q_ids  = self._get_q_ids(LEFT_ARM_JOINTS)
        self._q_lower     = self.model.lowerPositionLimit
        self._q_upper     = self.model.upperPositionLimit
        self._q_mid_right = np.array([(self._q_lower[i] + self._q_upper[i]) / 2
                                       for i in self._right_q_ids])
        self._q_mid_left  = np.array([(self._q_lower[i] + self._q_upper[i]) / 2
                                       for i in self._left_q_ids])

        self._right_ef = self.model.getFrameId("right_wrist_yaw_joint")
        self._left_ef  = self.model.getFrameId("left_wrist_yaw_joint")

        self._right_angles = np.zeros(7)
        self._left_angles  = np.zeros(7)
        self._lock = threading.Lock()
        self._running = False

    def _get_q_ids(self, joint_names):
        return [self.model.joints[self.model.getJointId(n)].idx_q for n in joint_names]

    def _solve_ik(self, target_pos, ef_id, q_ids, q_mid, q_init):
        q = q_init.copy()
        for _ in range(80):
            pin.forwardKinematics(self.model, self.data, q)
            pin.updateFramePlacements(self.model, self.data)
            error = target_pos - self.data.oMf[ef_id].translation
            if np.linalg.norm(error) < 1e-3:
                break
            pin.computeJointJacobians(self.model, self.data, q)
            J = pin.getFrameJacobian(self.model, self.data, ef_id,
                                     pin.LOCAL_WORLD_ALIGNED)[:3, q_ids]
            JJT = J @ J.T
            dq = J.T @ np.linalg.solve(JJT + DAMPING**2 * np.eye(3), error)
            J_pinv = J.T @ np.linalg.solve(JJT + DAMPING**2 * np.eye(3), np.eye(3))
            dq += (np.eye(7) - J_pinv @ J) @ (0.1 * (q_mid - q[q_ids]))
            q[q_ids] += 0.5 * dq
            for idx in q_ids:
                q[idx] = np.clip(q[idx], self._q_lower[idx], self._q_upper[idx])
        return q[q_ids]

    def _loop(self):
        reader = OculusReader()
        q = np.zeros(self.model.nq)
        right_home = left_home = None

        while self._running:
            poses, _ = reader.get_transformations_and_buttons()
            T_r = poses.get('r')
            T_l = poses.get('l')

            if T_r is not None:
                if right_home is None:
                    right_home = T_r[:3, 3].copy()
                delta = QUEST_TO_ROBOT @ (T_r[:3, 3] - right_home)
                if np.linalg.norm(delta) > WORKSPACE_RADIUS:
                    delta *= WORKSPACE_RADIUS / np.linalg.norm(delta)
                angles = self._solve_ik(RIGHT_HOME + delta, self._right_ef,
                                        self._right_q_ids, self._q_mid_right, q)
                with self._lock:
                    self._right_angles = angles
                q[self._right_q_ids] = angles

            if T_l is not None:
                if left_home is None:
                    left_home = T_l[:3, 3].copy()
                delta = QUEST_TO_ROBOT @ (T_l[:3, 3] - left_home)
                if np.linalg.norm(delta) > WORKSPACE_RADIUS:
                    delta *= WORKSPACE_RADIUS / np.linalg.norm(delta)
                angles = self._solve_ik(LEFT_HOME + delta, self._left_ef,
                                        self._left_q_ids, self._q_mid_left, q)
                with self._lock:
                    self._left_angles = angles
                q[self._left_q_ids] = angles

            time.sleep(0.05)

    def start(self):
        """Start reading the Quest and solving IK in the background."""
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("TeleopInterface started. Hold controllers at rest position first.")

    def stop(self):
        self._running = False

    def get_joint_angles(self):
        """
        Returns (left_angles, right_angles) — each a numpy array of 7 joint angles in radians.
        Order: shoulder_pitch, shoulder_roll, shoulder_yaw, elbow, wrist_roll, wrist_pitch, wrist_yaw
        """
        with self._lock:
            return self._left_angles.copy(), self._right_angles.copy()


if __name__ == "__main__":
    teleop = TeleopInterface()
    teleop.start()
    print("Move your controllers. Ctrl+C to stop.\n")
    try:
        while True:
            left, right = teleop.get_joint_angles()
            print(f"L: {np.degrees(left).round(1)}  R: {np.degrees(right).round(1)}")
            time.sleep(0.1)
    except KeyboardInterrupt:
        teleop.stop()
