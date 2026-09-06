// Geräteverwaltung + Flashen über den Browser (Phase 2).
//
// Alle API-Pfade sind relativ (ohne führenden Slash), damit sie unter dem
// Ingress-Präfix bleiben — Begründung in app.js.

const POLL_INTERVAL_MS = 2000;
const activePolls = new Set();

async function loadBoards() {
  const select = document.getElementById("board-select");
  try {
    const boards = await (await fetch("api/boards")).json();
    select.innerHTML = boards
      .map((b) => `<option value="${escapeHtml(b.key)}">${escapeHtml(b.label)}${b.experimental ? " ⚠" : ""}</option>`)
      .join("");
  } catch (err) {
    select.innerHTML = '<option value="">Boards konnten nicht geladen werden</option>';
  }
}

let kbPerPps = 0.09; // vom Backend überschrieben

// Voreinstellungen ersparen es, vier Parameter mit nicht offensichtlichen
// Wechselwirkungen von Hand zu treffen.
async function loadPresets() {
  const select = document.getElementById("preset-select");
  const description = document.getElementById("preset-description");
  const form = document.getElementById("device-form");
  let data;
  try {
    data = await (await fetch("api/presets")).json();
  } catch (err) {
    return;
  }
  kbPerPps = data.kb_per_second_per_pps;
  updateRateEstimate();

  for (const p of data.presets) {
    const opt = document.createElement("option");
    opt.value = p.key;
    opt.textContent = p.label;
    select.appendChild(opt);
  }
  select.value = "balanced";
  const apply = () => {
    const preset = data.presets.find((p) => p.key === select.value);
    description.textContent = preset ? preset.description : "";
    if (!preset) return;
    form.elements.csi_target_pps.value = preset.csi_target_pps;
    form.elements.detection_algorithm.value = preset.detection_algorithm;
    form.elements.evaluation_interval_ms.value = preset.evaluation_interval_ms;
    updateRateEstimate();
  };
  select.addEventListener("change", apply);
  apply();

  // Wer die Werte selbst anfasst, verlässt die Voreinstellung.
  for (const name of ["csi_target_pps", "detection_algorithm", "evaluation_interval_ms"]) {
    form.elements[name].addEventListener("input", () => {
      select.value = "";
      description.textContent = "";
    });
  }
  form.elements.csi_target_pps.addEventListener("input", updateRateEstimate);
}

function updateRateEstimate() {
  const el = document.getElementById("rate-estimate");
  const rate = Number(document.getElementById("device-form").elements.csi_target_pps.value);
  // 0 used to mean "an external source generates the traffic"; that is a
  // separate setting upstream now (csi_traffic_mode), and the rate itself
  // starts at 1.
  if (!Number.isFinite(rate) || rate <= 0) {
    el.textContent = "";
    return;
  }
  el.textContent = `≈ ${(rate * kbPerPps).toFixed(1)} KB/s Funklast pro Gerät`;
}

function statusLabel(status) {
  return { idle: "nicht gebaut", queued: "in Warteschlange…", running: "wird gebaut…", success: "bereit zum Flashen", error: "Build fehlgeschlagen" }[status] || status;
}

function statusClass(status) {
  return { success: "status-ok", error: "status-err", running: "status-warn", queued: "status-warn" }[status] || "status-pending";
}

const ENTITY_FIELDS = ["entity_motion", "entity_movement_score", "entity_threshold", "entity_calibrate"];
const ENTITY_LABELS = { entity_motion: "Bewegung", entity_movement_score: "Bewegungswert", entity_threshold: "Schwelle", entity_calibrate: "Kalibrierung" };

