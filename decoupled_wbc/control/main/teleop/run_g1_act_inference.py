"""
Live inference loop for a LeRobot ACT checkpoint on the G1 robot.

Architecture
------------
During data collection, the recorded 43-D ``action.wbc`` is produced by
``G1DecoupledWholeBodyPolicy`` which blends:
  - upper body → interpolated teleop target (waist + arms + hands)
  - lower body → closed-loop gear_wbc RL policy (legs + optionally waist)

At deployment we keep that decoupled architecture. ACT replaces the human
teleop source for the upper body, while the gear_wbc policy continues to
provide real balance control:

  ACT (43-D prediction)  →  upper-body slice  →  target_upper_body_pose
                                                             │
                                                             ▼
                                   InterpolationPolicy (smoother)
                                                             │
                         gear_wbc lower-body policy  ───────┤
                                                             ▼
                                      G1DecoupledWholeBodyPolicy
                                                             │
                                                             ▼
                                              env.queue_action({"q": q})

This mirrors the path used by ``run_g1_control_loop.py`` (the teleop loop)
except the upper-body target comes from ACT instead of the teleop ROS topic.

Launch prerequisites
--------------------
1. A camera server publishing the ``ego_view`` frame on ZMQ (typically
   ``gear_sonic.camera.composed_camera`` fed by the Jetson GStreamer pipeline
   — see ``act_inference_launch.py``).
2. A trained LeRobot ACT checkpoint (e.g. from ``train_act.sh``).

Usage::

    python -m decoupled_wbc.control.main.teleop.run_g1_act_inference \\
        --act_checkpoint outputs/act_checkpoints/checkpoints/last \\
        --camera_host localhost --camera_port 5555
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Optional

import numpy as np
import tyro

from decoupled_wbc.control.envs.g1.g1_env import G1Env
from decoupled_wbc.control.main.constants import (
    DEFAULT_BASE_HEIGHT,
    DEFAULT_NAV_CMD,
    STATE_TOPIC_NAME,
)
from decoupled_wbc.control.main.teleop.configs.configs import ActInferenceConfig
from decoupled_wbc.control.policy.lerobot_act_policy import LerobotActPolicy
from decoupled_wbc.control.policy.wbc_policy_factory import get_wbc_policy
from decoupled_wbc.control.robot_model.instantiation.g1 import (
    instantiate_g1_robot_model,
)
from decoupled_wbc.control.utils.keyboard_dispatcher import (
    KeyboardDispatcher,
    KeyboardEStop,
    KeyboardListenerPublisher,
    ROSKeyboardDispatcher,
)
from decoupled_wbc.control.utils.ros_utils import (
    ROSManager,
    ROSMsgPublisher,
)
from decoupled_wbc.control.utils.telemetry import Telemetry


def _import_camera_client():
    """Return the camera client class, preferring the gear_sonic variant that
    the teleop launcher uses. Both variants expose identical ``read()`` that
    returns ``{"images": {...}, "timestamps": {...}}``.
    """
    try:
        from gear_sonic.camera.composed_camera import ComposedCameraClientSensor
        return ComposedCameraClientSensor
    except ImportError:
        from decoupled_wbc.control.sensor.composed_camera import (
            ComposedCameraClientSensor,
        )
        return ComposedCameraClientSensor


CONTROL_NODE_NAME = "ActInferencePolicy"


def main(config: ActInferenceConfig):
    if not config.act_checkpoint:
        raise ValueError(
            "Missing --act_checkpoint. Point it at a pretrained_model directory "
            "or a LeRobot checkpoint like outputs/act_checkpoints/checkpoints/last."
        )

    # ── ROS / env setup (matches run_g1_control_loop.py) ────────────────────
    ros_manager = ROSManager(node_name=CONTROL_NODE_NAME)
    node = ros_manager.node

    wbc_config = config.load_wbc_yaml()

    data_exp_pub = ROSMsgPublisher(STATE_TOPIC_NAME)

    telemetry = Telemetry(window_size=100)

    waist_location = "lower_and_upper_body" if config.enable_waist else "lower_body"
    robot_model = instantiate_g1_robot_model(
        waist_location=waist_location, high_elbow_pose=config.high_elbow_pose
    )

    env = G1Env(
        env_name=config.env_name,
        robot_model=robot_model,
        config=wbc_config,
        wbc_version=config.wbc_version,
    )
    if env.sim and not config.sim_sync_mode:
        env.start_simulator()

    # ── Full decoupled WBC policy (upper-body interpolation + lower-body RL) ─
    # This is the SAME wbc_policy used by run_g1_control_loop.py.  It keeps
    # the gear_wbc base policy running for real balance control — we only
    # swap the upper-body goal source from teleop to ACT.
    wbc_policy = get_wbc_policy(
        "g1", robot_model, wbc_config, config.upper_body_joint_speed
    )

    # ── Camera client ───────────────────────────────────────────────────────
    print(f"[ACT] Connecting to camera server at "
          f"{config.camera_host}:{config.camera_port} …")
    ComposedCameraClientSensor = _import_camera_client()
    camera_client = ComposedCameraClientSensor(
        server_ip=config.camera_host,
        port=config.camera_port,
    )

    # ── ACT policy ──────────────────────────────────────────────────────────
    act_policy = LerobotActPolicy(
        checkpoint_dir=config.act_checkpoint,
        camera_client=camera_client,
        device=config.act_device,
        image_key_in_camera=config.act_image_camera_key,
    )

    # Upper-body indices used to slice ACT's 43-D prediction.  Matches the
    # `target_upper_body_pose` layout (waist + arms + hands when
    # `enable_waist=True`, or arms + hands otherwise) used during data
    # collection.
    upper_body_indices = robot_model.get_joint_group_indices("upper_body")
    lower_body_indices = robot_model.get_joint_group_indices("lower_body")

    # ── Right-arm lock (same as run_g1_control_loop.py) ─────────────────────
    # During data collection the right arm was frozen to an extended-down
    # pose (not teleoperated).  We apply the identical override here so the
    # deployment arm pose matches training.  Joint order for right_arm:
    #   [pitch, roll, yaw, elbow, wrist_roll, wrist_pitch, wrist_yaw]
    # With roll=0 the arm hangs straight at the side (URDF shoulder joint
    # frames have ±0.27931 rad offsets that cancel at roll=0); elbow=π/2
    # unfolds the forearm so the whole arm points downwards.
    _ra_indices = robot_model.get_joint_group_indices("right_arm")
    _RIGHT_ARM_DOWN = np.array(
        [0.0, 0.0, 0.0, np.pi / 2.0, 0.0, 0.0, 0.0], dtype=np.float64
    )
    print(
        f"[ACT] Right-arm lock ACTIVE: {_RIGHT_ARM_DOWN.tolist()} "
        f"(identical to teleop pipeline)"
    )

    # ── Keyboard dispatcher (E-stop + activation) ──────────────────────────
    keyboard_listener_pub = KeyboardListenerPublisher()
    keyboard_estop = KeyboardEStop()
    if config.keyboard_dispatcher_type == "raw":
        dispatcher = KeyboardDispatcher()
    elif config.keyboard_dispatcher_type == "ros":
        dispatcher = ROSKeyboardDispatcher()
    else:
        raise ValueError(
            f"Invalid keyboard dispatcher: {config.keyboard_dispatcher_type}"
        )
    dispatcher.register(env)
    dispatcher.register(wbc_policy)
    dispatcher.register(keyboard_listener_pub)
    dispatcher.register(keyboard_estop)
    dispatcher.start()

    # Auto-activate the gear_wbc lower-body balance policy unless the user
    # explicitly disables it. Equivalent to pressing ']' in the teleop loop.
    # Without this, the legs just hold default angles (no closed-loop balance).
    if config.act_activate_wbc:
        try:
            wbc_policy.activate_policy()
            print("[ACT] gear_wbc lower-body policy AUTO-ACTIVATED "
                  "(use_policy_action=True).", flush=True)
        except Exception as exc:
            print(f"[ACT] WARNING: auto-activation failed: {exc}. "
                  f"Press ']' manually to activate the balance policy.",
                  flush=True)
    else:
        print("[ACT] gear_wbc lower-body policy NOT activated "
              "(pass --act_activate_wbc True to enable). "
              "Press ']' in this pane to activate manually.", flush=True)

    rate = node.create_rate(config.control_frequency)

    # ── Startup bookkeeping ────────────────────────────────────────────────
    t_startup = time.monotonic()
    hold_until = t_startup + float(config.act_hold_pose_on_startup_s)
    first_frame_warned = False

    _debug_every_n_steps = int(config.control_frequency)
    _debug_step = 0

    print(
        f"[ACT] Inference loop @ {config.control_frequency} Hz. "
        f"Holding pose for {config.act_hold_pose_on_startup_s:.1f}s before "
        f"switching to policy actions. gear_wbc base policy ACTIVE."
    )

    try:
        while ros_manager.ok():
            t_start = time.monotonic()
            with telemetry.timer("total_loop"):
                with telemetry.timer("step_simulator"):
                    if env.sim and config.sim_sync_mode:
                        env.step_simulator()

                with telemetry.timer("observe"):
                    obs = env.observe()
                    wbc_policy.set_observation(obs)
                    act_policy.set_observation(obs)

                t_now = time.monotonic()

                # ── Build the WBC upper-body goal from ACT ─────────────────
                # During warm-up we skip ACT and let the upper-body
                # InterpolationPolicy keep its current waypoint (set from the
                # robot's observed pose at construction time).  After warm-up
                # we send ACT's 43-D prediction's upper-body slice as the
                # new goal.
                policy_active = t_now >= hold_until

                if policy_active:
                    with telemetry.timer("policy_action"):
                        act_output = act_policy.get_action(time=t_now)

                    act_q = act_output["q"]
                    upper_body_target = act_q[upper_body_indices]

                    wbc_goal = {
                        "target_upper_body_pose": upper_body_target,
                        "target_time": t_now + (1.0 / config.control_frequency),
                        "interpolation_garbage_collection_time":
                            t_now - 2 * (1.0 / config.control_frequency),
                        # Lock the base to its default height / still navigation
                        # unless you wire those up to ACT too.
                        "base_height_command": np.array([DEFAULT_BASE_HEIGHT]),
                        "navigate_cmd": np.array(DEFAULT_NAV_CMD),
                    }
                    with telemetry.timer("policy_setup"):
                        wbc_policy.set_goal(wbc_goal)

                    if not first_frame_warned and act_policy._latest_image is None:
                        print(
                            "[ACT] WARNING: No camera frame received yet — "
                            "ACT will hold its bootstrapped pose until frames "
                            "arrive. Check the camera server / GStreamer pipe."
                        )
                        first_frame_warned = True

                # ── Run the full WBC policy ────────────────────────────────
                # Produces 43-D q: ACT-driven upper body (smoothed via
                # InterpolationPolicy) + gear_wbc RL lower body.
                with telemetry.timer("wbc_policy_action"):
                    wbc_action = wbc_policy.get_action(time=t_now)

                # During training, `action.wbc` (our ACT target) was recorded
                # *after* the InterpolationPolicy ran.  So ACT already learned
                # to output the smoothed signal — routing it through Interp
                # again adds a second layer of lag.  To stay faithful to what
                # ACT was trained to produce, overwrite the upper-body slice
                # with ACT's raw prediction once the policy is active.
                # gear_wbc still owns the lower-body slice for real balance.
                if policy_active:
                    wbc_action["q"][upper_body_indices] = act_q[upper_body_indices]

                # Right arm is never teleoperated / learned — lock it to the
                # extended-down pose used during data collection.  Applied
                # AFTER the policy so it overrides whatever ACT predicted for
                # the right arm (ACT saw the fixed right-arm values in the
                # training data and *should* reproduce them, but we enforce
                # the exact teleop value to guarantee consistency).
                wbc_action["q"][_ra_indices] = _RIGHT_ARM_DOWN

                with telemetry.timer("queue_action"):
                    env.queue_action(wbc_action)

                # ── Export for logging / monitoring ────────────────────────
                msg = deepcopy(obs)
                for key in list(msg.keys()):
                    if key.endswith("_image"):
                        del msg[key]
                msg["action"] = wbc_action["q"]
                msg["timestamps"] = {
                    "main_loop": time.time(),
                    "proprio": time.time(),
                }
                data_exp_pub.publish(msg)

                # ── Periodic debug print ───────────────────────────────────
                if _debug_step % _debug_every_n_steps == 0:
                    def _fmt(arr, n=7):
                        return "[" + ", ".join(f"{v:+.3f}" for v in arr[:n]) + "]"
                    _la = robot_model.get_joint_group_indices("left_arm")
                    _ra = robot_model.get_joint_group_indices("right_arm")
                    _waist = robot_model.get_joint_group_indices("waist")
                    _ll = robot_model.get_joint_group_indices("left_leg")

                    # ACT raw prediction (full 43-D, pre-WBC)
                    if policy_active and act_policy._last_action is not None:
                        act_raw = act_policy._last_action
                        print(
                            f"[ACT-raw t={(t_now - t_startup):6.2f}s] "
                            f"L_arm={_fmt(act_raw[_la])}  "
                            f"R_arm={_fmt(act_raw[_ra])}  "
                            f"waist={_fmt(act_raw[_waist], n=3)}",
                            flush=True,
                        )
                        # Image feed sanity check — confirm image is fresh + in expected range.
                        img = act_policy._latest_image
                        if img is not None:
                            print(
                                f"[ACT-img]   shape={img.shape} dtype={img.dtype}  "
                                f"mean={img.mean():.1f} min={img.min()} max={img.max()}  "
                                f"(BGR if OpenCV, RGB if from lerobot dataset)",
                                flush=True,
                            )
                        else:
                            print("[ACT-img]   no frame received yet", flush=True)

                    # Final WBC action (what goes to motors)
                    print(
                        f"[WBC       ] "
                        f"{'POLICY' if policy_active else 'HOLD  '}  "
                        f"L_arm cmd={_fmt(wbc_action['q'][_la])}  "
                        f"R_arm cmd={_fmt(wbc_action['q'][_ra])}  "
                        f"L_leg cmd={_fmt(wbc_action['q'][_ll], n=6)}",
                        flush=True,
                    )
                    # Observed joint positions — compare to cmd to see if motors track.
                    if "q" in obs:
                        print(
                            f"[OBS       ] "
                            f"L_arm obs={_fmt(obs['q'][_la])}  "
                            f"R_arm obs={_fmt(obs['q'][_ra])}",
                            flush=True,
                        )
                _debug_step += 1

            if env.sim and (not env.sim.sim_thread or not env.sim.sim_thread.is_alive()):
                raise RuntimeError("Simulator thread is not alive")

            rate.sleep()

            if config.verbose_timing:
                telemetry.log_timing_info(context="ACT Loop", threshold=0.0)
            elif (time.monotonic() - t_start) > (1 / config.control_frequency) \
                    and not config.sim_sync_mode:
                telemetry.log_timing_info(
                    context="ACT Loop Missed", threshold=0.001
                )

    except ros_manager.exceptions() as e:
        print(f"ROSManager interrupted by user: {e}")
    finally:
        print("Cleaning up …")
        dispatcher.stop()
        try:
            camera_client.close()
        except Exception as exc:
            print(f"[ACT] camera_client.close() raised: {exc}")
        ros_manager.shutdown()
        env.close()


if __name__ == "__main__":
    cfg = tyro.cli(ActInferenceConfig)
    main(cfg)
