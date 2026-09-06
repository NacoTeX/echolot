// Guided ground-truth recording for transparent threshold recommendations.

function installCalibrationPanel() {
  const dashboardButton = document.querySelector('.tab-btn[data-tab="dashboard"]');
  const dashboardPanel = document.getElementById("tab-dashboard");
  if (!dashboardButton || !dashboardPanel || document.getElementById("tab-calibration")) return;
  dashboardButton.insertAdjacentHTML(
    "afterend",
    '<button class="tab-btn" data-tab="calibration">Kalibrierung</button>'
  );
  dashboardPanel.insertAdjacentHTML("afterend", `
    <section id="tab-calibration" class="tab-panel" hidden>
      <section class="card calibration-intro">
        <h2>Calibration Lab</h2>
        <p>Zeichne echte Situationen im Raum auf. Echolot verbindet die direkte
          ESPectre-Telemetrie mit deinen Markierungen und berechnet daraus eine
          nachvollziehbare Schwellenwert-Empfehlung.</p>
        <form id="calibration-form" class="calibration-form">
          <label>Gerät<select name="device_id" id="calibration-device" required></select></label>
          <label>Name der Messung<input name="name" maxlength="100" placeholder="z. B. Wohnzimmer am Abend"></label>
          <button type="submit">Aufzeichnung starten</button>
        </form>
        <p id="calibration-error" class="status status-err" hidden></p>
      </section>
      <section id="active-calibration" class="card" hidden>
        <div class="calibration-heading"><div><h2>Aufzeichnung läuft</h2>
          <strong data-session-name></strong></div><span class="recording-indicator">● LIVE</span></div>
        <p class="hint">Markiere immer den Zustand, der im Raum gerade tatsächlich gilt.</p>
        <div class="label-buttons">
          <button type="button" data-label="empty">Raum leer</button>
          <button type="button" data-label="moving">Person bewegt sich</button>
          <button type="button" data-label="still">Person sitzt still</button>
          <button type="button" data-label="interference">Störquelle aktiv</button>
        </div>
        <div class="calibration-live"><span><b data-sample-count>0</b> Samples</span>
          <span>Aktuelles Label: <b data-current-label>noch nicht markiert</b></span></div>
        <button type="button" id="stop-calibration" class="btn-secondary">Aufzeichnung beenden</button>
      </section>
      <section><h2>Messungen</h2><div id="calibration-list" class="device-list"></div></section>
    </section>`);
}

installCalibrationPanel();

const CALIBRATION_REFRESH_MS = 2000;
let activeCalibration = null;
let calibrationTimer = null;

const LABEL_NAMES = {
  unlabelled: "noch nicht markiert",
  empty: "Raum leer",
  moving: "Person bewegt sich",
  still: "Person sitzt still",
  interference: "Störquelle aktiv",
};

async function calibrationJson(url, options) {
  const response = await fetch(url, options);
  const body = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.detail || `HTTP ${response.status}`);
  return body;
}

async function loadCalibrationLab() {
  const error = document.getElementById("calibration-error");
  try {
    const [devices, sessions] = await Promise.all([
      calibrationJson("api/devices"),
      calibrationJson("api/calibrations"),
    ]);
    const eligible = devices.filter((d) => d.status === "success" && d.config.direct_api);
    const select = document.getElementById("calibration-device");
    select.innerHTML = eligible.length
      ? eligible.map((d) => `<option value="${d.id}">${escapeHtml(d.config.friendly_name || d.config.name)}</option>`).join("")
      : '<option value="">Kein Gerät mit Direkt-Telemetrie verfügbar</option>';
    select.disabled = !eligible.length;
    document.querySelector("#calibration-form button").disabled = !eligible.length;
    activeCalibration = sessions.find((session) => session.status === "recording") || null;
    renderActiveCalibration();
    renderCalibrationSessions(sessions, Object.fromEntries(devices.map((d) => [d.id, d])));
    error.hidden = true;
  } catch (err) {
    error.textContent = `Calibration Lab konnte nicht geladen werden: ${err.message}`;
    error.hidden = false;
  }
}

function renderActiveCalibration() {
  const panel = document.getElementById("active-calibration");
  panel.hidden = !activeCalibration;
  if (!activeCalibration) return;
  panel.querySelector("[data-session-name]").textContent = activeCalibration.name;
  panel.querySelector("[data-sample-count]").textContent = activeCalibration.sample_count;
  panel.querySelector("[data-current-label]").textContent = LABEL_NAMES[activeCalibration.label] || activeCalibration.label;
  for (const button of panel.querySelectorAll("[data-label]")) {
    button.classList.toggle("active", button.dataset.label === activeCalibration.label);
  }
}

