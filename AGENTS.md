# AGENTS.md

## Project: Sonic Station

Goal: build a local operator station for Unitree G1 where a user can keep the
robot standing idle in MuJoCo, enter a text prompt, generate a Kimodo motion,
preview/play it in simulation, and later send approved motions to the real robot.

This project must stay human-in-the-loop. Do not make the real robot execute a
newly generated motion without an explicit human approval step.

## Current Building Blocks

- `gear_sonic/scripts/run_sim_loop.py`
  - Starts the MuJoCo simulation.
  - Supports automatic elastic-band disable after low commands arrive.

- `gear_sonic_deploy/deploy.sh`
  - Starts the G1 deploy runtime.
  - Supports auto motion loop flags added for the dance-loop workflow.

- `launch_dance_loop.sh`
  - Existing scripted demo for looping predefined dance references.

- `tools/sonic_station/generate_kimodo_reference.sh`
  - Generates a Kimodo G1 motion from a prompt.
  - Converts it to a SONIC reference folder.

- `tools/sonic_station/kimodo_qpos_to_reference.py`
  - Converts Kimodo qpos CSV `[T, 36]` into SONIC reference files.

- `launch_kimodo_motion.sh`
  - Launches MuJoCo + G1 deploy against `reference/kimodo`.

## Target User Flow

```text
1. User starts Sonic Station.
2. MuJoCo opens with the G1 standing still on the ground.
3. User types a prompt in the UI.
4. User clicks Generate.
5. Kimodo generates a G1 motion.
6. Backend converts the motion to SONIC format.
7. User previews/plays the generated motion in MuJoCo.
8. User can choose predefined motions or generated motions.
9. Later, user explicitly approves sending a selected motion to the real robot.
```

## Architecture

```text
UI Web
  -> Sonic Station backend
      -> Kimodo generation worker
      -> qpos-to-reference converter
      -> motion library manager
      -> MuJoCo / deploy process manager
      -> optional ZMQ streaming publisher
  -> MuJoCo sim / GEAR-SONIC runtime
  -> real Unitree G1 only after approval
```

## Safety Rules

1. Real robot execution must require a clear, explicit human confirmation.
2. Generated motions must be previewed in simulation before real robot execution.
3. Never auto-send a prompt-generated motion directly to the real robot.
4. Keep stop/kill controls visible in the UI and documented in scripts.
5. Keep default behavior simulation-only.
6. Preserve existing deploy behavior unless the task explicitly changes it.
7. Avoid committing local model caches, virtual environments, generated Kimodo
   outputs, or generated reference motions unless intentionally requested.

## Development Plan

Current status:
- Step 1 is implemented with `tools/sonic_station/motion_registry.py`.
- Step 2 is implemented with `tools/sonic_station/server.py`.
- Step 3 is implemented for background Kimodo generation jobs through
  `POST /generate`, `GET /jobs`, and `GET /jobs/<id>`.
- Step 4 has a first launcher at `tools/sonic_station/launch_station.sh`.
- Step 5 has a first ZMQ streaming path through `tools/sonic_station/stream_motion.py`
  and `POST /motion/play`.
- Step 6 has a minimal browser UI under `tools/sonic_station/web/`, served at `/`.

### Step 0: Baseline Health Check

Objective: confirm the existing local stack still works.

Tasks:
- Run `bash -n` on shell scripts touched by the current change.
- Run `python -m py_compile` on Python scripts touched by the current change.
- Run `just build` from `gear_sonic_deploy` after C++ changes.
- Verify `./launch_dance_loop.sh` still starts and loops predefined motions.

Human Checkpoint:
- Ask the user to confirm the current dance loop works in MuJoCo.
- Do not proceed to Sonic Station runtime changes until confirmed.

### Step 1: Motion Library Model

Objective: create a clean local representation of available motions.

Tasks:
- Implement a small backend-side motion registry:
  - predefined motions from `gear_sonic_deploy/reference/example`
  - curated dance loop motions from `reference/dance_loop`
  - generated motions from `reference/kimodo`
- Each motion should expose:
  - id
  - display name
  - source type: `predefined`, `curated`, `generated`
  - path
  - duration/timesteps if available
  - created timestamp if generated
- Do not duplicate large CSV data unless needed.

Human Checkpoint:
- Show the user the discovered motion list.
- Ask whether naming/grouping feels right before building UI around it.

### Step 2: Backend Skeleton

Objective: provide a local API that can be called by a UI.

Tasks:
- Add `tools/sonic_station/server.py`.
- Use FastAPI if available, otherwise add a small dependency note.
- Endpoints:
  - `GET /health`
  - `GET /motions`
  - `POST /generate`
  - `POST /sim/start`
  - `POST /sim/stop`
  - `POST /motion/play`
- Keep all commands simulation-only by default.
- Stream process logs to files under a local ignored directory.

Human Checkpoint:
- Start the backend.
- Confirm with the user that `GET /motions` and `GET /health` return sane data.

### Step 3: Kimodo Generate Endpoint

Objective: make prompt generation callable from the backend.

Tasks:
- Make `/generate` call `generate_kimodo_reference.sh`.
- Return structured status:
  - `queued`
  - `running`
  - `succeeded`
  - `failed`
