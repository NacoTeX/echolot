// Alle API-Pfade sind relativ (ohne führenden Slash), damit sie unter dem
// Ingress-Token-Präfix von Home Assistant bleiben statt auf dessen Wurzel
// zu zeigen.

const OVERVIEW_REFRESH_MS = 10000;

// Shared by every script on the page. app.js is loaded first, so these are
// defined before devices.js, zones.js and dashboard.js run — each of which
// used to carry its own copy.
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

//: Seconds a person can read: "45 s", "3 min", "3 min 20 s".
function formatSeconds(seconds) {
  if (seconds >= 60) {
    const m = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    return rest ? `${m} min ${rest} s` : `${m} min`;
  }
  return `${Math.round(seconds)} s`;
}

function switchTab(name) {
  const btn = document.querySelector(`.tab-btn[data-tab="${name}"]`);
  if (btn) btn.click();
}

function renderZones(zones) {
  const el = document.getElementById("overview-zones");
  const section = document.getElementById("overview-zones-section");

  if (!zones.length) {
    section.hidden = false;
    el.innerHTML =
      '<p class="hint">Noch keine Zonen. Erst eine Zone macht aus einzelnen ' +
      'Geräten einen Anwesenheitszustand, den Home Assistant nutzen kann. ' +
      '<button class="link-btn" data-goto="zones">Zone anlegen</button></p>';
    return;
  }

  section.hidden = false;
  el.innerHTML = zones
    .map((z) => {
      // Three states, not two: "hält" is occupied but counting down, and
      // conflating it with "belegt" hides why a zone is still on.
      let label, cls;
      if (!z.device_count) {
        // A zone with no members is not "unavailable" — the cause is known
        // and different, and saying so points at the fix.
        label = "keine Geräte";
        cls = "zone-pill-unknown";
      } else if (z.pending) {
        // Die Auswertung ist eine eigene Schleife; eine gerade angelegte
        // Zone hat noch keine Runde hinter sich.
        label = "wird ausgewertet…";
        cls = "zone-pill-unknown";
      } else if (!z.available) {
        label = "nicht verfügbar";
        cls = "zone-pill-unknown";
      } else if (z.state === "holding") {
        label = `hält noch ${formatSeconds(z.hold_remaining)}`;
        cls = "zone-pill-holding";
      } else if (z.occupied) {
        label = "belegt";
        cls = "zone-pill-on";
      } else {
        label = "frei";
        cls = "zone-pill-off";
      }
      return `
        <button class="zone-pill ${cls}" data-goto="dashboard">
          <span class="zone-pill-name">${escapeHtml(z.name)}</span>
          <span class="zone-pill-state">${escapeHtml(label)}</span>
        </button>`;
    })
    .join("");
}

