# Meta Quest 3S Teleoperation Guide

This guide wires a Meta Quest 3S into the existing GR00T teleoperation stack without depending on the PICO SDK path. The integration added in this repo has three pieces:

1. A local HTTPS WebXR bridge page that runs in the Quest browser.
2. A Python `QuestStreamer` that reads the bridge state and emits GR00T teleop commands.
3. The existing WBC control + teleop loops, unchanged in structure.

The high-level data path is:

`Quest 3S -> WebXR bridge page -> HTTPS bridge server -> QuestStreamer -> TeleopPolicy -> WBC control loop -> MuJoCo sim or real G1`

## What Was Added

- Bridge server: [run_quest_bridge.py](/home/maurus/Documents/GR00T-WholeBodyControl/decoupled_wbc/control/teleop/main/run_quest_bridge.py)
- Bridge implementation: [quest_bridge.py](/home/maurus/Documents/GR00T-WholeBodyControl/decoupled_wbc/control/teleop/device/quest/quest_bridge.py)
- Headset browser page: [index.html](/home/maurus/Documents/GR00T-WholeBodyControl/decoupled_wbc/control/teleop/device/quest/web/index.html)
- GR00T device streamer: [quest_streamer.py](/home/maurus/Documents/GR00T-WholeBodyControl/decoupled_wbc/control/teleop/streamers/quest_streamer.py)

## Controls

The Quest bridge defaults to these mappings:

- Left thumbstick: translate base `[x, y]`
- Right thumbstick X: yaw
- Left trigger / grip: left finger closure heuristic
- Right trigger / grip: right finger closure heuristic
- Left primary / secondary buttons: base height down / up
- Left thumbstick click + left trigger: toggle lower-body policy action
- Right thumbstick click + right trigger: toggle teleop activation
- Right primary button: toggle data collection
- Right secondary button: abort data collection

You can always activate teleop from the PC keyboard with `l`, which is the safest way to calibrate the initial pose.

## 1. Install The Teleop Environment

From the repo root:

```bash
bash install_scripts/install_pico.sh
source .venv_teleop/bin/activate
```

The Quest bridge itself uses Python standard-library networking, so there are no extra bridge-specific packages beyond the repo code.

If the machine that will run the WBC control loop and teleop policy does not already have `decoupled_wbc` installed in that venv, install it there as well:

```bash
uv pip install -e "decoupled_wbc[full]"
```

That extra editable install is what makes commands like `run_g1_control_loop.py` and `run_teleop_policy_loop.py` available in the teleop environment.

## 2. Find The LAN IP Of The Machine Hosting The Bridge

On the machine that will run the Quest bridge:

```bash
hostname -I
```

Use the LAN IP address on the same Wi-Fi or LAN as the Quest, for example `192.168.1.42`.

## 3. Start The Quest Bridge

From the repo root on the bridge host:

```bash
source .venv_teleop/bin/activate
python decoupled_wbc/control/teleop/main/run_quest_bridge.py \
  --public-host 192.168.1.42 \
  --port 8765
```

Notes:

- The bridge auto-generates a self-signed certificate under `.quest_bridge/`.
- In the Quest browser you will likely need to accept the certificate warning once.
- The page URL will be:

```text
https://192.168.1.42:8765/
```

## 4. Open The Bridge Page In The Quest

In the Meta Quest browser:

1. Open the URL printed by the bridge server.
2. Accept the certificate warning if shown.
3. Press `Start VR Session`.
4. Keep the controllers in a neutral pose while you activate teleop from GR00T.

## 5. Run In MuJoCo Simulation

Open two more terminals on the same machine.

### Terminal A: Control Loop

```bash
source .venv_teleop/bin/activate
python decoupled_wbc/control/main/teleop/run_g1_control_loop.py \
  --interface sim \
  --enable_onscreen \
  --no-enable_waist
```

This starts the MuJoCo-backed control loop and opens the simulator viewer.

