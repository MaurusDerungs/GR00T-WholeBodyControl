const state = {
  motions: [],
  jobs: [],
  playbacks: [],
  stationMode: "unknown",
  robotInterface: "",
  robotIp: "",
  robotReachable: null,
  targets: [],
  busyGenerate: false,
  busyPlay: new Set(),
  playCooldownUntil: new Map(),
  lastErrors: new Map(),
  cameraReady: false,
  cameraView: { azimuth: 135, elevation: -18, distance: 3, lookat: [0, 0, 0.75] },
  cameraDrag: null,
  cameraPostTimer: null,
  teleopActive: false,
  teleopSpeed: 0.4,
  teleopYaw: 0,
  teleopKeys: new Set(),
  teleopHelpShown: false,
};

const el = {
  healthText: document.querySelector("#healthText"),
  stationMode: document.querySelector("#stationMode"),
  motionCount: document.querySelector("#motionCount"),
  jobCount: document.querySelector("#jobCount"),
  playbackCount: document.querySelector("#playbackCount"),
  cameraStatus: document.querySelector("#cameraStatus"),
  cameraResetButton: document.querySelector("#cameraResetButton"),
  simFrame: document.querySelector("#simFrame"),
  simImage: document.querySelector("#simImage"),
  simPlaceholder: document.querySelector("#simPlaceholder"),
  teleopOverlay: document.querySelector("#teleopOverlay"),
  generateForm: document.querySelector("#generateForm"),
  promptInput: document.querySelector("#promptInput"),
  nameInput: document.querySelector("#nameInput"),
  durationInput: document.querySelector("#durationInput"),
  refreshButton: document.querySelector("#refreshButton"),
  emergencyStopButton: document.querySelector("#emergencyStopButton"),
  idleResetButton: document.querySelector("#idleResetButton"),
  motionRestartButton: document.querySelector("#motionRestartButton"),
  teleopToggleButton: document.querySelector("#teleopToggleButton"),
  teleopDialog: document.querySelector("#teleopDialog"),
  statusLog: document.querySelector("#statusLog"),
  generatedList: document.querySelector("#generatedList"),
  predefinedList: document.querySelector("#predefinedList"),
  jobsList: document.querySelector("#jobsList"),
  playbacksList: document.querySelector("#playbacksList"),
};

function log(message) {
  const stamp = new Date().toLocaleTimeString();
  el.statusLog.textContent = `[${stamp}] ${message}\n${el.statusLog.textContent}`.slice(0, 6000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.message || data.error || `Request failed: ${response.status}`);
  }
  return data;
}

