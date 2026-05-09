const state = {
  motions: [],
  jobs: [],
  playbacks: [],
  stationMode: "unknown",
  robotInterface: "",
  busyGenerate: false,
  busyPlay: new Set(),
  playCooldownUntil: new Map(),
  lastErrors: new Map(),
  cameraReady: false,
  cameraView: { azimuth: 135, elevation: -18, distance: 3, lookat: [0, 0, 0.75] },
  cameraDrag: null,
  cameraPostTimer: null,
};

const el = {
  healthText: document.querySelector("#healthText"),
  stationMode: document.querySelector("#stationMode"),
  motionCount: document.querySelector("#motionCount"),
  jobCount: document.querySelector("#jobCount"),
  playbackCount: document.querySelector("#playbackCount"),
  cameraStatus: document.querySelector("#cameraStatus"),
  cameraResetButton: document.querySelector("#cameraResetButton"),
  simImage: document.querySelector("#simImage"),
  simPlaceholder: document.querySelector("#simPlaceholder"),
  generateForm: document.querySelector("#generateForm"),
  promptInput: document.querySelector("#promptInput"),
  nameInput: document.querySelector("#nameInput"),
  durationInput: document.querySelector("#durationInput"),
  refreshButton: document.querySelector("#refreshButton"),
  emergencyStopButton: document.querySelector("#emergencyStopButton"),
  idleResetButton: document.querySelector("#idleResetButton"),
  motionRestartButton: document.querySelector("#motionRestartButton"),
  statusLog: document.querySelector("#statusLog"),
  generatedList: document.querySelector("#generatedList"),
  curatedList: document.querySelector("#curatedList"),
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

function statusClass(status) {
  return `status-${status || "unknown"}`;
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
    const disabled =
      !motion.valid ||
      state.busyPlay.has(motion.id) ||
      (state.playCooldownUntil.get(motion.id) || 0) > Date.now();
    item.innerHTML = `
      <div class="motion-title">
        <strong>${motion.name}</strong>
        <span class="pill">${formatDuration(motion)}</span>
      </div>
      <div class="motion-meta">
        ${motion.id}<br />
        ${motion.timesteps ?? 0} frames · ${motion.path}
      </div>
      <div class="motion-actions">
        <button class="${state.stationMode === "real" ? "danger" : "primary"}" ${disabled ? "disabled" : ""} data-motion-id="${motion.id}">
          ${state.stationMode === "real" ? "Play on Robot" : "Play in Sim"}
        </button>
      </div>
    `;
    item.querySelector("button").addEventListener("click", () => playMotion(motion.id));
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
  renderMotionList(el.curatedList, "curated");
  renderMotionList(el.predefinedList, "predefined");
  renderActivityList(el.jobsList, state.jobs, "jobs");
  renderActivityList(el.playbacksList, state.playbacks, "playbacks");
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
    el.healthText.textContent = `${health.service} · ${health.repo_root}`;
    el.stationMode.textContent =
      state.stationMode === "real"
        ? `REAL ROBOT · ${state.robotInterface || "interface unknown"}`
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

async function playMotion(motionId) {
  if (state.busyPlay.has(motionId) || (state.playCooldownUntil.get(motionId) || 0) > Date.now()) return;

  state.busyPlay.add(motionId);
  state.playCooldownUntil.set(motionId, Date.now() + 3000);
  render();
  try {
    const result = await api("/motion/play", {
      method: "POST",
      body: JSON.stringify({ motion_id: motionId, startup_delay: 1.0 }),
    });
    log(`${state.stationMode === "real" ? "Robot" : "Sim"} playback queued: ${result.playback.motion_name}`);
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
  }
}

el.generateForm.addEventListener("submit", generateMotion);
el.refreshButton.addEventListener("click", () => refresh());
el.emergencyStopButton.addEventListener("click", () => sendReset("emergency_stop", "Emergency stop"));
el.idleResetButton.addEventListener("click", () => sendReset("idle_reset", "IDLE reset"));
el.motionRestartButton.addEventListener("click", () => sendReset("motion_restart", "Motion restart"));
el.cameraResetButton.addEventListener("click", resetCameraView);
el.simImage.parentElement.addEventListener("pointerdown", onCameraPointerDown);
el.simImage.parentElement.addEventListener("pointermove", onCameraPointerMove);
el.simImage.parentElement.addEventListener("pointerup", onCameraPointerUp);
el.simImage.parentElement.addEventListener("pointercancel", onCameraPointerUp);
el.simImage.parentElement.addEventListener("wheel", onCameraWheel, { passive: false });

refresh({ quiet: true });
refreshCameraImage();
setInterval(() => refresh({ quiet: true }), 2500);
setInterval(refreshCameraImage, 33);