- Store generation logs.
- Do not block the backend event loop for long-running generation.
- Reuse existing `.venv_kimodo` if present.
- Surface common errors clearly:
  - missing `kimodo_gen`
  - missing Hugging Face auth
  - gated repo access
  - CUDA out of memory

Human Checkpoint:
- User enters one prompt.
- Confirm generated `reference/kimodo/<name>` appears.
- Confirm no real robot command is triggered.

### Step 4: Idle Simulation Runtime

Objective: start MuJoCo with the robot standing still and ready.

Tasks:
- Create `tools/sonic_station/launch_station.sh`.
- Start MuJoCo sim.
- Start deploy runtime against a neutral/idle reference motion.
- Automate the known initialization sequence:
  - controller start equivalent of `]`
  - MuJoCo elastic-band disable equivalent of `9`
  - wait for stable low motion
- Keep robot idle until user selects a motion.

Human Checkpoint:
- User confirms:
  - MuJoCo opens
  - robot lands safely
  - robot stands still
  - no dance starts automatically

### Step 5: Play Generated Motion

Objective: play a selected generated motion after the sim is already stable.

Preferred path:
- Use ZMQ streaming protocol v1 if it can play motions without restarting the
  runtime.

Fallback path:
- Restart deploy against selected `reference/kimodo` motion using
  `launch_kimodo_motion.sh`.

Tasks:
- Implement `tools/sonic_station/stream_motion.py` if using ZMQ:
  - read `joint_pos.csv`, `joint_vel.csv`, `body_quat.csv`
  - publish protocol v1 messages on topic `pose`
  - frame indices monotonic
  - preserve IsaacLab joint order
- Add `/motion/play` backend endpoint.
- Keep an idle/neutral mode to return to after playback.

Human Checkpoint:
- User selects `disco_dance`.
- Confirm robot plays in MuJoCo.
- Confirm robot returns to idle or holds safely after completion.

### Step 6: Minimal UI

Objective: build a usable local browser interface.

Tasks:
- Add a simple UI under `tools/sonic_station/web/`.
- Required controls:
  - prompt input
  - Generate button
  - generated motions list
  - predefined motions list
  - Play in Sim button
  - Stop Sim button
  - log/status panel
- UI must default to simulation controls only.
- Avoid decorative landing pages; show the operator console immediately.

Human Checkpoint:
- User can generate and play one motion from UI.
- User confirms labels and workflow are understandable.

### Step 7: MuJoCo Visualization in UI

Objective: show simulation state inside or next to the UI.

Options:
- Short term: display status/logs and keep native MuJoCo window.
- Medium term: stream images from MuJoCo offscreen camera to UI.
- Long term: integrate a browser viewer if the SONIC demo source becomes
  available.

Tasks:
- Prefer a robust short-term status panel first.
- Add camera streaming only after core generate/play works.

Human Checkpoint:
- User chooses whether native MuJoCo window is enough for V1.

### Step 8: Predefined Animations

Objective: allow clicking known-safe motions.

Tasks:
- Expose curated predefined motions:
  - dance loop motions
  - neutral/idle
  - selected examples
- Add UI buttons to play each in sim.
- Keep generated and predefined sections separate.

Human Checkpoint:
- User confirms the default curated list.

### Step 9: Real Robot Mode

Objective: add a real robot path only after simulation workflow is stable.

Tasks:
- Add explicit backend state `real_robot_enabled=false` by default.
- Require network/interface checks before real launch.
- Require a human confirmation phrase or UI modal before real execution.
- Require a selected motion to have been previewed in sim in the current session.
- Do not auto-loop generated motions on real robot unless explicitly requested.

Human Checkpoint:
- User physically confirms robot area is clear.
- User confirms Ethernet/interface setup.
- User confirms the exact selected motion.

### Step 10: Commit Discipline

Objective: keep changes reviewable.

Rules:
- Commit focused slices:
  - dance loop
  - Kimodo converter
  - backend skeleton
  - UI
  - ZMQ streaming
  - real robot controls
- Do not commit:
  - `.venv_kimodo/`
  - `kimodo_outputs/`
  - generated `reference/kimodo/` motions by default
  - Hugging Face caches
- Before commit:
  - `git diff --cached --stat`
  - `bash -n` changed shell scripts
  - `python -m py_compile` changed Python scripts
  - `just build` for C++ changes

## Open Decisions

- Should V1 use runtime restart for motion playback or ZMQ streaming?
- Should generated motions auto-return to idle after completion?
- Should generated motions be saved permanently or session-only by default?
- Should UI use native MuJoCo window for V1 or embedded camera stream?
- What exact safety gate is required before enabling real robot execution?

## Recommended Next Task

Human review for Steps 1-6, then implement Step 7:

1. Start the backend.
2. Confirm `/health`, `/motions`, `/jobs`, and `/generate` behavior.
3. Confirm the three motion categories: `curated`, `generated`, `predefined`.
4. Run `tools/sonic_station/launch_station.sh`.
5. Confirm MuJoCo starts with the robot standing idle and no dancing playback.
6. Use `POST /motion/play` to stream one generated or curated motion.
7. Open `http://127.0.0.1:8765/` and confirm generate/play controls are clear.
8. Decide whether the native MuJoCo window is enough for V1 or whether to add
   image streaming into the UI.
