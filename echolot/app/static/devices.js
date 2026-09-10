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

    // The band field belongs to the board, not to the form: only the
    // ESP32-C5 has two radios, and ESPHome rejects `band_mode` outright
    // on every other chip. Showing the control anywhere else would offer
    // a choice the hardware cannot make — so it is hidden *and* disabled,
    // because a disabled field is the one FormData leaves out.
    dualBandBoards = new Set(boards.filter((b) => b.dual_band).map((b) => b.key));
    const dualBand = dualBandBoards;
    const field = document.getElementById("wifi-band-field");
    const applyBand = () => {
      const offered = dualBand.has(select.value);
      field.hidden = !offered;
      field.querySelector("select").disabled = !offered;
    };
    select.addEventListener("change", applyBand);
    applyBand();
  } catch (err) {
    select.innerHTML = '<option value="">Boards konnten nicht geladen werden</option>';
  }
}

let kbPerPps = 0.09; // vom Backend überschrieben

// Which boards offer a band at all. Filled by loadBoards(); until then
// empty, which is the safe direction — the field stays hidden rather
// than offering a radio the chip may not have.
let dualBandBoards = new Set();

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

// Only worth saying where there was a choice. On every other chip the
// band is 2.4 GHz by construction, and printing it would read as a
// setting rather than as a fact about the radio.
const BAND_LABELS = { "2.4GHz": "2,4 GHz", "5GHz": "5 GHz", auto: "Band automatisch" };

function bandSuffix(config) {
  const label = BAND_LABELS[config.wifi_band];
  return label && config.wifi_band !== "2.4GHz" ? ` (${escapeHtml(label)})` : "";
}

function statusLabel(status) {
  return { idle: "nicht gebaut", queued: "in Warteschlange…", running: "wird gebaut…", success: "bereit zum Flashen", error: "Build fehlgeschlagen" }[status] || status;
}

function statusClass(status) {
  return { success: "status-ok", error: "status-err", running: "status-warn", queued: "status-warn" }[status] || "status-pending";
}

const ENTITY_FIELDS = ["entity_motion", "entity_movement_score", "entity_threshold", "entity_calibrate"];
const ENTITY_LABELS = { entity_motion: "Bewegung", entity_movement_score: "Bewegungswert", entity_threshold: "Schwelle", entity_calibrate: "Kalibrierung" };

//: Which cards are open. A poll re-renders the whole list, so without
//: this every open card would snap shut under the person reading it.
const openCards = new Set();
//: The status each device had when it was last drawn, so a build that
//: fails can open its own card instead of hiding the reason.
const lastStatus = new Map();

function expandAllCards() {
  return readPref("echolot.expandCards");
}

function shouldOpen(device, deviceCount) {
  const previous = lastStatus.get(device.id);
  const first = previous === undefined;
  lastStatus.set(device.id, device.status);

  // Something is happening or went wrong: the detail is the whole point,
  // so open it — but only on the transition, never on every poll, or the
  // card would spring back open each time it is closed.
  const busy = device.status === "queued" || device.status === "running";
  if ((first && (busy || device.status === "error")) || (!first && previous !== device.status && device.status === "error")) {
    openCards.add(device.id);
  }
  // With one device there is nothing to scan past, so the list view buys
  // nothing and the card opens.
  if (first && deviceCount === 1) openCards.add(device.id);

  return expandAllCards() || openCards.has(device.id);
}

//: A collapsible group inside a card. Kept out of the summary line so the
//: card stays a list entry: name, state, done.
function section(title, body, { open = false } = {}) {
  return `<details class="device-section"${open ? " open" : ""}>
      <summary>${escapeHtml(title)}</summary>
      <div class="device-section-body">${body}</div>
    </details>`;
}

