// Sensors: list, create, and one sheet per device for firmware, network,
// credentials and settings. Also the Wi-Fi CSI devices of earlier versions,
// which can be turned into radar nodes under the same identity.

(() => {
  const E = Echolot;
  const { escapeHtml, icon, api, toast, state } = E;

  let boards = [];
  async function loadBoards() {
    if (!boards.length) boards = await api("api/boards");
    return boards;
  }

  function slug(text) {
    let s = String(text || "").toLowerCase();
    for (const [a, b] of [["ä", "ae"], ["ö", "oe"], ["ü", "ue"], ["ß", "ss"]]) s = s.split(a).join(b);
    s = s.normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    return s.slice(0, 32).replace(/-+$/, "");
  }

  function firmwareChip(d) {
    if (d.status === "queued") return '<span class="chip warn">Build wartet</span>';
    if (d.status === "running") return '<span class="chip warn">wird gebaut…</span>';
    if (d.status === "error") return '<span class="chip err">Build fehlgeschlagen</span>';
    if (d.status !== "success") return '<span class="chip">nicht gebaut</span>';
    if (d.runs_radar_firmware === false) return '<span class="chip warn">noch CSI-Firmware</span>';
    if (d.firmware_behind_config) return '<span class="chip warn">Einstellungen nicht geflasht</span>';
    return '<span class="chip ok">Firmware gebaut</span>';
  }

  function linkChip(d) {
    const l = d.link;
    if (!l) return '<span class="chip">keine Verbindung</span>';
    if (l.connected && l.fresh && l.link_state === "receiving") return '<span class="chip ok">Radar empfängt</span>';
    if (l.connected && l.link_state === "quiet") return '<span class="chip ok">verbunden · still</span>';
    if (l.connected && l.no_frame_entity) return '<span class="chip warn">falsche Firmware</span>';
    if (l.connected) return '<span class="chip warn">verbunden · Radar schweigt</span>';
    return '<span class="chip err">offline</span>';
  }

  function field(label, inner, hint = "") {
    return `<label class="field"><span>${label}</span>${inner}${hint ? `<p class="hint">${hint}</p>` : ""}</label>`;
  }

  // ---------------------------------------------------------------- list

  const view = {
    async mount(el, params) {
      this.el = el;
      this.sheet = null;
      this.render();
      await loadBoards().catch(() => {});
      if (params.action === "new") this.openCreate();
      if (params.open) this.openDevice(params.open);
    },
    unmount() { if (this.sheet) { const s = this.sheet; this.sheet = null; s.close(); } },
    onData() {
      this.render();
      if (this.sheet && this.sheet.refresh) this.sheet.refresh();
    },
    render() {
      const devices = state.devices;
      this.el.innerHTML = `
        <div class="page-head">
          <div><div class="eyebrow">Einrichtung</div><h1>Sensoren</h1>
            <p class="sub">ESP32 mit HLK-LD2460 — anlegen, Firmware bauen, flashen.</p></div>
          <div class="head-actions"><button class="btn primary" id="add-device">${icon("plus")}Sensor anlegen</button></div>
        </div>
        ${devices.length ? "" : `<div class="card pad empty-state" style="margin-bottom:16px">
          <p>Noch kein Sensor. Du brauchst einen ESP32 (etwa den Waveshare ESP32-C5-Zero) und ein HLK-LD2460:
          ESP-TX an Pin 8 (Rx2), ESP-RX an Pin 7 (Tx2), 5 V und GND.</p></div>`}
        <div class="device-grid">
          ${devices.map((d) => `
            <button class="card device-tile" data-open="${escapeHtml(d.id)}">
              <div class="head"><div class="device-glyph">${icon("radar")}</div>
                <div><div class="title">${escapeHtml(d.config.friendly_name || d.config.name)}</div>
                  <div class="meta">${escapeHtml(d.config.name)} · ${escapeHtml((boards.find((b) => b.key === d.config.board) || {}).label || d.config.board)}</div></div></div>
              <div class="chips">${firmwareChip(d)}${linkChip(d)}${d.room ? `<span class="chip plain">${escapeHtml(d.room.name)}</span>` : '<span class="chip plain">keinem Raum zugeordnet</span>'}</div>
            </button>`).join("")}
        </div>
        ${state.legacy.length ? `
          <h2 style="margin-top:30px">Aus früheren Versionen</h2>
          <p class="hint" style="margin-bottom:14px">WLAN-CSI-Geräte. Echolot 1.0 baut keine CSI-Firmware mehr. Die Einträge
            sind unverändert gespeichert. Umstellen behält Kennung, Namen, WLAN und alle Zugangsdaten; danach ein LD2460
            anschließen und die neue Firmware aufspielen — per WLAN, das OTA-Passwort bleibt dasselbe.</p>
          <div class="device-grid">${state.legacy.map((d) => `
            <div class="card device-tile legacy" style="cursor:default">
              <div class="head"><div class="device-glyph">${icon("wifi")}</div>
                <div><div class="title">${escapeHtml(d.friendly_name || d.name)}</div>
                  <div class="meta">${escapeHtml(d.name)} · ${escapeHtml(d.board_label)} · WLAN-CSI</div></div></div>
              <div class="actions">
                <button class="btn primary small" data-convert="${escapeHtml(d.id)}" ${d.convertible ? "" : "disabled"}>Auf Radar umstellen</button>
                <button class="btn danger small" data-drop="${escapeHtml(d.id)}">Entfernen</button>
              </div>
            </div>`).join("")}</div>` : ""}`;
      this.el.querySelector("#add-device").addEventListener("click", () => this.openCreate());
      this.el.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => this.openDevice(b.dataset.open)));
      this.el.querySelectorAll("[data-convert]").forEach((b) => b.addEventListener("click", () => this.convert(b.dataset.convert)));
      this.el.querySelectorAll("[data-drop]").forEach((b) => b.addEventListener("click", () => this.dropLegacy(b.dataset.drop)));
    },

    async convert(id) {
      const d = state.legacy.find((x) => x.id === id);
      const ok = await E.confirmDialog({
        title: `„${d.friendly_name || d.name}“ auf Radar umstellen?`,
        text: "Kennung, Knotenname, WLAN, API-Schlüssel und OTA-Passwort bleiben. Die CSI-Einstellungen werden als Datei im Geräteordner aufbewahrt. Danach Firmware bauen und aufspielen.",
        confirm: "Umstellen",
      });
      if (!ok) return;
      try {
        const device = await api(`api/legacy-devices/${encodeURIComponent(id)}/convert`, { method: "POST", body: {} });
        await E.refresh();
        toast("Umgestellt. Jetzt Firmware bauen.");
        this.openDevice(device.id);
      } catch (err) { toast(err.message, "err"); }
    },

    async dropLegacy(id) {
      const d = state.legacy.find((x) => x.id === id);
      const ok = await E.confirmDialog({ title: `„${d.friendly_name || d.name}“ entfernen?`,
        text: "Der Eintrag verschwindet aus der Liste. Eine Kopie bleibt als csi_record.json im Geräteordner unter /data.",
        confirm: "Entfernen", danger: true });
      if (!ok) return;
      try { await api(`api/legacy-devices/${encodeURIComponent(id)}`, { method: "DELETE" }); await E.refresh(); }
      catch (err) { toast(err.message, "err"); }
    },

    // -------------------------------------------------------- create

    async openCreate() {
      await loadBoards();
      const sheet = E.openSheet("Sensor anlegen", { onClose: () => { this.sheet = null; if (location.hash.startsWith("#/devices/")) history.replaceState(null, "", "#/devices"); } });
      this.sheet = sheet;
      sheet.body.innerHTML = `
        <form id="create" class="card">
          ${field("Name", '<input name="friendly_name" required maxlength="40" placeholder="z. B. Wohnzimmer-Radar" autofocus>',
            'Knotenname in ESPHome: <b class="mono" id="node-name">—</b>')}
          ${field("Board", `<select name="board">${boards.map((b) => `<option value="${b.key}" ${b.key === "esp32c5" ? "selected" : ""}>${escapeHtml(b.label)}</option>`).join("")}</select>`)}
          <button type="button" class="btn small" id="waveshare">Waveshare ESP32-C5-Zero übernehmen</button>
          <p class="hint" style="margin:6px 0 14px">Setzt TX GPIO11, RX GPIO12 und hält GPIO26 low (Onboard-Antenne) — die Belegung des Prototyps.</p>
          <div class="row2">
            ${field("WLAN-Name", '<input name="wifi_ssid" required maxlength="32" autocomplete="off">')}
            ${field("WLAN-Passwort", '<input name="wifi_password" type="password" maxlength="64" autocomplete="new-password">')}
          </div>
          <div id="band-field">${field("WLAN-Band", `<select name="wifi_band"><option value="2.4GHz">2,4 GHz</option><option value="5GHz">5 GHz</option><option value="auto">automatisch</option></select>`,
            "Nur der ESP32-C5 hat zwei Bänder. Fürs Radar ist es egal — es entscheidet nur, welches Funknetz die Daten trägt.")}</div>
          <div class="row3">
            ${field("TX-Pin", '<input name="radar_tx_pin" type="number" min="0" max="56" inputmode="numeric">', "an Pin 8 (Rx2)")}
            ${field("RX-Pin", '<input name="radar_rx_pin" type="number" min="0" max="56" inputmode="numeric">', "an Pin 7 (Tx2)")}
            ${field("Antennen-Pin", '<input name="antenna_select_pin" type="number" min="0" max="56" placeholder="—" inputmode="numeric">', "optional, low")}
          </div>
          <details class="more"><summary>Weitere Optionen</summary>
            <label class="check"><input type="checkbox" name="radar_quiet_means_empty"><span>Stille = leerer Raum
              <small>Antwortet das Modul, meldet aber nichts, gilt der Raum als leer statt als unbekannt. Erst einschalten, wenn du beobachtet hast, dass dein Modul im leeren Raum verstummt.</small></span></label>
            <label class="check"><input type="checkbox" name="diagnostics" checked><span>Diagnose-Entitäten<small>WLAN-Signal, Laufzeit, Temperatur, Byte- und Meldungszähler, Neustart-Knopf.</small></span></label>
            <label class="check"><input type="checkbox" name="web_server" checked><span>Statusseite auf dem Gerät<small>http://&lt;ip&gt;/ — nützlich auf dem iPad, wo es kein Web Serial gibt.</small></span></label>
            ${field("Log-Level", `<select name="log_level">${["INFO", "DEBUG", "WARN", "ERROR", "VERBOSE", "NONE"].map((l) => `<option>${l}</option>`).join("")}</select>`)}
            ${field("BSSID (optional)", '<input name="wifi_bssid" placeholder="AA:BB:CC:DD:EE:FF">', "Nur um an einen bestimmten Access Point zu binden.")}
          </details>
          <div class="actions" style="margin-top:14px"><button class="btn primary" type="submit">${icon("check")}Anlegen</button></div>
        </form>`;
      const form = sheet.body.querySelector("#create");
      const nodeName = sheet.body.querySelector("#node-name");
      const applyBoard = () => {
        const b = boards.find((x) => x.key === form.board.value);
        sheet.body.querySelector("#band-field").hidden = !(b && b.dual_band);
        if (b && b.radar_uart_pins) { form.radar_tx_pin.value = b.radar_uart_pins[0]; form.radar_rx_pin.value = b.radar_uart_pins[1]; }
        if (!b || !b.dual_band) form.wifi_band.value = "2.4GHz";
      };
      form.board.addEventListener("change", applyBoard);
      form.friendly_name.addEventListener("input", () => { nodeName.textContent = slug(form.friendly_name.value) || "—"; });
      sheet.body.querySelector("#waveshare").addEventListener("click", () => {
        form.board.value = "esp32c5"; applyBoard();
        form.radar_tx_pin.value = 11; form.radar_rx_pin.value = 12; form.antenna_select_pin.value = 26;
        toast("Waveshare-Belegung übernommen.");
      });
      applyBoard();
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const n = (v) => (v === "" || v === null ? null : Number(v));
        const body = {
          name: slug(form.friendly_name.value),
          friendly_name: form.friendly_name.value.trim(),
          board: form.board.value,
          wifi_ssid: form.wifi_ssid.value,
          wifi_password: form.wifi_password.value,
          wifi_bssid: form.wifi_bssid.value.trim() || null,
          wifi_band: sheet.body.querySelector("#band-field").hidden ? "2.4GHz" : form.wifi_band.value,
          radar_tx_pin: n(form.radar_tx_pin.value),
          radar_rx_pin: n(form.radar_rx_pin.value),
          antenna_select_pin: n(form.antenna_select_pin.value),
          radar_quiet_means_empty: form.radar_quiet_means_empty.checked,
          diagnostics: form.diagnostics.checked,
          web_server: form.web_server.checked,
          log_level: form.log_level.value,
        };
        if (!body.name) { toast("Der Name braucht mindestens einen Buchstaben oder eine Ziffer.", "err"); return; }
        try {
          const device = await api("api/devices", { method: "POST", body });
          await E.refresh();
          sheet.close();
          this.openDevice(device.id);
          toast("Sensor angelegt. Als Nächstes die Firmware bauen.");
        } catch (err) { toast(err.message, "err"); }
      });
    },

    // -------------------------------------------------------- device

    openDevice(id) {
      const device = state.devices.find((d) => d.id === id);
      if (!device) { toast("Diesen Sensor gibt es nicht mehr.", "err"); return; }
      if (this.sheet) this.sheet.close();
      const sheet = E.openSheet(device.config.friendly_name || device.config.name, {
        onClose: () => { clearInterval(fast); this.sheet = null; if (location.hash.startsWith("#/device/")) history.replaceState(null, "", "#/devices"); },
      });
      this.sheet = sheet;
      let lastJson = "";
      sheet.refresh = () => {
        const d = state.devices.find((x) => x.id === id);
        if (!d) { sheet.close(); return; }
        const json = JSON.stringify({ ...d, link: null });
        if (json === lastJson) { this.updateLive(sheet, d); return; }
        const active = document.activeElement;
        if (active && sheet.body.contains(active) && active.matches("input, select, textarea")) { this.updateLive(sheet, d); return; }
        lastJson = json;
        this.renderDevice(sheet, d);
      };
      // Build and OTA runs change the record for minutes; poll faster
      // while the sheet is open than the page does.
      const fast = setInterval(() => {
        const d = state.devices.find((x) => x.id === id);
        if (d && ["queued", "running"].includes(d.status) || d && ["queued", "running"].includes(d.ota_status)) E.refresh();
      }, 2000);
      sheet.refresh();
    },

    updateLive(sheet, d) {
      const box = sheet.body.querySelector("#live-lines");
      if (box) box.innerHTML = this.liveLines(d);
      const chips = sheet.body.querySelector("#dev-chips");
      if (chips) chips.innerHTML = this.chips(d);
    },

    chips(d) {
      return `${firmwareChip(d)}${linkChip(d)}${d.room ? `<a class="chip plain" href="#/room/${encodeURIComponent(d.room.id)}">${escapeHtml(d.room.name)}</a>` : ""}`;
    },

    liveLines(d) {
      const l = d.link || {};
      const states = { receiving: "empfängt Meldungen", quiet: "antwortet, meldet nichts", unknown: "Modul antwortet nicht" };
      const rows = [
        ["Verbindung", l.connected ? '<span class="chip ok">verbunden</span>' : `<span class="chip err">getrennt</span>`],
        ["Radarmodul", l.link_state ? escapeHtml(states[l.link_state] || l.link_state) : "—"],
        ["Letzte Meldung", l.frame_age_s !== null && l.frame_age_s !== undefined ? `vor ${E.formatNumber(l.frame_age_s, 1)} s` : "—"],
        ["WLAN-Signal", l.wifi_signal !== null && l.wifi_signal !== undefined ? `${Math.round(l.wifi_signal)} dBm` : "—"],
        ["Modul-Firmware", l.firmware ? escapeHtml(l.firmware) : "—"],
        ["Adresse", escapeHtml(l.address || d.address || `${d.config.name}.local`)],
      ];
      return rows.map(([a, b]) => `<div class="stat-line"><span>${a}</span><span>${b}</span></div>`).join("")
        + (l.error ? `<p class="hint" style="margin-top:8px">${escapeHtml(l.error)}</p>` : "");
    },

    renderDevice(sheet, d) {
      const c = d.config;
      const board = boards.find((b) => b.key === c.board) || { label: c.board, dual_band: false };
      const built = d.status === "success" && d.firmware_bin;
      const busy = ["queued", "running"].includes(d.status) || ["queued", "running"].includes(d.ota_status);
      const size = d.firmware_size ? ` (${(d.firmware_size / 1048576).toFixed(1)} MB)` : "";
      sheet.head.textContent = c.friendly_name || c.name;
      sheet.body.innerHTML = `
        <div class="card"><div class="chips" id="dev-chips" style="margin-bottom:10px">${this.chips(d)}</div>
          <div class="stat-lines" id="live-lines">${this.liveLines(d)}</div></div>

        <div class="card device-card"><h2>Firmware</h2>
          ${d.runs_radar_firmware === false ? '<div class="notice warn"><div class="grow">Auf dem Chip läuft noch die WLAN-CSI-Firmware. LD2460 anschließen, Firmware bauen und aufspielen.</div></div>' : ""}
          ${d.firmware_behind_config ? '<div class="notice warn"><div class="grow">Die Einstellungen haben sich seit dem letzten Build geändert. Neu bauen und aufspielen, damit der Sensor sie kennt.</div></div>' : ""}
          ${d.status === "running" || d.status === "queued" ? `<p class="flash-progress busy-note">${d.status === "queued" ? "Wartet auf einen freien Build-Platz…" : "Firmware wird gebaut. Der erste Build lädt etwa 2 GB ESP-IDF und dauert 10–20 Minuten."}</p>` : ""}
          ${d.status === "error" ? `<div class="notice err"><div class="grow"><strong>Build fehlgeschlagen</strong>${escapeHtml(d.build_error || "")}</div></div>` : ""}
          <div class="actions">
            <button class="btn ${built ? "" : "primary"}" id="build" ${busy ? "disabled" : ""}>${built ? "Neu bauen" : "Firmware bauen"}</button>
            ${(d.build_error || "").includes("Toolchain") ? `<button class="btn" id="toolchain">Toolchain zurücksetzen</button>` : ""}
          </div>
          ${built ? `
            <h3 style="margin-top:18px">${icon("usb")} Über USB (erstes Mal)</h3>
            <div class="actions">
              <esp-web-install-button manifest="api/devices/${encodeURIComponent(d.id)}/manifest.json">
                <button slot="activate">Über USB flashen</button>
                <span slot="unsupported">Dieser Browser kann kein Web Serial — Chrome oder Edge am Computer nutzen, oder die Datei laden.</span>
                <span slot="not-allowed">Web Serial braucht HTTPS.</span>
              </esp-web-install-button>
              <a class="btn" href="api/devices/${encodeURIComponent(d.id)}/firmware.bin" download="${escapeHtml(c.name)}-firmware.bin">${icon("download")}Datei${escapeHtml(size)}</a>
            </div>
            <p class="flash-progress" data-flash hidden></p>
            <h3 style="margin-top:18px">${icon("wifi")} Über WLAN</h3>
            <div class="actions" style="flex-wrap:nowrap">
              <input class="grow" id="ota-address" value="${escapeHtml(d.address || "")}" placeholder="${escapeHtml(c.name)}.local oder IP"
                style="flex:1;min-width:0;min-height:40px;padding:8px 12px;border-radius:11px;border:1px solid var(--line-strong);background:var(--surface-2)">
              <button class="btn" id="ota" ${busy ? "disabled" : ""}>Aufspielen</button>
            </div>
            ${d.ota_status === "running" || d.ota_status === "queued" ? '<p class="flash-progress busy-note">Update über WLAN läuft…</p>' : ""}
            ${d.ota_error ? `<div class="notice err" style="margin-top:10px"><div class="grow">${escapeHtml(d.ota_error)}</div></div>` : ""}
            ${d.ota_last_success && !d.ota_error && d.ota_status === "success" ? `<p class="hint" style="margin-top:8px">Zuletzt per WLAN aktualisiert: ${escapeHtml(new Date(d.ota_last_success * 1000).toLocaleString("de-DE"))}</p>` : ""}
          ` : ""}
          ${d.build_log || d.ota_log ? `<details class="more" style="margin-top:14px"><summary>Protokoll</summary><pre class="log">${escapeHtml(d.ota_log && ["running", "error", "success"].includes(d.ota_status) && d.ota_log ? d.ota_log : d.build_log)}</pre></details>` : ""}
          <details class="more" style="margin-top:8px"><summary>Verkabelung</summary>
            <p class="hint">ESP GPIO${c.radar_tx_pin} (TX) → LD2460 Pin 8 (Rx2) · ESP GPIO${c.radar_rx_pin} (RX) ← Pin 7 (Tx2) · 5 V → VCC · GND → GND.
            ${c.antenna_select_pin !== null ? `GPIO${c.antenna_select_pin} wird beim Start low gehalten (Antennenwahl).` : ""} VDD33 bleibt frei.</p></details>
        </div>

        <div class="card"><h2>Home Assistant</h2>
          <p class="hint">Home Assistant findet den Sensor selbst (ESPHome-Integration). Beim Hinzufügen fragt es nach dem Schlüssel — der steht unter „Zugangsdaten“.
          Räume und Zonen kommen zusätzlich als eigene Geräte über MQTT.</p>
          <details class="more" style="margin-top:10px" id="creds"><summary>${icon("key")} Zugangsdaten anzeigen</summary><div id="creds-body" class="hint">Wird geladen…</div></details>
          <div class="actions" style="margin-top:10px"><button class="btn small" id="probe">Erreichbarkeit prüfen</button>
            ${c.web_server ? `<a class="btn small" target="_blank" rel="noopener" href="http://${encodeURIComponent(d.address || c.name + ".local")}/">Statusseite</a>` : ""}</div>
          <p class="hint" id="probe-result" style="margin-top:8px"></p>
        </div>

        <details class="card more" style="padding:18px"><summary>Einstellungen</summary>
          <form id="settings">
            ${field("Name", `<input name="friendly_name" value="${escapeHtml(c.friendly_name || "")}" maxlength="40">`, `Knotenname <span class="mono">${escapeHtml(c.name)}</span> und Board ${escapeHtml(board.label)} bleiben fest.`)}
            <div class="row2">
              ${field("WLAN-Name", `<input name="wifi_ssid" value="${escapeHtml(c.wifi_ssid)}" maxlength="32">`)}
              ${field("WLAN-Passwort", `<input name="wifi_password" type="password" placeholder="unverändert" autocomplete="new-password">`)}
            </div>
            ${board.dual_band ? field("WLAN-Band", `<select name="wifi_band">${[["2.4GHz", "2,4 GHz"], ["5GHz", "5 GHz"], ["auto", "automatisch"]].map(([v, l]) => `<option value="${v}" ${c.wifi_band === v ? "selected" : ""}>${l}</option>`).join("")}</select>`) : ""}
            <div class="row3">
              ${field("TX-Pin", `<input name="radar_tx_pin" type="number" min="0" max="56" value="${c.radar_tx_pin}">`)}
              ${field("RX-Pin", `<input name="radar_rx_pin" type="number" min="0" max="56" value="${c.radar_rx_pin}">`)}
              ${field("Antennen-Pin", `<input name="antenna_select_pin" type="number" min="0" max="56" value="${c.antenna_select_pin ?? ""}" placeholder="—">`)}
            </div>
            <label class="check"><input type="checkbox" name="radar_quiet_means_empty" ${c.radar_quiet_means_empty ? "checked" : ""}><span>Stille = leerer Raum<small>Ändert, was „0 Personen“ bedeutet — erst nach Beobachtung im leeren Raum einschalten.</small></span></label>
            <label class="check"><input type="checkbox" name="diagnostics" ${c.diagnostics ? "checked" : ""}><span>Diagnose-Entitäten</span></label>
            <label class="check"><input type="checkbox" name="web_server" ${c.web_server ? "checked" : ""}><span>Statusseite auf dem Gerät</span></label>
            ${field("Log-Level", `<select name="log_level">${["NONE", "ERROR", "WARN", "INFO", "DEBUG", "VERBOSE"].map((l) => `<option ${c.log_level === l ? "selected" : ""}>${l}</option>`).join("")}</select>`)}
            <p class="hint">Alles außer dem Namen wird erst mit dem nächsten Build und Flash wirksam.</p>
            <div class="actions" style="margin-top:12px"><button class="btn primary" type="submit">Speichern</button></div>
          </form>
        </details>

        <div class="card"><button class="btn danger" id="delete">${icon("trash")}Sensor löschen</button>
          <p class="hint" style="margin-top:8px">Nimmt ihn auch aus seinem Raum. Das Gerät selbst läuft weiter, bis es neu geflasht wird.</p></div>`;
      this.bindDevice(sheet, d);
    },

    bindDevice(sheet, d) {
      const $ = (s) => sheet.body.querySelector(s);
      const id = encodeURIComponent(d.id);
      $("#build").addEventListener("click", async () => {
        try { await api(`api/devices/${id}/build`, { method: "POST" }); await E.refresh(); toast("Build gestartet."); }
        catch (err) { toast(err.message, "err"); }
      });
      const tc = $("#toolchain");
      if (tc) tc.addEventListener("click", async () => {
        try { await api(`api/devices/${id}/toolchain/reset`, { method: "POST" }); toast("Toolchain entfernt — der nächste Build lädt sie neu."); }
        catch (err) { toast(err.message, "err"); }
      });
      const ota = $("#ota");
      if (ota) ota.addEventListener("click", async () => {
        const address = $("#ota-address").value.trim();
        try { await api(`api/devices/${id}/ota`, { method: "POST", body: address ? { address } : {} }); await E.refresh(); toast("Update über WLAN gestartet."); }
        catch (err) { toast(err.message, "err"); }
      });
      $("#creds").addEventListener("toggle", async (e) => {
        if (!e.target.open) return;
        try {
          const c = await api(`api/devices/${id}/credentials`);
          $("#creds-body").innerHTML = `
            ${field("API-Schlüssel (für Home Assistant)", `<div class="secret">${escapeHtml(c.api_encryption_key)}</div>`)}
            ${field("OTA-Passwort", `<div class="secret">${escapeHtml(c.ota_password)}</div>`)}
            ${field(`Notfall-WLAN „${escapeHtml(d.fallback_ssid)}“`, `<div class="secret">${escapeHtml(c.fallback_password)}</div>`,
              "Öffnet der Sensor nur, wenn er dein WLAN nicht erreicht.")}`;
        } catch (err) { $("#creds-body").textContent = err.message; }
      });
      $("#probe").addEventListener("click", async () => {
        const out = $("#probe-result");
        out.textContent = "Wird geprüft…";
        try {
          const address = $("#ota-address") ? $("#ota-address").value.trim() : "";
          const r = await api(`api/devices/${id}/reachability${address ? `?host=${encodeURIComponent(address)}` : ""}`);
          out.textContent = r.message;
        } catch (err) { out.textContent = err.message; }
      });
      $("#settings").addEventListener("submit", async (e) => {
        e.preventDefault();
        const f = e.target;
        const n = (v) => (v === "" ? null : Number(v));
        const config = {
          wifi_ssid: f.wifi_ssid.value,
          radar_tx_pin: n(f.radar_tx_pin.value),
          radar_rx_pin: n(f.radar_rx_pin.value),
          antenna_select_pin: n(f.antenna_select_pin.value),
          radar_quiet_means_empty: f.radar_quiet_means_empty.checked,
          diagnostics: f.diagnostics.checked,
          web_server: f.web_server.checked,
          log_level: f.log_level.value,
        };
        if (f.wifi_band) config.wifi_band = f.wifi_band.value;
        if (f.wifi_password.value) config.wifi_password = f.wifi_password.value;
        const c = d.config;
        const changed = Object.fromEntries(Object.entries(config).filter(([k, v]) => k === "wifi_password" || c[k] !== v));
        try {
          if (Object.keys(changed).length) await api(`api/devices/${id}/config`, { method: "PATCH", body: changed });
          if ((f.friendly_name.value.trim() || null) !== (c.friendly_name || null)) {
            await api(`api/devices/${id}`, { method: "PATCH", body: { friendly_name: f.friendly_name.value.trim() || null } });
          }
          await E.refresh();
          toast("Gespeichert.");
        } catch (err) { toast(err.message, "err"); }
      });
      $("#delete").addEventListener("click", async () => {
        const ok = await E.confirmDialog({ title: "Sensor löschen?", text: "Zugangsdaten und gebaute Firmware in Echolot gehen verloren.", confirm: "Löschen", danger: true });
        if (!ok) return;
        try { await api(`api/devices/${id}`, { method: "DELETE" }); sheet.close(); await E.refresh(); toast("Sensor gelöscht."); }
        catch (err) { toast(err.message, "err"); }
      });
    },
  };

  E.register("devices", view);
})();
