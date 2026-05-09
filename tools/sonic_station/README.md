# SONIC Station

Local tools for the prompt-to-robot pipeline:

```text
prompt -> Kimodo G1 qpos CSV -> SONIC reference motion -> MuJoCo / G1 deploy
```

## 1. Generate a Kimodo Motion

Kimodo can run from an isolated local environment so it does not pollute the
`g1_deploy` environment.

```bash
tools/sonic_station/install_kimodo_env.sh
hf auth login
```

If you prefer to manage the environment yourself, install Kimodo in any active
Python 3.10 environment:

```bash
pip install "kimodo[all] @ git+https://github.com/nv-tlabs/kimodo.git"
```

Generate and convert a G1 motion:

```bash
tools/sonic_station/generate_kimodo_reference.sh \
  "A Unitree G1 robot does a smooth disco dance with arm waves." \
  disco_dance
```

This creates:

```text
kimodo_outputs/disco_dance.csv
gear_sonic_deploy/reference/kimodo/disco_dance/
```

## 2. Convert an Existing Kimodo CSV

If you already have a Kimodo-G1 qpos CSV:

```bash
python tools/sonic_station/kimodo_qpos_to_reference.py path/to/motion.csv \
  --output-root gear_sonic_deploy/reference/kimodo \
  --name my_motion \
  --input-fps 30 \
  --output-fps 50 \
  --force
```

The converter writes the minimal reference files required by SONIC:

- `joint_pos.csv` in IsaacLab joint order
- `joint_vel.csv`
- `body_pos.csv` root-only
- `body_quat.csv` root-only
- `body_lin_vel.csv`
- `body_ang_vel.csv`
- `metadata.txt`
- `info.txt`

## 3. Preview / Deploy

Start the station with MuJoCo idle, controller active, and no motion playback:

```bash
tools/sonic_station/launch_station.sh
```

This creates a local `reference/station_idle` dataset from one stable standing
frame, repeats it, and zeroes velocities so the controller has a static target.
It is generated locally and ignored by git.

The station runtime uses `--input-type zmq_manager`. A bootstrap ZMQ publisher
starts control and streams the static idle motion so later playback can happen
over ZMQ without restarting the deploy runtime.

It also enables MuJoCo offscreen image publishing on port `5555`, which the
backend exposes to the UI as `/camera/latest.jpg`.

Once a motion exists under `gear_sonic_deploy/reference/kimodo`, point the deploy
stack at that dataset:

```bash
cd gear_sonic_deploy
./deploy.sh sim --motion-data reference/kimodo --auto-motion-loop --auto-motion-playback-start-index 0
```

For the current dance-loop script, replace the reference folder with
`reference/kimodo` once you want generated motions instead of the curated
`reference/dance_loop` folder.

## Notes

- Kimodo-G1 qpos is MuJoCo order: root xyz, root quaternion `wxyz`, then 29 joints.
- SONIC reference motions use IsaacLab joint order. The converter applies the same
  `mujoco_to_isaaclab` mapping used by the C++ planner path.
- The output reference files are 50 Hz by default. Kimodo input CSV is assumed to
  be 30 Hz unless `--input-fps` is changed.
- Keep generated motions in simulation first. Only send to the real robot after
  visual inspection and stability checks.

## 4. Local Backend

Start the operator-station backend:

```bash
python tools/sonic_station/server.py
```

Open the browser UI:

```text
http://127.0.0.1:8765/
```

The UI shows the latest MuJoCo camera frame when `tools/sonic_station/launch_station.sh`
is running.

Useful endpoints:

```text
GET  /health
GET  /motions
GET  /jobs
GET  /jobs/<id>
POST /generate
POST /motion/play
GET  /playbacks
GET  /playbacks/<id>
GET  /camera/status
GET  /camera/latest.jpg
```

Generate a motion without starting playback:

```bash
curl -X POST http://127.0.0.1:8765/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"A Unitree G1 robot does a smooth disco dance with arm waves.","name":"disco_dance","duration":6}'
```

Stream an existing motion into the running station:

```bash
curl -X POST http://127.0.0.1:8765/motion/play \
  -H 'Content-Type: application/json' \
  -d '{"motion_id":"generated:disco_dance"}'
```

Or from CLI:

```bash
python tools/sonic_station/stream_motion.py disco_dance
```
