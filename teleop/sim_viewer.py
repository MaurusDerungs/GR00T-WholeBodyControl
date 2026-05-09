import sys
import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, 'oculus_reader'))

import mujoco
import mujoco.viewer
import numpy as np
from teleop import TeleopInterface

SCENE_XML = os.path.join(os.path.dirname(_HERE), 'gear_sonic_deploy', 'g1', 'scene_29dof.xml')

LEFT_ARM  = [
    "left_shoulder_pitch_joint",  "left_shoulder_roll_joint",
    "left_shoulder_yaw_joint",    "left_elbow_joint",
    "left_wrist_roll_joint",      "left_wrist_pitch_joint",
    "left_wrist_yaw_joint",
]
RIGHT_ARM = [
    "right_shoulder_pitch_joint", "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint",   "right_elbow_joint",
    "right_wrist_roll_joint",     "right_wrist_pitch_joint",
    "right_wrist_yaw_joint",
]


def main():
    model = mujoco.MjModel.from_xml_path(SCENE_XML)
    data  = mujoco.MjData(model)

    left_adr  = [model.joint(n).qposadr[0] for n in LEFT_ARM]
    right_adr = [model.joint(n).qposadr[0] for n in RIGHT_ARM]

    # Standing pose: free joint xyz + identity quaternion (w,x,y,z)
    data.qpos[0:3] = [0.0, 0.0, 0.8]
    data.qpos[3]   = 1.0
    mujoco.mj_forward(model, data)

    teleop = TeleopInterface()
    teleop.start()
    print("Quest controllers active — move your arms to drive the sim. Close the viewer to quit.")

    with mujoco.viewer.launch_passive(model, data) as viewer:
        viewer.cam.azimuth   = 140
        viewer.cam.elevation = -15
        viewer.cam.distance  = 2.5
        viewer.cam.lookat[:] = [0.0, 0.0, 0.8]

        while viewer.is_running():
            left, right = teleop.get_joint_angles()

            for i, adr in enumerate(left_adr):
                data.qpos[adr] = left[i]
            for i, adr in enumerate(right_adr):
                data.qpos[adr] = right[i]

            mujoco.mj_forward(model, data)
            viewer.sync()
            time.sleep(0.02)

    teleop.stop()


if __name__ == "__main__":
    main()