function renderDevice(device, deviceCount) {
  const c = device.config;
  const title = c.friendly_name || c.name;
  const canBuild = device.status !== "queued" && device.status !== "running";
  const built = device.status === "success";

  const logBlock = device.build_log
    ? section("Build-Protokoll", `<pre class="build-log">${escapeHtml(device.build_log.slice(-4000))}</pre>`)
    : "";
  const errorLine = device.build_error
    ? `<p class="status status-err">${escapeHtml(device.build_error)}</p>`
    : "";

  // Not an error — the device works, it just works the way it was last
  // flashed. But somebody changed a setting and is entitled to know it
  // has not arrived on the chip yet.
  const staleLine = device.firmware_behind_config
    ? `<p class="stale-line status status-warn">
         Umkonfiguriert, aber noch nicht neu gebaut — auf dem Gerät läuft
         weiter das zuletzt geflashte Image.
       </p>`
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

  const sizeLabel = device.firmware_size
    ? ` (${(device.firmware_size / 1048576).toFixed(1)} MB)`
    : "";

  const flashBlock = built
    ? `<esp-web-install-button manifest="api/devices/${device.id}/manifest.json">
         <button slot="activate">Über USB flashen</button>
         <span slot="unsupported">Dieser Browser unterstützt kein Web Serial (nutze Chrome oder Edge).</span>
         <span slot="not-allowed">Web Serial benötigt HTTPS oder localhost — siehe Hinweis oben.</span>
       </esp-web-install-button>
       <a class="download-fw-link btn-secondary"
          href="api/devices/${device.id}/firmware.bin"
          download="${escapeHtml(c.name)}-firmware.bin"
          title="Zum Flashen mit einem anderen Werkzeug, etwa web.esphome.io oder esptool"
          >Firmware herunterladen${escapeHtml(sizeLabel)}</a>
       <p class="flash-progress" hidden></p>`
    : "";

  // The live state stays out of a collapsible group: it is the reason to
  // open the card at all, and burying it behind a second click would put
  // the most-wanted number two clicks deep.
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
       </div>`
    : "";

  const detailSections = built
    ? section("Netzwerk und Update über WLAN", `
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
           <p class="probe-direct status status-pending" hidden></p>
         </div>`)
      + section("Diagnose", `
         <div class="health-block">
           <p class="hint">Prüft, ob das Gerät wirklich misst — und nicht nur erreichbar ist.</p>
           <button type="button" class="health-btn btn-secondary">Diagnose stellen</button>
           <div class="health-result" hidden></div>
         </div>`)
      + section("Verschlüsselungscode für Home Assistant", `
         <div class="key-block">
           <p class="hint">
             ${device.config.api_encryption
               ? "Home Assistant fragt danach, wenn es dieses Gerät übernimmt. Ohne den Code kann niemand im Netz das Gerät auslesen oder steuern."
               : "Für dieses Gerät ist die API-Verschlüsselung <strong>abgeschaltet</strong> — der Code steckt also nicht in der Firmware und Home Assistant fragt nicht danach. Er bleibt gespeichert, falls du sie später einschaltest."}
           </p>
           <div class="key-row">
             <code class="api-key">— aufklappen zum Anzeigen —</code>
             <button type="button" class="copy-key-btn btn-secondary">Kopieren</button>
           </div>
           <p class="hint">
             Notfall-WLAN <code>${escapeHtml(device.config.name)} Fallback</code> —
             das Gerät öffnet es, wenn es dein WLAN nicht erreicht.
             Passwort: <code class="fallback-password">— aufklappen zum Anzeigen —</code>
           </p>
         </div>`)
      + section("HA-Entity-IDs", `
         <div class="entity-editor">
           <p class="hint">
             Über diese IDs holt Echolot Zustand und Schwelle aus Home
             Assistant. Sie sind begründete Vermutungen — wenn ein Gerät als
             „nicht verfügbar“ angezeigt wird, stimmt hier meist etwas nicht.
           </p>
           ${ENTITY_FIELDS.map((f) => `
             <label>${ENTITY_LABELS[f]}
               <input class="entity-input" data-field="${f}" value="${escapeHtml(device[f] || "")}">
             </label>`).join("")}
           <button type="button" class="save-entities-btn">Entity-IDs speichern</button>
         </div>`)
    : "";

  // Editable at any point in a device's life, built or not. Changing one
  // of these used to mean deleting the device and making a new one — and
  // losing its entity ids, its learned profile and its recordings to
  // change one number.
  const configEditor = section("Firmware-Optionen ändern", `
     <div class="config-editor">
       <p class="hint">
         Diese Werte stecken im Image. Nach dem Speichern muss die Firmware
         neu gebaut und übertragen werden, sonst läuft auf dem Gerät weiter
         die alte. Gerätename und Board lassen sich nicht ändern — dafür ist
         ein neues Gerät der ehrliche Weg.
       </p>
       <label>Anzeigename
         <input class="cfg" data-field="friendly_name" value="${escapeHtml(c.friendly_name || "")}">
       </label>
       <label>WLAN-Name (SSID)
         <input class="cfg" data-field="wifi_ssid" maxlength="32" value="${escapeHtml(c.wifi_ssid || "")}">
       </label>
       <label>WLAN-Passwort
         <input class="cfg" data-field="wifi_password" type="password" maxlength="64"
                placeholder="unverändert lassen">
       </label>
       ${dualBandBoards.has(c.board) ? `
       <label>WLAN-Band
         <select class="cfg" data-field="wifi_band">
           ${["2.4GHz", "5GHz", "auto"].map((b) => `
             <option value="${b}"${c.wifi_band === b ? " selected" : ""}>${escapeHtml(BAND_LABELS[b] || b)}</option>`).join("")}
         </select>
         <span class="field-hint">
           Gemessen ist bisher nur 2,4 GHz. Ein Bandwechsel entwertet ein
           gelerntes Profil — es beschreibt dann eine andere Messung.
         </span>
       </label>` : ""}
       <label>Erkennungsprofil
         <select class="cfg" data-field="detection_algorithm">
           <option value="lightweight"${c.detection_algorithm === "lightweight" ? " selected" : ""}>Lightweight</option>
           <option value="high_accuracy"${c.detection_algorithm === "high_accuracy" ? " selected" : ""}>High Accuracy</option>
         </select>
       </label>
       <label>Paketrate (Pakete/s)
         <input class="cfg" data-field="csi_target_pps" type="number" min="1" max="500" value="${Number(c.csi_target_pps)}">
       </label>
       <label>Auswerteintervall (ms)
         <input class="cfg" data-field="evaluation_interval_ms" type="number" min="10" max="10000" value="${Number(c.evaluation_interval_ms)}">
       </label>
       <label>Log-Level
         <select class="cfg" data-field="log_level">
           ${["NONE", "ERROR", "WARN", "INFO", "DEBUG", "VERBOSE"].map((l) => `
             <option value="${l}"${c.log_level === l ? " selected" : ""}>${l}</option>`).join("")}
         </select>
       </label>
       <label class="checkbox-field">
         <input type="checkbox" class="cfg" data-field="web_server"${c.web_server ? " checked" : ""}>
         <span>Statusseite auf dem Gerät</span>
       </label>
       <label class="checkbox-field">
         <input type="checkbox" class="cfg" data-field="diagnostics"${c.diagnostics ? " checked" : ""}>
         <span>Diagnose-Sensoren</span>
       </label>
       <label class="checkbox-field">
         <input type="checkbox" class="cfg" data-field="direct_api"${c.direct_api ? " checked" : ""}>
         <span>Direkte Telemetrie (Port 62587)</span>
       </label>
       <label class="checkbox-field">
         <input type="checkbox" class="cfg" data-field="api_encryption"${c.api_encryption ? " checked" : ""}>
         <span>API-Verschlüsselung
           <span class="field-hint">
             Baut derzeit nicht — siehe „Der Verschlüsselungscode“ in der Doku.
           </span>
         </span>
       </label>
       <button type="button" class="save-config-btn">Firmware-Optionen speichern</button>
       <p class="config-result status" hidden></p>
     </div>`);

  // The summary carries the two things worth scanning a list for: what the
  // build is doing, and — once built — whether the room is occupied.
  const summaryLive = built
    ? `<span class="summary-live" data-summary-id="${device.id}">
         <span class="summary-dot"></span><span class="summary-live-text">…</span>
       </span>`
    : "";

  return `
    <details class="card device-card" data-id="${device.id}"${shouldOpen(device, deviceCount) ? " open" : ""}>
      <summary class="device-summary">
        <span class="device-summary-text">
          <span class="device-title">${escapeHtml(title)}</span>
          <span class="device-meta">${escapeHtml(c.name)} · ${escapeHtml(c.board)}${bandSuffix(c)} · ${escapeHtml(c.detection_algorithm)}</span>
        </span>
        <span class="device-summary-state">
          ${summaryLive}
          <span class="status ${statusClass(device.status)}">${statusLabel(device.status)}</span>
        </span>
      </summary>
      <div class="device-body">
        ${errorLine}
        ${otaLine}
        ${staleLine}
        <div class="device-actions">
          <button class="build-btn${built ? " btn-secondary" : ""}" ${canBuild ? "" : "disabled"}>${built ? "Neu bauen" : "Firmware bauen"}</button>
          ${repairBlock}
          ${flashBlock}
          <button class="delete-btn">Löschen</button>
        </div>
        ${liveBlock}
        ${detailSections}
        ${configEditor}
        ${logBlock}
      </div>
    </details>`;
}

function addressOf(card) {
  const input = card.querySelector(".address-input");
  return input ? input.value.trim() : "";
}

async function probeDevice(id, card) {
  const out = card.querySelector(".probe-result");
  const direct = card.querySelector(".probe-direct");
  const button = card.querySelector(".probe-btn");
  button.disabled = true;
  out.hidden = false;
  direct.hidden = true;
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
    // Port 62587 is a separate question from "can Home Assistant reach it",
    // and it is the one that decides whether the Calibration Lab has data.
    if (body.direct_message) {
      direct.className = `probe-direct status ${body.direct ? "status-ok" : "status-warn"}`;
      direct.textContent = body.direct_message;
      direct.hidden = false;
    }
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
    // The fallback access point's password lives in the same section:
    // it is needed at exactly the moment the device is unreachable, so
    // it has to be readable before that happens.
    const fallback = card.querySelector(".fallback-password");
    if (fallback) fallback.textContent = body.fallback_password || "—";
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
    ? devices.map((device) => renderDevice(device, devices.length)).join("")
    : '<p class="status status-pending">Noch keine Geräte — lege oben eines an.</p>';

  // With devices present, creating another one is the rare case, so the
  // form folds away; with none, it is the only thing to do.
  const createCard = document.getElementById("create-device");
  if (createCard && !createCard.dataset.touched) createCard.open = devices.length === 0;
  if (createCard && !createCard.dataset.wired) {
    createCard.dataset.wired = "1";
    createCard.querySelector("summary").addEventListener("click", () => {
      createCard.dataset.touched = "1";
    });
  }

  for (const el of list.querySelectorAll(".device-card")) {
    const id = el.dataset.id;

    el.addEventListener("toggle", () => {
      if (el.open) openCards.add(id);
      else openCards.delete(id);
    });
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

    const healthBtn = el.querySelector(".health-btn");
    if (healthBtn) healthBtn.addEventListener("click", () => runDiagnosis(id, el));

    const saveEntitiesBtn = el.querySelector(".save-entities-btn");
    if (saveEntitiesBtn) saveEntitiesBtn.addEventListener("click", () => saveEntityIds(id, el));

    const saveConfigBtn = el.querySelector(".save-config-btn");
    if (saveConfigBtn) saveConfigBtn.addEventListener("click", () => saveConfig(id, el));

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
  // The same reading, shown once in the open card and once on the closed
  // summary line — otherwise a collapsed list says nothing about the rooms.
  const summary = document.querySelector(`.summary-live[data-summary-id="${id}"]`);
  const setSummary = (stateName, text) => {
    if (!summary) return;
    summary.querySelector(".summary-dot").dataset.state = stateName;
    summary.querySelector(".summary-live-text").textContent = text;
  };

  if (!state.available) {
    motionEl.textContent = "nicht verfügbar";
    motionEl.className = "live-motion status status-warn";
    scoreEl.textContent = "—";
    setSummary("warn", "nicht verfügbar");
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
  setSummary(
    state.motion ? "on" : "off",
    state.motion ? "Bewegung" : "frei",
  );
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

// The "your change has not reached the chip yet" line, added or removed
// without redrawing the card around it.
function markStale(cardEl, stale) {
  const existing = cardEl.querySelector(".stale-line");
  if (!stale) {
    if (existing) existing.remove();
    return;
  }
  if (existing) return;
  const body = cardEl.querySelector(".device-body");
  const line = document.createElement("p");
  line.className = "stale-line status status-warn";
  line.textContent =
    "Umkonfiguriert, aber noch nicht neu gebaut — auf dem Gerät läuft weiter "
    + "das zuletzt geflashte Image.";
  body.insertBefore(line, body.firstElementChild);
}

async function saveConfig(id, cardEl) {
  const out = cardEl.querySelector(".config-result");
  const button = cardEl.querySelector(".save-config-btn");
  const patch = {};
  for (const field of cardEl.querySelectorAll(".cfg")) {
    const name = field.dataset.field;
    if (field.type === "checkbox") {
      patch[name] = field.checked;
    } else if (field.type === "number") {
      patch[name] = Number(field.value);
    } else if (name === "wifi_password") {
      // Empty means "leave it alone": the API never hands the real one
      // back, so sending the placeholder would overwrite a working
      // password with asterisks.
      if (field.value) patch[name] = field.value;
    } else {
      patch[name] = field.value;
    }
  }

  button.disabled = true;
  out.hidden = false;
  out.className = "config-result status status-pending";
  out.textContent = "Wird gespeichert…";
  try {
    const res = await fetch(`api/devices/${id}/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      out.className = "config-result status status-err";
      out.textContent = Array.isArray(body.detail)
        ? body.detail.map((d) => d.msg).join(" · ")
        : body.detail || "Speichern fehlgeschlagen";
      return;
    }
    out.className = "config-result status status-ok";
    out.textContent = "Gespeichert. Jetzt neu bauen und übertragen.";
    // Deliberately not a full reload: re-rendering the list replaces this
    // card, which collapses the section that was just used and throws
    // away the confirmation with it. Only two things on screen can have
    // changed, so both are updated in place.
    markStale(cardEl, body.firmware_behind_config);
    const meta = cardEl.querySelector(".device-meta");
    if (meta && body.config) {
      meta.textContent = `${body.config.name} · ${body.config.board}${bandSuffix(body.config)} · ${body.config.detection_algorithm}`;
    }
  } catch (err) {
    out.className = "config-result status status-err";
    out.textContent = "Backend nicht erreichbar";
  } finally {
    button.disabled = false;
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

// Web Serial needs a secure context. Where the page already has one, the
// warning is noise on every visit, and permanent noise is what stops
// warnings from being read at all.
if (!window.isSecureContext) {
  document.getElementById("web-serial-notice").hidden = false;
}

// Once the person opens or closes the create form themselves, stop
// deciding it for them on every reload.
//
// The listener is on the summary's click rather than the element's
// `toggle`, because `toggle` cannot tell a person apart from the line in
// loadDevices that opens the form when there are no devices — and it is
// fired asynchronously, so a flag set around that assignment would be
// gone by the time the event arrived. A click on the summary only ever
// comes from a person; the keyboard sends one too.

loadBoards();
loadPresets();
loadDevices();
setInterval(() => {
  // One switch in the header stops every poller on the page.
  if (!refreshPaused()) refreshAllLiveStates();
}, 5000);

// Flipping "cards open" in the header applies to the cards already drawn,
// rather than only to the next poll.
document.addEventListener("echolot:prefs", () => {
  const open = expandAllCards();
  for (const el of document.querySelectorAll(".device-card")) {
    if (open) el.open = true;
  }
});

// --- Diagnose ---------------------------------------------------------------
//
// Answers the question the rest of this page never could: is this device
// actually sensing? Three real ways it can look healthy and not be — a
// threshold belonging to the other detection profile, missing sensing
// entities from stale firmware, and CSI diagnostics that never published —
// each come back with the button that addresses them.

const SEVERITY_CLASS = { blocker: "status-err", warning: "status-warn", info: "status-pending" };
const SEVERITY_LABEL = { blocker: "Blockiert", warning: "Achtung", info: "Hinweis" };
const ACTION_LABEL = {
  recalibrate: "Neu kalibrieren",
  rebuild: "Firmware neu bauen",
  refresh_diagnostics: "Diagnosewerte abrufen",
};

function renderDiagnosis(card, body) {
  const box = card.querySelector(".health-result");
  box.hidden = false;
  box.textContent = "";

  if (!body.findings.length) {
    const ok = document.createElement("p");
    ok.className = "status status-ok";
    ok.textContent = "Nichts zu beanstanden — das Gerät misst.";
    box.appendChild(ok);
  }

  for (const finding of body.findings) {
    const item = document.createElement("div");
    item.className = "health-finding";

    const head = document.createElement("strong");
    head.className = `status ${SEVERITY_CLASS[finding.severity] || "status-pending"}`;
    head.textContent = SEVERITY_LABEL[finding.severity] || finding.severity;
    item.appendChild(head);

    const text = document.createElement("p");
    text.textContent = finding.message;
    item.appendChild(text);

    if (finding.action && ACTION_LABEL[finding.action]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn-secondary health-action";
      button.dataset.action = finding.action;
      button.textContent = ACTION_LABEL[finding.action];
      item.appendChild(button);
    }
    box.appendChild(item);
  }

  // The numbers behind the verdict, so it can be checked rather than
  // believed. Only shown once something has actually reported.
  const measured = Object.entries(body.diagnostics || {}).filter(
    ([, value]) => value !== null && value !== "unknown" && value !== "unavailable",
  );
  if (measured.length) {
    const list = document.createElement("dl");
    list.className = "health-numbers";
    for (const [label, value] of measured) {
      const term = document.createElement("dt");
      term.textContent = label;
      const def = document.createElement("dd");
      def.textContent = value;
      list.append(term, def);
    }
    box.appendChild(list);
  }

  const note = document.createElement("p");
  note.className = "field-note";
  note.textContent = `${body.samples} Messwerte aus den letzten ${body.window_minutes} Minuten.`;
  box.appendChild(note);

  for (const button of box.querySelectorAll(".health-action")) {
    button.addEventListener("click", () => runHealthAction(card, button));
  }
}

async function runDiagnosis(id, card) {
  const button = card.querySelector(".health-btn");
  const box = card.querySelector(".health-result");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Prüft…";
  try {
    const res = await fetch(`api/devices/${id}/health`);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      box.hidden = false;
      box.textContent = "";
      const err = document.createElement("p");
      err.className = "status status-err";
      err.textContent = body.detail || "Diagnose fehlgeschlagen";
      box.appendChild(err);
      return;
    }
    card.dataset.deviceId = id;
    renderDiagnosis(card, body);
  } catch (err) {
    box.hidden = false;
    box.textContent = "";
    const offline = document.createElement("p");
    offline.className = "status status-err";
    offline.textContent = "Backend nicht erreichbar";
    box.appendChild(offline);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function runHealthAction(card, button) {
  const id = card.dataset.deviceId;
  const endpoints = {
    recalibrate: `api/devices/${id}/calibrate`,
    rebuild: `api/devices/${id}/build`,
    refresh_diagnostics: `api/devices/${id}/diagnostics/refresh`,
  };
  const url = endpoints[button.dataset.action];
  if (!url) return;

  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Läuft…";
  try {
    const res = await fetch(url, { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      button.textContent = body.detail || "Fehlgeschlagen";
      return;
    }
    // The device needs a moment to publish what was just asked of it;
    // re-running the diagnosis immediately would read the old values.
    button.textContent = "Erledigt — Diagnose neu stellen";
  } catch (err) {
    button.textContent = "Backend nicht erreichbar";
  } finally {
    button.disabled = false;
    setTimeout(() => {
      button.textContent = original;
    }, 6000);
  }
}