async function tryApi(path, fallback = null) {
  try {
    return await api(path);
  } catch (error) {
    const previous = state.lastErrors.get(path);
    if (previous !== error.message) {
      state.lastErrors.set(path, error.message);
      log(`${path}: ${error.message}`);
    }
    return fallback;
  }
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function queueCameraViewPost() {
  clearTimeout(state.cameraPostTimer);
  state.cameraPostTimer = setTimeout(async () => {
    try {
      await api("/camera/view", {
        method: "POST",
        body: JSON.stringify(state.cameraView),
      });
    } catch (error) {
      log(`/camera/view: ${error.message}`);
    }
  }, 80);
}

function setCameraView(nextView, { post = true } = {}) {
  state.cameraView = {
    azimuth: ((Number(nextView.azimuth ?? state.cameraView.azimuth) % 360) + 360) % 360,
    elevation: clamp(Number(nextView.elevation ?? state.cameraView.elevation), -80, 20),
    distance: clamp(Number(nextView.distance ?? state.cameraView.distance), 1, 8),
    lookat: Array.isArray(nextView.lookat) ? nextView.lookat : state.cameraView.lookat,
  };
  if (post) queueCameraViewPost();
}

function formatDuration(motion) {
  if (motion.duration_sec == null) return "unknown";
  return `${motion.duration_sec.toFixed(1)}s`;
}

function formatMode(motion) {
  if (motion.source !== "predefined") return motion.source;
  return motion.name.endsWith("_M") ? "planner" : "normal";
}

function statusClass(status) {
  return `status-${status || "unknown"}`;
}

function targetAvailable(target) {
  if (!state.targets.includes(target)) return false;
  return target !== "robot" || state.robotReachable === true;
}

function robotControlsAvailable() {
  return state.stationMode !== "real" || targetAvailable("robot");
}

function renderMotionList(container, source) {
  const motions = state.motions.filter((motion) => motion.source === source);
  container.innerHTML = "";
  if (motions.length === 0) {
    container.innerHTML = `<div class="empty">No motions.</div>`;
    return;
  }

  for (const motion of motions) {
    const item = document.createElement("article");
    item.className = "motion-item";
    const simDisabled =
      !motion.valid ||
      state.busyPlay.has(motion.id) ||
      (state.playCooldownUntil.get(motion.id) || 0) > Date.now() ||
      !targetAvailable("sim");
    const robotDisabled =
      !motion.valid ||
      state.busyPlay.has(motion.id) ||
      (state.playCooldownUntil.get(motion.id) || 0) > Date.now() ||
      !targetAvailable("robot");
    const robotLabel = state.robotReachable === true ? "Play on Robot" : "Robot Offline";
    const actionsHtml =
      state.stationMode === "real"
        ? `
          <button class="primary" ${simDisabled ? "disabled" : ""} data-target="sim" data-motion-id="${motion.id}">
            Play in Sim
          </button>
          <button class="danger" ${robotDisabled ? "disabled" : ""} data-target="robot" data-motion-id="${motion.id}">
            ${robotLabel}
          </button>
        `
        : `
          <button class="primary" ${simDisabled ? "disabled" : ""} data-target="sim" data-motion-id="${motion.id}">
            Play in Sim
          </button>
        `;
    item.innerHTML = `
      <div class="motion-title">
        <strong>${motion.name}</strong>
        <span class="pill">${formatDuration(motion)}</span>
      </div>
      <div class="motion-meta">
        ${motion.id} · ${formatMode(motion)}<br />
        ${motion.timesteps ?? 0} frames · ${motion.path}
      </div>
      <div class="motion-actions">
        ${actionsHtml}
      </div>
    `;
    for (const button of item.querySelectorAll("button")) {
      button.addEventListener("click", () => playMotion(motion.id, button.dataset.target || "sim"));
    }
    container.appendChild(item);
  }
}

function renderActivityList(container, items, type) {
  container.innerHTML = "";
  if (items.length === 0) {
    container.innerHTML = `<div class="empty">No ${type} yet.</div>`;
    return;
  }

  for (const item of items.slice(0, 8)) {
    const title = type === "jobs" ? item.name : item.motion_name;
    const subtitle = type === "jobs" ? item.prompt : item.motion_id;
    const activity = document.createElement("article");
    activity.className = "activity-item";
    activity.innerHTML = `
      <div class="activity-title">
        <strong>${title}</strong>
        <span class="${statusClass(item.status)}">${item.status}</span>
      </div>
      <div class="activity-meta">
        ${subtitle || ""}<br />
        ${item.error ? item.error : item.updated_at_iso || ""}
      </div>
    `;
    container.appendChild(activity);
  }
}

function render() {
  el.motionCount.textContent = `${state.motions.length} motions`;
  el.jobCount.textContent = `${state.jobs.length} jobs`;
  el.playbackCount.textContent = `${state.playbacks.length} playbacks`;

  renderMotionList(el.generatedList, "generated");
  renderMotionList(el.predefinedList, "predefined");
  renderActivityList(el.jobsList, state.jobs, "jobs");
  renderActivityList(el.playbacksList, state.playbacks, "playbacks");
  el.teleopToggleButton.textContent = state.teleopActive
    ? `Teleop On · ${state.teleopSpeed.toFixed(1)} m/s`
    : "Teleop Off";
  el.teleopToggleButton.setAttribute("aria-pressed", String(state.teleopActive));
  document.body.classList.toggle("teleop-active", state.teleopActive);
  const controlsAvailable = robotControlsAvailable();
  document.body.classList.toggle("robot-offline", state.stationMode === "real" && !controlsAvailable);
  el.idleResetButton.disabled = !controlsAvailable;
  el.motionRestartButton.disabled = !controlsAvailable;
  el.teleopToggleButton.disabled = !controlsAvailable;
}

async function refresh({ quiet = false } = {}) {
  const [health, motions, jobs, playbacks, camera, cameraView] = await Promise.all([
    tryApi("/health"),
    tryApi("/motions"),
    tryApi("/jobs", { jobs: [] }),
    tryApi("/playbacks", { playbacks: [] }),
    tryApi("/camera/status"),
    tryApi("/camera/view"),
  ]);

  if (health) {
    state.stationMode = health.station_mode || "unknown";
    state.robotInterface = health.robot_interface || "";
    state.robotIp = health.robot_ip || "";
    state.robotReachable = health.robot_reachable;
    state.targets = health.targets || [];
    el.healthText.textContent = `${health.service} · ${health.repo_root}`;
    el.stationMode.textContent =
      state.stationMode === "real"
        ? `REAL ROBOT · ${state.robotInterface || "interface unknown"} · ${
            state.robotReachable === true ? "online" : "offline"
          }`
        : "SIMULATION";
    el.stationMode.className = state.stationMode === "real" ? "mode-real" : "mode-sim";
    document.body.classList.toggle("real-mode", state.stationMode === "real");
  } else {
    el.healthText.textContent = "Backend unavailable";
    el.stationMode.textContent = "mode unknown";
    el.stationMode.className = "";
    document.body.classList.remove("real-mode");
  }
  if (motions) {
    state.motions = motions.motions || [];
  }
  if (jobs) {
    state.jobs = jobs.jobs || [];
  }
  if (playbacks) {
    state.playbacks = playbacks.playbacks || [];
  }
  if (cameraView && cameraView.view) {
    setCameraView(cameraView.view, { post: false });
  }
  updateCameraStatus(camera ? camera.camera : null);
  render();
  if (!quiet) log("Refreshed station state.");
}

function onCameraPointerDown(event) {
  event.preventDefault();
  state.cameraDrag = {
    pointerId: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    azimuth: state.cameraView.azimuth,
    elevation: state.cameraView.elevation,
  };
  event.currentTarget.setPointerCapture(event.pointerId);
}

function onCameraPointerMove(event) {
  if (!state.cameraDrag || event.pointerId !== state.cameraDrag.pointerId) return;
  const dx = event.clientX - state.cameraDrag.x;
  const dy = event.clientY - state.cameraDrag.y;
  setCameraView({
    ...state.cameraView,
    azimuth: state.cameraDrag.azimuth - dx * 0.25,
    elevation: state.cameraDrag.elevation + dy * 0.18,
  });
}

function onCameraPointerUp(event) {
  if (!state.cameraDrag || event.pointerId !== state.cameraDrag.pointerId) return;
  state.cameraDrag = null;
}

function onCameraWheel(event) {
  event.preventDefault();
  setCameraView({
    ...state.cameraView,
    distance: state.cameraView.distance + Math.sign(event.deltaY) * 0.25,
  });
}

function resetCameraView() {
  setCameraView({ azimuth: 135, elevation: -18, distance: 3, lookat: [0, 0, 0.75] });
}

function updateCameraStatus(camera) {
  if (!camera || !camera.enabled) {
    state.cameraReady = false;
    el.cameraStatus.textContent = "disabled";
    el.cameraStatus.className = "pill status-failed";
    document.body.classList.remove("has-camera");
    return;
  }
  if (!camera.has_frame) {
    state.cameraReady = false;
    el.cameraStatus.textContent = "waiting";
    el.cameraStatus.className = "pill status-running";
    document.body.classList.remove("has-camera");
    return;
  }
  state.cameraReady = true;
  el.cameraStatus.textContent = `${camera.camera || "camera"} · ${camera.frames}f`;
  el.cameraStatus.className = "pill status-succeeded";
  document.body.classList.add("has-camera");
}

function refreshCameraImage() {
  if (!state.cameraReady) return;
  const url = `/camera/latest.jpg?t=${Date.now()}`;
  el.simImage.onload = () => document.body.classList.add("has-camera");
  el.simImage.onerror = () => {
    document.body.classList.remove("has-camera");
    el.cameraStatus.textContent = "waiting";
    el.cameraStatus.className = "pill status-running";
  };
  el.simImage.src = url;
}

async function generateMotion(event) {
  event.preventDefault();
  if (state.busyGenerate) return;

  const prompt = el.promptInput.value.trim();
  if (!prompt) {
    log("Prompt is required.");
    return;
  }

  state.busyGenerate = true;
  const button = el.generateForm.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const payload = {
      prompt,
      name: el.nameInput.value.trim(),
      duration: Number(el.durationInput.value || 6),
    };
    const result = await api("/generate", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    log(`Generation queued: ${result.job.name}`);
    await refresh({ quiet: true });
  } catch (error) {
    log(error.message);
  } finally {
    state.busyGenerate = false;
    button.disabled = false;
  }
}

async function playMotion(motionId, target = state.stationMode === "real" ? "robot" : "sim") {
  if (state.busyPlay.has(motionId) || (state.playCooldownUntil.get(motionId) || 0) > Date.now()) return;
  if (!targetAvailable(target)) {
    log(
      target === "robot"
        ? `Robot offline: cannot play on ${state.robotIp || state.robotInterface || "robot"}.`
        : "Simulation target unavailable.",
    );
    return;
  }

  state.busyPlay.add(motionId);
  state.playCooldownUntil.set(motionId, Date.now() + 3000);
  render();
  try {
    const result = await api("/motion/play", {
      method: "POST",
      body: JSON.stringify({ motion_id: motionId, startup_delay: 1.0, target }),
    });
    log(`${target === "robot" ? "Robot" : "Sim"} playback queued: ${result.playback.motion_name}`);
    await refresh({ quiet: true });
  } catch (error) {
    log(error.message);
  } finally {
    state.busyPlay.delete(motionId);
    render();
    setTimeout(render, 3000);
  }
}

async function sendReset(action, label) {
  if (!robotControlsAvailable()) {
    log(`Robot offline: cannot send ${label}.`);
    return;
  }
  const buttons = [el.emergencyStopButton, el.idleResetButton, el.motionRestartButton];
  buttons.forEach((button) => {
    button.disabled = true;
  });
  try {
    await api("/control/reset", {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    log(`${label} sent.`);
  } catch (error) {
    log(error.message);
  } finally {
    buttons.forEach((button) => {
      button.disabled = false;
    });
    render();
  }
}

function unitFromYaw(yaw) {
  return [Math.cos(yaw), Math.sin(yaw), 0];
}

function teleopPayload() {
  const forward = unitFromYaw(state.teleopYaw);
  let movement = [0, 0, 0];
  let moving = false;
  if (state.teleopKeys.has("w")) {
    movement = forward;
    moving = true;
  } else if (state.teleopKeys.has("s")) {
    movement = [-forward[0], -forward[1], 0];
    moving = true;
  } else if (state.teleopKeys.has(",")) {
    movement = [-Math.sin(state.teleopYaw), Math.cos(state.teleopYaw), 0];
    moving = true;
  } else if (state.teleopKeys.has(".")) {
    movement = [Math.sin(state.teleopYaw), -Math.cos(state.teleopYaw), 0];
    moving = true;
  }
  return {
    active: state.teleopActive,
    movement,
    facing: forward,
    speed: state.teleopActive && moving ? state.teleopSpeed : -1,
    height: -1,
  };
}

async function sendTeleop() {
  if (!state.teleopActive) return;
  if (state.teleopKeys.has("q") || state.teleopKeys.has("a")) state.teleopYaw += 0.08;
  if (state.teleopKeys.has("d")) state.teleopYaw -= 0.08;
  try {
    await api("/teleop", {
      method: "POST",
      body: JSON.stringify(teleopPayload()),
    });
  } catch (error) {
    log(`/teleop: ${error.message}`);
  }
}

async function setTeleopActive(active) {
  state.teleopActive = active;
  state.teleopKeys.clear();
  render();
  if (active) {
    el.simFrame.focus();
    if (!state.teleopHelpShown) {
      state.teleopHelpShown = true;
      el.teleopDialog.showModal();
    }
    log("Teleop enabled. Click the MuJoCo view and use W/S/Q/D.");
  } else {
    try {
      await api("/teleop", {
        method: "POST",
        body: JSON.stringify({ active: false }),
      });
    } catch (error) {
      log(`/teleop: ${error.message}`);
    }
    log("Teleop disabled.");
  }
}

function onTeleopKeyDown(event) {
  if (!state.teleopActive) return;
  if (event.target !== el.simFrame && !el.simFrame.contains(event.target)) return;
  const key = event.key.toLowerCase();
  if (["w", "s", "q", "a", "d", ",", ".", " ", "9", "0", "escape"].includes(key)) {
    event.preventDefault();
  }
  if (key === "escape") {
    setTeleopActive(false);
    return;
  }
  if (key === "9" && !event.repeat) {
    state.teleopSpeed = clamp(state.teleopSpeed - 0.1, 0.2, 0.8);
    render();
    log(`Teleop speed: ${state.teleopSpeed.toFixed(1)} m/s`);
    return;
  }
  if (key === "0" && !event.repeat) {
    state.teleopSpeed = clamp(state.teleopSpeed + 0.1, 0.2, 0.8);
    render();
    log(`Teleop speed: ${state.teleopSpeed.toFixed(1)} m/s`);
    return;
  }
  if (key === " ") {
    state.teleopKeys.clear();
    return;
  }
  state.teleopKeys.add(key);
}

function onTeleopKeyUp(event) {
  if (!state.teleopActive) return;
  state.teleopKeys.delete(event.key.toLowerCase());
}

el.generateForm.addEventListener("submit", generateMotion);
el.refreshButton.addEventListener("click", () => refresh());
el.emergencyStopButton.addEventListener("click", () => sendReset("emergency_stop", "Emergency stop"));
el.idleResetButton.addEventListener("click", () => sendReset("idle_reset", "IDLE reset"));
el.motionRestartButton.addEventListener("click", () => sendReset("motion_restart", "Motion restart"));
el.teleopToggleButton.addEventListener("click", () => setTeleopActive(!state.teleopActive));
el.teleopDialog.addEventListener("close", () => {
  if (state.teleopActive) el.simFrame.focus();
});
el.cameraResetButton.addEventListener("click", resetCameraView);
el.simFrame.addEventListener("pointerdown", (event) => {
  el.simFrame.focus();
  onCameraPointerDown(event);
});
el.simFrame.addEventListener("pointermove", onCameraPointerMove);
el.simFrame.addEventListener("pointerup", onCameraPointerUp);
el.simFrame.addEventListener("pointercancel", onCameraPointerUp);
el.simFrame.addEventListener("wheel", onCameraWheel, { passive: false });
el.simFrame.addEventListener("keydown", onTeleopKeyDown);
el.simFrame.addEventListener("keyup", onTeleopKeyUp);

refresh({ quiet: true });
refreshCameraImage();
setInterval(() => refresh({ quiet: true }), 2500);
setInterval(refreshCameraImage, 33);
setInterval(sendTeleop, 50);
