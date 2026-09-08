// Zonen: Geräte gruppieren und die Präsenz-Logik der Zone abstimmen.
//
// Die Rohaggregation ist weiterhin ODER über die Mitglieder; Haltezeit und
// Hysterese sitzen im Backend dahinter (app/zone_logic.py). Hier geht es
// nur darum, sie einstellbar und ihren Zustand sichtbar zu machen.
//
// Alle API-Pfade sind relativ (ohne führenden Slash) — siehe app.js.

// Leere Zahlenfelder heißen "nicht gesetzt", nicht "0". Ein leeres Feld als
// 0 zu senden würde eine Schwelle setzen, die alles durchlässt.
function numberOrNull(input) {
  const raw = input.value.trim();
  return raw === "" ? null : Number(raw);
}

function tuningSummary(zone) {
  const parts = [];
  if (zone.hold_seconds > 0) parts.push(`Haltezeit ${formatSeconds(zone.hold_seconds)}`);
  if (zone.enter_threshold !== null && zone.enter_threshold !== undefined) {
    parts.push(
      zone.exit_threshold !== null && zone.exit_threshold !== undefined
        ? `Schwelle ${zone.enter_threshold} / ${zone.exit_threshold}`
        : `Schwelle ${zone.enter_threshold}`
    );
  }
  return parts;
}

async function loadZoneDevicePicker() {
  const picker = document.getElementById("zone-device-picker");
  let devices;
  try {
    devices = await (await fetch("api/devices")).json();
  } catch (err) {
    picker.innerHTML = '<p class="status status-err">Geräte konnten nicht geladen werden</p>';
    return;
  }
  picker.innerHTML = devices.length
    ? devices
        .map(
          (d) => `
        <label class="zone-device-option">
          <input type="checkbox" name="device_ids" value="${d.id}">
          ${escapeHtml(d.config.friendly_name || d.config.name)}
        </label>`
        )
        .join("")
    : '<p class="status status-pending">Noch keine Geräte — lege zuerst im Tab „Geräte“ eines an.</p>';
}

function renderZone(zone) {
  const memberChips = zone.device_ids.length
    ? zone.device_ids.map((id) => `<span class="chip" data-member="${id}">…</span>`).join("")
    : '<span class="status status-pending">Keine Geräte in dieser Zone</span>';

  const tuning = tuningSummary(zone);
  const tuningRow = tuning.length
    ? `<div class="zone-tuning">${tuning.map((t) => `<span class="chip chip-quiet">${escapeHtml(t)}</span>`).join("")}</div>`
    : "";

  return `
    <div class="card zone-card" data-id="${zone.id}">
      <div class="device-card-header">
        <h3>${escapeHtml(zone.name)}</h3>
        <span class="status status-pending zone-status" data-zone-status="${zone.id}">wird geprüft…</span>
      </div>
      <div class="zone-members">${memberChips}</div>
      ${tuningRow}
      <div class="device-actions">
        <button class="tune-zone-btn">Abstimmen</button>
        <button class="delete-zone-btn">Löschen</button>
      </div>
      <details class="device-section zone-timeline">
        <summary>Verlauf — warum die Zone tut, was sie tut</summary>
        <div class="device-section-body">
          <div class="timeline-body"><p class="hint">wird geladen…</p></div>
        </div>
      </details>
    </div>`;
}

async function loadZones() {
  const list = document.getElementById("zone-list");
  let zoneList;
  try {
    zoneList = await (await fetch("api/zones")).json();
  } catch (err) {
    list.innerHTML = '<p class="status status-err">Zonen konnten nicht geladen werden</p>';
    return;
  }

  list.innerHTML = zoneList.length
    ? zoneList.map(renderZone).join("")
    : '<p class="status status-pending">Noch keine Zonen — lege oben eine an.</p>';

  const byId = new Map(zoneList.map((z) => [z.id, z]));
  for (const el of list.querySelectorAll(".zone-card")) {
    const id = el.dataset.id;
    el.querySelector(".delete-zone-btn").addEventListener("click", () => deleteZone(id));
    el.querySelector(".tune-zone-btn").addEventListener("click", () => tuneZone(byId.get(id)));
    // Loaded when the section is opened rather than with the list: most
    // visits never ask, and the answer is only interesting after
    // something surprised you.
    const history = el.querySelector(".zone-timeline");
    history.addEventListener("toggle", () => {
      if (history.open) loadTimeline(id, history.querySelector(".timeline-body"));
    });
    refreshZoneState(id);
  }
}

//: The transitions, newest first, each with the sentence the backend
//: wrote for it — the reason lives next to the state machine that knows
//: it, not in a second copy over here.
async function loadTimeline(id, target) {
  target.innerHTML = '<p class="hint">wird geladen…</p>';
  try {
    const body = await (await fetch(`api/zones/${id}/timeline?limit=25`)).json();
    if (!body.events.length) {
      target.innerHTML =
        '<p class="hint">Noch keine Übergänge aufgezeichnet. Der Verlauf liegt ' +
        'im Arbeitsspeicher und beginnt nach jedem Neustart des Add-ons von vorn.</p>';
      return;
    }
    target.innerHTML = `<ul class="timeline-list">${body.events
      .map((event) => {
        const when = new Date(event.t * 1000).toLocaleTimeString("de-DE");
        const who = event.members
          .filter((member) => member.motion)
          .map((member) => member.name)
          .join(", ");
        return `<li>
            <span class="timeline-when">${escapeHtml(when)}</span>
            <span class="timeline-what">${escapeHtml(event.explanation)}</span>
            ${who ? `<span class="timeline-who">${escapeHtml(who)}</span>` : ""}
          </li>`;
      })
      .join("")}</ul>`;
  } catch (err) {
    target.innerHTML = '<p class="status status-err">Verlauf nicht abrufbar</p>';
  }
}