function renderDevice(device) {
  const c = device.config;
  const title = c.friendly_name || c.name;
  const canBuild = device.status !== "queued" && device.status !== "running";
  const built = device.status === "success";

  const logBlock = device.build_log
    ? `<details><summary>Build-Protokoll</summary><pre class="build-log">${escapeHtml(device.build_log.slice(-4000))}</pre></details>`
    : "";
  const errorLine = device.build_error
    ? `<p class="status status-err">${escapeHtml(device.build_error)}</p>`
    : "";

  // Only offered after a failure the backend traced to the toolchain: the
  // button throws away a ~2 GB download, so it must not read as a routine
  // "try this" next to every build.
  const otaLine = device.ota_status === "running" || device.ota_status === "queued"
    ? '<p class="status status-pending">Update über WLAN läuft…</p>'
    : device.ota_error
      ? `<p class="status status-err">${escapeHtml(device.ota_error)}</p>`
      : device.ota_last_success
        ? `<p class="field-note">Zuletzt über WLAN aktualisiert: ${
            escapeHtml(new Date(device.ota_last_success * 1000).toLocaleString("de-DE"))}</p>`
        : "";

  const toolchainBroken = (device.build_error || "").includes("Toolchain");
  const repairBlock = toolchainBroken
    ? `<button class="repair-btn btn-secondary" ${canBuild ? "" : "disabled"}>Toolchain zurücksetzen</button>`
    : "";

  const flashBlock = built
    ? `<esp-web-install-button manifest="api/devices/${device.id}/manifest.json">
         <button slot="activate">Über USB flashen</button>
         <span slot="unsupported">Dieser Browser unterstützt kein Web Serial (nutze Chrome oder Edge).</span>
         <span slot="not-allowed">Web Serial benötigt HTTPS oder localhost — siehe Hinweis oben.</span>
       </esp-web-install-button>
       <a class="btn-secondary firmware-download" href="api/devices/${device.id}/firmware.bin"
          download="${escapeHtml(c.name)}-firmware.bin">Firmware herunterladen</a>`
    : "";

  const liveBlock = built
    ? `<div class="live-block" data-live-id="${device.id}">
         <div class="live-row"><span class="live-label">Bewegung</span><span class="live-motion status status-pending">wird geprüft…</span></div>
         <div class="live-row"><span class="live-label">Bewegungswert</span><span class="live-score">—</span></div>
         <form class="threshold-form">
           <label>Schwelle
             <input class="threshold-input" type="number" min="0" max="10" step="0.1" placeholder="0.0–10.0">
           </label>
           <button type="submit">Senden</button>
         </form>
         <button type="button" class="calibrate-btn">Neu kalibrieren</button>
         <p class="live-error status status-err" hidden></p>
         <button type="button" class="detect-btn btn-secondary" hidden>Entities in Home Assistant suchen</button>
       </div>
       <div class="network-block">
         <label class="address-label">Netzwerkadresse
           <input class="address-input" value="${escapeHtml(device.address || "")}"
                  placeholder="${escapeHtml(device.config.name)}.local oder IP">
         </label>
         <div class="device-actions">
           <button type="button" class="probe-btn btn-secondary">Erreichbarkeit prüfen</button>
           <button type="button" class="ota-btn btn-secondary">Update über WLAN</button>
           ${device.config.web_server
             ? `<a class="status-page-link btn-secondary" target="_blank" rel="noopener"
                   href="http://${encodeURIComponent(device.address || device.config.name + ".local")}/"
                   >Statusseite öffnen</a>`
             : ""}
         </div>
         <p class="probe-result status" hidden></p>
       </div>
       <details class="key-block">
         <summary>Verschlüsselungscode für Home Assistant</summary>
         <p class="hint">
           ${device.config.api_encryption
             ? "Home Assistant fragt danach, wenn es dieses Gerät übernimmt. Ohne den Code kann niemand im Netz das Gerät auslesen oder steuern."
             : "Für dieses Gerät ist die API-Verschlüsselung <strong>abgeschaltet</strong> — der Code steckt also nicht in der Firmware und Home Assistant fragt nicht danach. Er bleibt gespeichert, falls du sie später einschaltest."}
         </p>
         <div class="key-row">
           <code class="api-key">— aufklappen zum Anzeigen —</code>
           <button type="button" class="copy-key-btn btn-secondary">Kopieren</button>
         </div>
       </details>
       <details class="entity-editor">
         <summary>HA-Entity-IDs</summary>
         ${ENTITY_FIELDS.map((f) => `
           <label>${ENTITY_LABELS[f]}
             <input class="entity-input" data-field="${f}" value="${escapeHtml(device[f] || "")}">
           </label>`).join("")}
         <button type="button" class="save-entities-btn">Entity-IDs speichern</button>
       </details>`
    : "";

  return `
    <div class="card device-card" data-id="${device.id}">
      <div class="device-card-header">
        <h3>${escapeHtml(title)}</h3>
        <span class="status ${statusClass(device.status)}">${statusLabel(device.status)}</span>
      </div>
      <p class="device-meta">${escapeHtml(c.name)} · ${escapeHtml(c.board)} · ${escapeHtml(c.detection_algorithm)}</p>
      ${errorLine}
      ${otaLine}
      <div class="device-actions">
        <button class="build-btn${built ? " btn-secondary" : ""}" ${canBuild ? "" : "disabled"}>${built ? "Neu bauen" : "Firmware bauen"}</button>
        ${repairBlock}
        ${flashBlock}
        <button class="delete-btn">Löschen</button>
      </div>
      ${liveBlock}
      ${logBlock}
    </div>`;
}

