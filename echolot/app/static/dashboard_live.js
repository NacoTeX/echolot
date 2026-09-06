// Direct-telemetry enhancement layered over the stable HA-backed dashboard.
// Keeping this separate avoids making the dashboard unusable when direct data
// is absent and keeps feature development out of the core dashboard module.

const liveSources = new Map();
let liveGeneration = 0;

function applyLiveSample(id, sample) {
  const tile = document.querySelector(`[data-dash-id="${id}"]`);
  if (!tile) return;
  const state = trace(id);
  state.live = true;
  if (sample.movement_score != null) {
    state.points.push({ t: sample.t * 1000, v: sample.movement_score });
    if (state.points.length > MAX_POINTS) state.points.splice(0, state.points.length - MAX_POINTS);
    const score = tile.querySelector("[data-score]");
    if (score) score.textContent = sample.movement_score.toFixed(2);
  }
  if (sample.threshold != null) {
    state.threshold = sample.threshold;
    const threshold = tile.querySelector("[data-threshold]");
    if (threshold) threshold.textContent = sample.threshold.toFixed(2);
  }
  const note = tile.querySelector("[data-note]");
  if (note) {
    note.textContent = "Direkt verbunden · Live-Telemetrie";
    note.hidden = false;
  }
  redraw(id);
}

async function waitForDashboardTile(id) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (document.querySelector(`[data-dash-id="${id}"]`)) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return false;
}

async function seedDirectHistory(id) {
  try {
    const response = await fetch(`api/devices/${id}/telemetry?seconds=${HISTORY_MINUTES * 60}`);
    const snapshot = await response.json();
    if (!response.ok || !snapshot.available) return;
    const state = trace(id);
    state.points = snapshot.points.slice(-MAX_POINTS)
      .filter((sample) => sample.movement_score != null)
      .map((sample) => ({ t: sample.t * 1000, v: sample.movement_score }));
    const last = snapshot.points[snapshot.points.length - 1];
    if (last?.threshold != null) state.threshold = last.threshold;
    redraw(id);
  } catch (err) {
    /* Home Assistant history in the core dashboard remains the fallback. */
  }
}

async function startLiveDashboard() {
  stopLiveDashboard();
  const generation = liveGeneration;
  let devices;
  try {
    const response = await fetch("api/devices", { cache: "no-store" });
    devices = await response.json();
    if (!response.ok || !Array.isArray(devices)) return;
  } catch (err) {
    return; // The core dashboard continues with Home Assistant polling.
  }
  for (const device of devices.filter((item) => item.status === "success" && item.config.direct_api)) {
    if (!await waitForDashboardTile(device.id)) continue;
    if (generation !== liveGeneration) return;
    await seedDirectHistory(device.id);
    if (generation !== liveGeneration) return;
    const source = new EventSource(`api/devices/${device.id}/telemetry/stream`);
    source.onmessage = (event) => {
      try {
        applyLiveSample(device.id, JSON.parse(event.data));
      } catch (err) {
        /* Ignore a malformed sample without dropping the connection. */
      }
    };
    liveSources.set(device.id, source);
  }
}

function stopLiveDashboard() {
  liveGeneration += 1;
  for (const source of liveSources.values()) source.close();
  liveSources.clear();
}

document.querySelector('.tab-btn[data-tab="dashboard"]').addEventListener("click", () => {
  // dashboard.js renders synchronously until its first awaited history call.
  setTimeout(startLiveDashboard, 0);
});

for (const button of document.querySelectorAll('.tab-btn:not([data-tab="dashboard"])')) {
  button.addEventListener("click", stopLiveDashboard);
}
