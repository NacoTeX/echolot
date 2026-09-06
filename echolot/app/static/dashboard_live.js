// Direct-telemetry enhancement layered over the stable HA-backed dashboard.
// Keeping this separate avoids making the dashboard unusable when direct data
// is absent and keeps feature development out of the core dashboard module.

const liveSources = new Map();
let liveGeneration = 0;
let fusionTimer = null;

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
  await refreshFusion();
  if (generation === liveGeneration && !fusionTimer) {
    fusionTimer = setInterval(refreshFusion, FUSION_POLL_MS);
  }
}

// --- Confidence fusion ------------------------------------------------------
//
// A second opinion on each zone, computed from direct telemetry and the
// Calibration Lab profiles rather than from Home Assistant.
//
// It gets its own line and deliberately does NOT write [data-zone-state],
// [data-dot] or the member chips. dashboard.js writes those every POLL_MS
// from the zone state machine, and that machine — with its hold time — is
// what actually drives Home Assistant and the MQTT export. Two timers
// writing one element would flip the tile between two verdicts, and it
// would overwrite the very value the fusion is worth comparing against.

const FUSION_POLL_MS = 2000;

function fusionStateLabel(result) {
  const percent = Math.round(result.confidence * 100);
  if (result.state === "occupied") return `belegt · ${percent} %`;
  if (result.state === "vacant") return `frei · ${100 - percent} %`;
  return `unsicher · ${percent} %`;
}

function fusionDetail(result) {
  return result.members
    .map((member) =>
      member.reliability > 0
        ? `${member.name}: ${Math.round(member.probability * 100)} % Präsenz, ` +
          `${Math.round(member.reliability * 100)} % Verlässlichkeit (${member.basis})`
        : `${member.name}: keine aktuellen Direktdaten`,
    )
    .join("\n");
}

function fusionNote(tile) {
  let note = tile.querySelector(".fusion-note");
  if (!note) {
    note = document.createElement("p");
    note.className = "tile-note fusion-note";
    tile.appendChild(note);
  }
  return note;
}

async function refreshFusion() {
  let results;
  try {
    const response = await fetch("api/fusion/zones", { cache: "no-store" });
    results = await response.json();
    if (!response.ok || !Array.isArray(results)) return;
  } catch (err) {
    return; // The zone state machine on the tile is unaffected.
  }
  for (const result of results) {
    const tile = document.querySelector(`[data-dash-zone="${result.zone_id}"]`);
    if (!tile) continue;
    const note = fusionNote(tile);
    if (!result.available) {
      note.textContent = "Direkt-Fusion wartet auf aktuelle Samples";
      note.title = fusionDetail(result);
      note.hidden = false;
      continue;
    }
    note.textContent =
      `Direkt-Fusion: ${fusionStateLabel(result)} · ` +
      `Übereinstimmung ${Math.round(result.agreement * 100)} %`;
    note.title = fusionDetail(result);
    note.hidden = false;
  }
}

function stopLiveDashboard() {
  liveGeneration += 1;
  for (const source of liveSources.values()) source.close();
  liveSources.clear();
  if (fusionTimer) {
    clearInterval(fusionTimer);
    fusionTimer = null;
  }
}

document.querySelector('.tab-btn[data-tab="dashboard"]').addEventListener("click", () => {
  // dashboard.js renders synchronously until its first awaited history call.
  setTimeout(startLiveDashboard, 0);
});

for (const button of document.querySelectorAll('.tab-btn:not([data-tab="dashboard"])')) {
  button.addEventListener("click", stopLiveDashboard);
}