function recommendationBlock(session) {
  const r = session.recommendation;
  if (!r) {
    return `<p class="hint">Für eine Empfehlung werden mindestens je 20 Samples für
      „Raum leer“ und für Anwesenheit benötigt.</p>`;
  }
  const quality = { good: "gut", fair: "brauchbar", poor: "schwach" }[r.quality] || r.quality;
  return `<div class="recommendation">
    <strong>Empfehlung: Ein ${r.enter_threshold.toFixed(2)} · Aus ${r.exit_threshold.toFixed(2)}</strong>
    <span>Trennung: ${quality} (${r.separation.toFixed(2)}× Rauschen)</span>
    <span>Baseline ${r.baseline.toFixed(3)} · Rauschen ${r.noise.toFixed(3)}</span>
    <span>Geschätzt: ${(r.estimated_false_positive_rate * 100).toFixed(1)} % Fehlalarm · ${(r.estimated_false_negative_rate * 100).toFixed(1)} % übersehen</span>
  </div>`;
}

function renderCalibrationSessions(sessions, devices) {
  const list = document.getElementById("calibration-list");
  list.innerHTML = sessions.length ? sessions.map((session) => {
    const device = devices[session.device_id];
    const deviceName = device ? device.config.friendly_name || device.config.name : session.device_id;
    const status = session.status === "recording" ? "läuft" : session.status === "complete" ? "beendet" : "unterbrochen";
    return `<article class="card calibration-session" data-calibration-id="${session.id}">
      <div class="device-card-header"><h3>${escapeHtml(session.name)}</h3><span class="status">${status}</span></div>
      <p class="device-meta">${escapeHtml(deviceName)} · ${session.sample_count} Samples · ${new Date(session.started_at * 1000).toLocaleString("de-DE")}</p>
      ${recommendationBlock(session)}
      <div class="device-actions">
        <a class="btn-secondary" href="api/calibrations/${session.id}/export.csv" download>CSV exportieren</a>
        <button type="button" class="delete-calibration btn-secondary">Löschen</button>
      </div>
    </article>`;
  }).join("") : '<p class="status status-pending">Noch keine Messung aufgezeichnet.</p>';

  for (const button of list.querySelectorAll(".delete-calibration")) {
    button.addEventListener("click", async () => {
      const id = button.closest("[data-calibration-id]").dataset.calibrationId;
      if (!confirm("Diese Kalibrierungsmessung endgültig löschen?")) return;
      try {
        await calibrationJson(`api/calibrations/${id}`, { method: "DELETE" });
        await loadCalibrationLab();
      } catch (err) {
        alert(err.message);
      }
    });
  }
}

document.getElementById("calibration-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const error = document.getElementById("calibration-error");
  try {
    activeCalibration = await calibrationJson("api/calibrations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: form.elements.device_id.value, name: form.elements.name.value }),
    });
    form.elements.name.value = "";
    error.hidden = true;
    await loadCalibrationLab();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

for (const button of document.querySelectorAll("#active-calibration [data-label]")) {
  button.addEventListener("click", async () => {
    if (!activeCalibration) return;
    try {
      activeCalibration = await calibrationJson(`api/calibrations/${activeCalibration.id}/label`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: button.dataset.label }),
      });
      renderActiveCalibration();
    } catch (err) {
      alert(err.message);
    }
  });
}

document.getElementById("stop-calibration").addEventListener("click", async () => {
  if (!activeCalibration) return;
  try {
    await calibrationJson(`api/calibrations/${activeCalibration.id}/stop`, { method: "POST" });
    activeCalibration = null;
    await loadCalibrationLab();
  } catch (err) {
    alert(err.message);
  }
});

document.querySelector('.tab-btn[data-tab="calibration"]').addEventListener("click", () => {
  for (const button of document.querySelectorAll(".tab-btn")) button.classList.remove("active");
  document.querySelector('.tab-btn[data-tab="calibration"]').classList.add("active");
  for (const panel of document.querySelectorAll(".tab-panel")) panel.hidden = true;
  document.getElementById("tab-calibration").hidden = false;
  loadCalibrationLab();
  if (!calibrationTimer) calibrationTimer = setInterval(loadCalibrationLab, CALIBRATION_REFRESH_MS);
});

for (const button of document.querySelectorAll('.tab-btn:not([data-tab="calibration"])')) {
  button.addEventListener("click", () => {
    clearInterval(calibrationTimer);
    calibrationTimer = null;
  });
}