function renderProblems(problems) {
  const section = document.getElementById("overview-problems-section");
  const list = document.getElementById("overview-problems");
  if (!problems.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  list.innerHTML = problems
    .map(
      (p) => `
      <li class="problem">
        <span>${escapeHtml(p.message)}</span>
        <button class="link-btn" data-goto="${escapeHtml(p.tab)}">ansehen</button>
      </li>`
    )
    .join("");
}

function renderSystem(data) {
  const el = document.getElementById("overview-system");
  const rows = [];

  const d = data.devices;
  rows.push([
    "Geräte",
    d.total === 0
      ? "keine"
      : d.built === d.total
        ? plural(d.total, "Gerät", "Geräte")
        : `${plural(d.total, "Gerät", "Geräte")}, davon ${d.built} gebaut`,
  ]);

  // Every device probes the air continuously, so the fleet total is the
  // number that matters for the household's Wi-Fi, not the per-device one.
  if (d.total) {
    rows.push(["Funklast", `≈ ${data.radio_load_kb_per_second.toFixed(1)} KB/s insgesamt`]);
  }

  rows.push([
    "Zonen in Home Assistant",
    !data.mqtt.wanted
      ? "Export abgeschaltet"
      : data.mqtt.connected
        ? "werden exportiert"
        : "kein MQTT-Broker erreichbar",
  ]);

  // `esphome version` prints "Version: 2026.6.5"; with "ESPHome" already
  // as the term, the prefix reads as a stutter.
  const esphomeVersion = (data.esphome.version || "").replace(/^Version:\s*/i, "");
  rows.push([
    "ESPHome",
    data.esphome.available ? esphomeVersion || "installiert" : "nicht verfügbar",
  ]);

  el.innerHTML = rows
    .map(([term, value]) => `<dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd>`)
    .join("");
}

async function loadOverview() {
  const loading = document.getElementById("overview-loading");
  const empty = document.getElementById("overview-empty");
  const body = document.getElementById("overview-body");

  let data;
  try {
    const res = await fetch("api/overview");
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch (err) {
    loading.hidden = false;
    loading.textContent = "Backend nicht erreichbar";
    loading.className = "status status-err";
    body.hidden = true;
    empty.hidden = true;
    markStatusUnreachable();
    return;
  }

  // The chip is on every tab, so it is updated before the overview panel
  // decides whether it has anything to show.
  renderStatusChip(data.problems);

  loading.hidden = true;

  // With nothing set up, a status report has nothing to report — the
  // useful thing to show is the way in.
  const fresh = data.devices.total === 0 && data.zones.length === 0;
  empty.hidden = !fresh;
  body.hidden = fresh;
  if (fresh) return;

  renderZones(data.zones);
  renderProblems(data.problems);
  renderSystem(data);
}

// Any element can ask for a tab switch; that keeps the setup steps and the
// problem list from each needing their own wiring.
document.addEventListener("click", (evt) => {
  const target = evt.target.closest("[data-goto]");
  if (target) switchTab(target.dataset.goto);
});

for (const btn of document.querySelectorAll(".tab-btn")) {
  btn.addEventListener("click", () => {
    for (const b of document.querySelectorAll(".tab-btn")) b.classList.remove("active");
    btn.classList.add("active");
    for (const panel of document.querySelectorAll(".tab-panel")) panel.hidden = true;
    document.getElementById(`tab-${btn.dataset.tab}`).hidden = false;
    if (btn.dataset.tab === "overview") loadOverview();
  });
}

// --- View preferences -------------------------------------------------
//
// Two switches, both about reading rather than about the system: pausing
// the refresh so a log or a number stays put while you read it, and
// keeping device cards open. They live in localStorage because they are
// per-browser habits, not configuration the add-on should carry.

const PREFS = { pause: "echolot.pause", expand: "echolot.expandCards" };

function readPref(key) {
  try {
    return localStorage.getItem(key) === "1";
  } catch (err) {
    // Private windows and blocked site data throw on access rather than
    // returning null, and a preference is never worth a broken page.
    return false;
  }
}

function writePref(key, value) {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch (err) {
    /* nothing to do — the switch still works for this page view */
  }
}

//: Read by every poller on the page, so one switch stops all of them.
function refreshPaused() {
  return readPref(PREFS.pause);
}

// --- Header status chip -----------------------------------------------

//: How the overall state is worded. A count beats a colour: "3 Hinweise"
//: says what to expect before the panel is even open.
function chipState(problems) {
  if (!problems.length) return { state: "ok", text: "läuft" };
  return {
    state: "err",
    text: problems.length === 1 ? "1 Hinweis" : `${problems.length} Hinweise`,
  };
}

function renderStatusChip(problems) {
  const dot = document.querySelector("#status-chip .status-dot");
  const text = document.getElementById("status-chip-text");
  const { state, text: label } = chipState(problems);
  dot.dataset.state = state;
  text.textContent = label;

  const list = document.getElementById("status-panel-problems");
  list.innerHTML = problems.length
    ? problems
        .map(
          (p) =>
            `<button type="button" class="status-panel-problem" data-goto="${escapeHtml(p.tab)}">` +
            `${escapeHtml(p.message)}</button>`,
        )
        .join("")
    : '<p class="hint">Keine offenen Hinweise.</p>';
}

function markStatusUnreachable() {
  const dot = document.querySelector("#status-chip .status-dot");
  dot.dataset.state = "warn";
  document.getElementById("status-chip-text").textContent = "kein Backend";
  document.getElementById("status-panel-problems").innerHTML =
    '<p class="status status-err">Das Add-on antwortet nicht.</p>';
}

const chipButton = document.getElementById("status-chip");
const chipPanel = document.getElementById("status-panel");

function closeStatusPanel() {
  chipPanel.hidden = true;
  chipButton.setAttribute("aria-expanded", "false");
}

chipButton.addEventListener("click", (evt) => {
  evt.stopPropagation();
  const open = chipPanel.hidden;
  chipPanel.hidden = !open;
  chipButton.setAttribute("aria-expanded", String(open));
});

// Clicking anywhere else closes it — including a problem entry, which also
// switches tab through the shared [data-goto] handler above.
document.addEventListener("click", (evt) => {
  if (!chipPanel.hidden && !chipPanel.contains(evt.target)) closeStatusPanel();
});
document.addEventListener("keydown", (evt) => {
  if (evt.key === "Escape" && !chipPanel.hidden) {
    closeStatusPanel();
    chipButton.focus();
  }
});
chipPanel.addEventListener("click", (evt) => {
  if (evt.target.closest("[data-goto]")) closeStatusPanel();
});

for (const [id, key] of [["pref-pause", PREFS.pause], ["pref-expand", PREFS.expand]]) {
  const box = document.getElementById(id);
  box.checked = readPref(key);
  box.addEventListener("change", () => {
    writePref(key, box.checked);
    document.dispatchEvent(new CustomEvent("echolot:prefs"));
  });
}

// --- Polling ----------------------------------------------------------

//: The overview tab wants fresh numbers every ten seconds; the chip is
//: content with half a minute, and asking more often would cost Home
//: Assistant requests for a single word.
const CHIP_EVERY_N_TICKS = 3;
let tick = 0;

loadOverview();
setInterval(() => {
  if (refreshPaused()) return;
  const visible = !document.getElementById("tab-overview").hidden;
  tick += 1;
  // The overview renders from the same response, so a visible tab needs
  // no second request for the chip.
  if (visible || tick % CHIP_EVERY_N_TICKS === 0) loadOverview();
}, OVERVIEW_REFRESH_MS);