function addressOf(card) {
  const input = card.querySelector(".address-input");
  return input ? input.value.trim() : "";
}

async function probeDevice(id, card) {
  const out = card.querySelector(".probe-result");
  const button = card.querySelector(".probe-btn");
  button.disabled = true;
  out.hidden = false;
  out.className = "probe-result status status-pending";
  out.textContent = "Wird geprüft…";

  const host = addressOf(card);
  const url = host
    ? `api/devices/${id}/reachability?host=${encodeURIComponent(host)}`
    : `api/devices/${id}/reachability`;
  try {
    const res = await fetch(url);
    const body = await res.json();
    if (!res.ok) {
      out.className = "probe-result status status-err";
      out.textContent = body.detail || "Prüfung fehlgeschlagen";
      return;
    }
    // "ok" means the device answers — which, when it is still missing in
    // Home Assistant, points at adoption rather than at the network. That
    // is a caveat, not a success, so it is not painted green.
    out.className = `probe-result status ${body.api ? "status-ok" : "status-warn"}`;
    out.textContent = body.message;
  } catch (err) {
    out.className = "probe-result status status-err";
    out.textContent = "Backend nicht erreichbar";
  } finally {
    button.disabled = false;
  }
}

async function startOta(id, card) {
  const address = addressOf(card);
  if (!confirm(
    "Die gebaute Firmware wird über das WLAN auf das Gerät geschoben" +
    (address ? ` (${address}).` : ".") +
    "\n\nDas Gerät startet dabei neu. Fortfahren?"
  )) return;

  const button = card.querySelector(".ota-btn");
  button.disabled = true;
  try {
    const res = await fetch(`api/devices/${id}/ota`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(address ? { address } : {}),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(body.detail || "Das Update konnte nicht gestartet werden");
      return;
    }
    pollDevice(id);
  } catch (err) {
    alert("Backend nicht erreichbar");
  } finally {
    button.disabled = false;
  }
}

async function revealKey(id, card) {
  const target = card.querySelector(".api-key");
  if (target.dataset.loaded) return;
  target.textContent = "wird geladen…";
  try {
    const res = await fetch(`api/devices/${id}/credentials`);
    const body = await res.json();
    if (!res.ok) {
      target.textContent = body.detail || "konnte nicht geladen werden";
      return;
    }
    target.textContent = body.api_encryption_key;
    target.dataset.loaded = "1";
  } catch (err) {
    target.textContent = "Backend nicht erreichbar";
  }
}

async function copyKey(card, button) {
  const key = card.querySelector(".api-key").textContent;
  const original = button.textContent;
  try {
    await navigator.clipboard.writeText(key);
    button.textContent = "Kopiert";
  } catch (err) {
    // Clipboard access needs a secure context, which Ingress over plain
    // HTTP is not. Select the text instead so it can be copied by hand.
    const range = document.createRange();
    range.selectNodeContents(card.querySelector(".api-key"));
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    button.textContent = "Markiert — jetzt kopieren";
  }
  setTimeout(() => { button.textContent = original; }, 2500);
}