async function refreshZoneState(id) {
  const statusEl = document.querySelector(`[data-zone-status="${id}"]`);
  if (!statusEl) return;
  let state;
  try {
    state = await (await fetch(`api/zones/${id}/state`)).json();
  } catch (err) {
    statusEl.textContent = "nicht erreichbar";
    statusEl.className = "status status-err zone-status";
    return;
  }

  if (!state.available) {
    statusEl.textContent = "nicht verfügbar";
    statusEl.className = "status status-warn zone-status";
  } else if (state.state === "holding") {
    // Der Countdown ist der interessante Teil: er erklärt, warum die Zone
    // noch belegt ist, obwohl gerade nichts mehr gemessen wird.
    statusEl.textContent = `hält noch ${formatSeconds(state.hold_remaining)}`;
    statusEl.className = "status status-warn zone-status";
  } else {
    statusEl.textContent = state.occupied ? "belegt" : "frei";
    statusEl.className = `status zone-status ${state.occupied ? "status-ok" : "status-pending"}`;
  }

  const card = statusEl.closest(".zone-card");
  for (const member of state.members) {
    const chip = card.querySelector(`[data-member="${member.device_id}"]`);
    if (!chip) continue;
    const label = member.name || member.device_id.slice(0, 8);
    const flag = member.available ? (member.motion ? "●" : "○") : "?";
    chip.textContent = `${flag} ${label}`;
    chip.title = member.available ? "" : member.error || "nicht verfügbar";
  }
}

function refreshAllZoneStates() {
  for (const el of document.querySelectorAll(".zone-card")) {
    refreshZoneState(el.dataset.id);
  }
}

// Abstimmen läuft bewusst über prompt() statt über ein zweites Formular:
// drei Zahlen, die man selten anfasst, rechtfertigen keinen Dialog-Aufbau
// samt eigenem Fokus- und Escape-Handling.
async function tuneZone(zone) {
  if (!zone) return;

  const holdRaw = prompt(
    `Haltezeit für „${zone.name}“ in Sekunden.\n\n` +
      "Wie lange die Zone nach der letzten Bewegung noch als belegt gilt.\n" +
      "0 schaltet die Haltezeit ab.",
    String(zone.hold_seconds ?? 0)
  );
  if (holdRaw === null) return;

  const enterRaw = prompt(
    "Einschalt-Schwellwert (leer = Schwelle des Geräts nutzen).",
    zone.enter_threshold ?? ""
  );
  if (enterRaw === null) return;

  let exitRaw = "";
  if (enterRaw.trim() !== "") {
    const answer = prompt(
      "Ausschalt-Schwellwert (leer = gleicher Wert).\n\n" +
        "Ein niedrigerer Wert erzeugt eine Hysterese — dazwischen behält\n" +
        "die Zone ihren Zustand und flackert nicht.",
      zone.exit_threshold ?? ""
    );
    if (answer === null) return;
    exitRaw = answer;
  }

  const payload = {
    hold_seconds: Number(holdRaw.trim() || 0),
    enter_threshold: enterRaw.trim() === "" ? null : Number(enterRaw),
    exit_threshold: exitRaw.trim() === "" ? null : Number(exitRaw),
  };

  const res = await fetch(`api/zones/${zone.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = Array.isArray(body.detail)
      ? body.detail.map((e) => e.msg).join("; ")
      : body.detail || "Zone konnte nicht abgestimmt werden";
    alert(detail);
    return;
  }
  await loadZones();
}

async function deleteZone(id) {
  if (!confirm("Diese Zone löschen?")) return;
  await fetch(`api/zones/${id}`, { method: "DELETE" });
  await loadZones();
}

document.getElementById("zone-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const errorEl = document.getElementById("zone-form-error");
  errorEl.hidden = true;

  const form = evt.target;
  const name = form.elements.name.value;
  const device_ids = Array.from(form.querySelectorAll('input[name="device_ids"]:checked')).map((el) => el.value);
  const payload = {
    name,
    device_ids,
    hold_seconds: Number(form.elements.hold_seconds.value.trim() || 0),
    enter_threshold: numberOrNull(form.elements.enter_threshold),
    exit_threshold: numberOrNull(form.elements.exit_threshold),
  };

  try {
    const res = await fetch("api/zones", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const detail = Array.isArray(body.detail) ? body.detail.map((e) => e.msg).join("; ") : body.detail || "Zone konnte nicht angelegt werden";
      errorEl.textContent = detail;
      errorEl.hidden = false;
      return;
    }
    form.reset();
    await loadZones();
  } catch (err) {
    errorEl.textContent = "Backend nicht erreichbar";
    errorEl.hidden = false;
  }
});

document.querySelector('.tab-btn[data-tab="zones"]').addEventListener("click", loadZoneDevicePicker);

loadZoneDevicePicker();
loadZones();
setInterval(refreshAllZoneStates, 5000);