### Terminal B: Teleop Policy With Quest Input

```bash
source .venv_teleop/bin/activate
python decoupled_wbc/control/main/teleop/run_teleop_policy_loop.py \
  --body_control_device quest \
  --hand_control_device quest \
  --quest_bridge_host 127.0.0.1 \
  --quest_bridge_port 8765 \
  --enable_real_device \
  --no-enable_visualization
```

### Start Teleoperation

In the control-loop terminal:

- Press `i` to move the robot to its initial pose if needed.
- Hold the Quest controllers in a neutral pose.
- Press `l` to activate the teleop policy and calibrate to your current hand positions.
- Use the thumbsticks to move, and the controllers to drive the upper body.

Recommended simulation safety habits:

- Start with `--no-enable_waist`.
- Activate teleop only while standing comfortably in a neutral pose.
- Stop the VR session or release the controls before making large posture changes.

## 6. Run On The Real G1

The safest topology is:

- Laptop or desktop: runs the Quest HTTPS bridge and serves the WebXR page to the headset.
- Robot or onboard compute: runs the GR00T control loop and teleop policy.

Assume:

- Bridge host IP: `192.168.1.42`
- Robot control interface: replace `enx...` with your actual robot NIC if needed

### Bridge Host

```bash
source .venv_teleop/bin/activate
python decoupled_wbc/control/teleop/main/run_quest_bridge.py \
  --public-host 192.168.1.42 \
  --port 8765
```

### Robot Terminal A: Control Loop

```bash
source .venv_teleop/bin/activate
python decoupled_wbc/control/main/teleop/run_g1_control_loop.py \
  --interface real
```

If your setup requires a specific interface name instead of the `real` shortcut, use that explicit interface:

```bash
python decoupled_wbc/control/main/teleop/run_g1_control_loop.py --interface enxe8ea6a9c4e09
```

### Robot Terminal B: Teleop Policy

```bash
source .venv_teleop/bin/activate
python decoupled_wbc/control/main/teleop/run_teleop_policy_loop.py \
  --body_control_device quest \
  --hand_control_device quest \
  --quest_bridge_host 192.168.1.42 \
  --quest_bridge_port 8765 \
  --enable_real_device \
  --no-enable_visualization
```

### Bring The Robot Up Carefully

1. Make sure the Quest page is already streaming.
2. Put the robot into its initial safe pose.
3. Hold both controllers in a neutral pose.
4. Activate teleop from the GR00T side with `l`.
5. Only then enable locomotion or policy-action mode.

Recommended real-robot precautions:

- Begin with the robot safely supported or closely spotted.
- Keep translational stick inputs small until you confirm frame alignment.
- Test only upper-body motion first before adding locomotion.
- Leave `enable_waist` off for the first session.
- Verify that stopping the Quest stream causes the teleop commands to go stale and the robot to settle safely.

## 7. Troubleshooting

### The Quest page does not enter VR

- Confirm you opened the HTTPS URL, not HTTP.
- Accept the self-signed certificate warning in the Quest browser.
- Make sure the browser supports immersive WebXR on your current Quest OS build.

### The teleop loop is not receiving Quest data

- Check the bridge health endpoint on the host:

```text
https://<bridge-host>:8765/api/health
```

- Confirm the Quest browser page is open and `VR session live` is shown.
- Confirm the teleop loop uses the same `quest_bridge_host` and `quest_bridge_port`.

### The robot pose feels rotated or offset

- Re-enter teleop calibration by holding a neutral pose and pressing `l` again.
- Keep the headset facing roughly toward the robot during calibration.
- Start with upper-body-only teleop before engaging locomotion.

## 8. SONIC Follow-Up

This implementation is aimed first at the WBC teleop path because that is the shortest route to a working Quest bridge. Once you are happy with Quest -> WBC, the same upstream bridge can be reused for SONIC by feeding the teleop command stream into the SONIC deploy path that already converts wrist poses into the `vr_3point_*` features used by the policy.