async function detectEntities(id, button) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Wird gesucht…";
  try {
    const res = await fetch(`api/devices/${id}/entities/detect`, { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(body.detail || "Die Entities konnten nicht gefunden werden");
      return;
    }
    alert(
      "Gefunden:\n" +
      Object.values(body.detected).join("\n") +
      "\n\nDie IDs sind gespeichert."
    );
    await loadDevices();
  } catch (err) {
    alert("Backend nicht erreichbar");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function resetToolchain(id, button) {
  if (!confirm(
    "Die Toolchain für dieses Board wird gelöscht und beim nächsten Build " +
    "neu heruntergeladen (rund 2 GB). Fortfahren?"
  )) return;

  button.disabled = true;
  button.textContent = "Wird zurückgesetzt…";
  try {
    const res = await fetch(`api/devices/${id}/toolchain/reset`, { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(body.detail || "Die Toolchain konnte nicht zurückgesetzt werden");
      return;
    }
    alert(
      body.removed
        ? "Toolchain entfernt. Starte den Build neu — der Download läuft dann automatisch."
        : "Es war keine Toolchain installiert. Starte den Build einfach neu."
    );
    await loadDevices();
  } catch (err) {
    alert("Backend nicht erreichbar");
  } finally {
    button.disabled = false;
    button.textContent = "Toolchain zurücksetzen";
  }
}

async function loadDevices() {
  const list = document.getElementById("device-list");
  let devices;
  try {
    devices = await (await fetch("api/devices")).json();
  } catch (err) {
    list.innerHTML = '<p class="status status-err">Geräte konnten nicht geladen werden</p>';
    return;
  }

  list.innerHTML = devices.length
    ? devices.map(renderDevice).join("")
    : '<p class="status status-pending">Noch keine Geräte — lege oben eines an.</p>';

  for (const el of list.querySelectorAll(".device-card")) {
    const id = el.dataset.id;
    el.querySelector(".build-btn").addEventListener("click", () => startBuild(id));
    el.querySelector(".delete-btn").addEventListener("click", () => deleteDevice(id));

    const repairBtn = el.querySelector(".repair-btn");
    if (repairBtn) repairBtn.addEventListener("click", () => resetToolchain(id, repairBtn));

    const thresholdForm = el.querySelector(".threshold-form");
    if (thresholdForm) thresholdForm.addEventListener("submit", (evt) => pushThreshold(evt, id));

    const detectBtn = el.querySelector(".detect-btn");
    if (detectBtn) detectBtn.addEventListener("click", () => detectEntities(id, detectBtn));

    const calibrateBtn = el.querySelector(".calibrate-btn");
    if (calibrateBtn) calibrateBtn.addEventListener("click", () => calibrateDevice(id, calibrateBtn));

    const saveEntitiesBtn = el.querySelector(".save-entities-btn");
    if (saveEntitiesBtn) saveEntitiesBtn.addEventListener("click", () => saveEntityIds(id, el));

    const addressInput = el.querySelector(".address-input");
    if (addressInput) {
      addressInput.addEventListener("change", () =>
        fetch(`api/devices/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ address: addressInput.value.trim() || null }),
        }).catch(() => {})
      );
    }

    const probeBtn = el.querySelector(".probe-btn");
    if (probeBtn) probeBtn.addEventListener("click", () => probeDevice(id, el));

    const otaBtn = el.querySelector(".ota-btn");
    if (otaBtn) otaBtn.addEventListener("click", () => startOta(id, el));

    const keyBlock = el.querySelector(".key-block");
    if (keyBlock) {
      keyBlock.addEventListener("toggle", () => {
        if (keyBlock.open) revealKey(id, el);
      });
    }

    const copyKeyBtn = el.querySelector(".copy-key-btn");
    if (copyKeyBtn) copyKeyBtn.addEventListener("click", () => copyKey(el, copyKeyBtn));
  }

  for (const d of devices) {
    if (d.status === "queued" || d.status === "running") pollDevice(d.id);
    if (d.status === "success") refreshLiveState(d.id);
  }
}

async function refreshLiveState(id) {
  const block = document.querySelector(`.live-block[data-live-id="${id}"]`);
  if (!block) return;
  const motionEl = block.querySelector(".live-motion");
  const scoreEl = block.querySelector(".live-score");
  const thresholdInput = block.querySelector(".threshold-input");
  const errorEl = block.querySelector(".live-error");

  let state;
  try {
    state = await (await fetch(`api/devices/${id}/state`)).json();
  } catch (err) {
    state = { available: false, error: "Backend nicht erreichbar" };
  }

  const detectBtn = block.querySelector(".detect-btn");

  if (!state.available) {
    motionEl.textContent = "nicht verfügbar";
    motionEl.className = "live-motion status status-warn";
    scoreEl.textContent = "—";
    errorEl.textContent = state.error || "Nicht verfügbar";
    errorEl.hidden = false;
    // The lookup only helps when the entity is missing; a broken
    // connection to Home Assistant is a different problem and offering it
    // there would just waste a click.
    if (detectBtn) detectBtn.hidden = !(state.error || "").includes("existiert in Home Assistant nicht");
    return;
  }

  if (detectBtn) detectBtn.hidden = true;
  errorEl.hidden = true;
  motionEl.textContent = state.motion ? "erkannt" : "frei";
  motionEl.className = `live-motion status ${state.motion ? "status-ok" : "status-pending"}`;
  scoreEl.textContent = state.movement_score != null ? state.movement_score.toFixed(2) : "—";
  if (state.threshold != null && document.activeElement !== thresholdInput) {
    thresholdInput.value = state.threshold;
  }
}

function refreshAllLiveStates() {
  for (const block of document.querySelectorAll(".live-block")) {
    refreshLiveState(block.dataset.liveId);
  }
}

async function pushThreshold(evt, id) {
  evt.preventDefault();
  const form = evt.target;
  const input = form.querySelector(".threshold-input");
  const value = Number(input.value);
  const errorEl = form.closest(".live-block").querySelector(".live-error");
  try {
    const res = await fetch(`api/devices/${id}/threshold`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      errorEl.textContent = body.detail || "Schwelle konnte nicht gesendet werden";
      errorEl.hidden = false;
      return;
    }
    errorEl.hidden = true;
  } catch (err) {
    errorEl.textContent = "Backend nicht erreichbar";
    errorEl.hidden = false;
  }
}

async function calibrateDevice(id, button) {
  const errorEl = button.closest(".live-block").querySelector(".live-error");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Kalibriert…";
  try {
    const res = await fetch(`api/devices/${id}/calibrate`, { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      errorEl.textContent = body.detail || "Kalibrierung konnte nicht ausgelöst werden";
      errorEl.hidden = false;
    } else {
      errorEl.hidden = true;
    }
  } catch (err) {
    errorEl.textContent = "Backend nicht erreichbar";
    errorEl.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function saveEntityIds(id, cardEl) {
  const patch = {};
  for (const input of cardEl.querySelectorAll(".entity-input")) {
    patch[input.dataset.field] = input.value || null;
  }
  try {
    const res = await fetch(`api/devices/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (res.ok) refreshLiveState(id);
  } catch (err) {
    // Best-effort — the entity editor has no dedicated error slot; a failed
    // save just leaves the live state as before.
  }
}

async function startBuild(id) {
  try {
    const res = await fetch(`api/devices/${id}/build`, { method: "POST" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(data.detail || "Build konnte nicht gestartet werden");
      return;
    }
  } catch (err) {
    alert("Backend nicht erreichbar");
    return;
  }
  await loadDevices();
}

async function deleteDevice(id) {
  if (!confirm("Dieses Gerät löschen?")) return;
  await fetch(`api/devices/${id}`, { method: "DELETE" });
  await loadDevices();
}

function pollDevice(id) {
  if (activePolls.has(id)) return;
  activePolls.add(id);
  const tick = async () => {
    let device;
    try {
      device = await (await fetch(`api/devices/${id}`)).json();
    } catch (err) {
      activePolls.delete(id);
      return;
    }
    const busy = (st) => st === "queued" || st === "running";
    if (busy(device.status) || busy(device.ota_status)) {
      setTimeout(tick, POLL_INTERVAL_MS);
    } else {
      activePolls.delete(id);
      await loadDevices();
    }
  };
  setTimeout(tick, POLL_INTERVAL_MS);
}

document.getElementById("device-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const errorEl = document.getElementById("device-form-error");
  errorEl.hidden = true;

  const form = evt.target;
  const data = Object.fromEntries(new FormData(form).entries());
  data.csi_target_pps = Number(data.csi_target_pps);
  data.evaluation_interval_ms = Number(data.evaluation_interval_ms);
  // FormData drops unchecked boxes entirely and reports "on" for checked
  // ones, so neither state survives as the boolean the API expects.
  data.web_server = form.elements.web_server.checked;
  data.diagnostics = form.elements.diagnostics.checked;
  data.api_encryption = form.elements.api_encryption.checked;
  if (!data.friendly_name) delete data.friendly_name;
  if (!data.wifi_password) delete data.wifi_password;

  try {
    const res = await fetch("api/devices", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const detail = Array.isArray(body.detail)
        ? body.detail.map((e) => e.msg).join("; ")
        : body.detail || "Gerät konnte nicht angelegt werden";
      errorEl.textContent = detail;
      errorEl.hidden = false;
      return;
    }
    form.reset();
    await loadDevices();
  } catch (err) {
    errorEl.textContent = "Backend nicht erreichbar";
    errorEl.hidden = false;
  }
});

loadBoards();
loadPresets();
loadDevices();
setInterval(refreshAllLiveStates, 5000);
