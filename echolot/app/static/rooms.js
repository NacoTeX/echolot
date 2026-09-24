// Pages about rooms: the overview, a room's live map, the room editor,
// creating a room, and the system page.

(() => {
  const E = Echolot;
  const { escapeHtml, icon, api, toast, state } = E;

  function deviceName(id) {
    const d = state.devices.find((x) => x.id === id);
    return d ? d.config.friendly_name || d.config.name : null;
  }

  function roomType(room) {
    return E.ROOM_ICONS[room.icon] || "Raum";
  }

  // ------------------------------------------------------------ overview

  const home = {
    thumbs: [],
    mount(el) {
      this.el = el;
      this.render();
    },
    unmount() { this.thumbs.forEach((t) => t.plan.destroy()); this.thumbs = []; },
    onData() { this.render(); },
    onLive() {
      for (const t of this.thumbs) t.plan.setLive(state.live[t.id] || null);
      this.updateTiles();
    },
    render() {
      this.unmount();
      const rooms = state.rooms;
      const devices = state.devices;
      const total = rooms.reduce((n, r) => n + (E.roomStatus(r.id).count || 0), 0);
      const notices = [];
      if (state.legacy.length) {
        notices.push(`<div class="notice warn">${icon("alert")}<div class="grow"><strong>${state.legacy.length === 1 ? "Ein WLAN-CSI-Gerät" : `${state.legacy.length} WLAN-CSI-Geräte`} aus einer früheren Version</strong>
          Echolot arbeitet seit 1.0 nur noch mit Radar. Die Geräte sind samt Zugangsdaten erhalten und lassen sich auf Radar umstellen.</div>
          <a class="btn small" href="#/devices">Ansehen</a></div>`);
      }
      const info = state.info;
      if (info && info.mqtt.wanted && !info.mqtt.connected) {
        notices.push(`<div class="notice">${icon("wifi")}<div class="grow"><strong>Home Assistant bekommt noch keine Räume</strong>
          Kein MQTT-Broker verbunden${info.mqtt.error ? ` (${escapeHtml(info.mqtt.error)})` : ""}. Mit dem Mosquitto-Add-on erscheinen Räume und Zonen automatisch als Entitäten.</div></div>`);
      }
      const built = devices.some((d) => d.status === "success");
      const onboarding = !rooms.length || !devices.length ? `
        <div class="steps">
          <div class="card step ${devices.length ? "done" : ""}"><div class="num">${devices.length ? icon("check") : 1}</div>
            <h3>Sensor anlegen</h3><p>ESP32 mit HLK-LD2460 — Board und WLAN eintragen.</p>
            <a class="btn small ${devices.length ? "" : "primary"}" href="#/devices/new">Sensor anlegen</a></div>
          <div class="card step ${built ? "done" : ""}"><div class="num">${built ? icon("check") : 2}</div>
            <h3>Firmware aufspielen</h3><p>Einmal über USB aus dem Browser, danach per WLAN.</p>
            <a class="btn small" href="#/devices">Zu den Sensoren</a></div>
          <div class="card step ${rooms.length ? "done" : ""}"><div class="num">${rooms.length ? icon("check") : 3}</div>
            <h3>Raum einrichten</h3><p>Grundriss, Sensorposition und Zonen — wie in der Aqara-App.</p>
            <a class="btn small ${devices.length && !rooms.length ? "primary" : ""}" href="#/new-room">Raum hinzufügen</a></div>
        </div>` : "";

      this.el.innerHTML = `
        <div class="page-head">
          <div><div class="eyebrow">Zuhause</div><h1>Räume</h1>
            <p class="sub" id="home-sub">${rooms.length ? `${rooms.length} ${rooms.length === 1 ? "Raum" : "Räume"} · ${total ? `${E.people(total)} erkannt` : "niemand erkannt"}` : "Noch keine Räume eingerichtet"}</p></div>
          <div class="head-actions"><a class="btn primary" href="#/new-room">${icon("plus")}Raum</a></div>
        </div>
        ${notices.join("")}${onboarding}
        <div class="tiles" id="tiles">
          ${rooms.map((r) => `
            <button class="room-tile" data-room="${escapeHtml(r.id)}">
              <div class="tile-head"><div class="room-icon">${icon(r.icon)}</div>
                <div><div class="tile-name">${escapeHtml(r.name)}</div><div class="tile-state"></div></div></div>
              <div class="tile-count"></div>
              <div class="tile-plan"></div>
              <div class="tile-zones"></div>
            </button>`).join("")}
          <a class="room-tile add-tile" href="#/new-room">${icon("plus")}Raum hinzufügen</a>
        </div>`;
      this.el.querySelectorAll("[data-room]").forEach((tile) => {
        const room = rooms.find((r) => r.id === tile.dataset.room);
        tile.addEventListener("click", () => E.go(`#/room/${encodeURIComponent(room.id)}`));
        const plan = new Plan.PlanView(tile.querySelector(".tile-plan"), { mode: "thumb" });
        plan.setRoom(room);
        plan.setLive(state.live[room.id] || null);
        this.thumbs.push({ id: room.id, plan });
      });
      this.updateTiles();
    },
    updateTiles() {
      let total = 0;
      for (const room of state.rooms) {
        const tile = this.el.querySelector(`[data-room="${CSS.escape(room.id)}"]`);
        if (!tile) continue;
        const status = E.roomStatus(room.id);
        total += status.count || 0;
        tile.classList.toggle("present", status.kind === "present");
        tile.querySelector(".tile-state").textContent = status.kind === "present" ? `Anwesend · ${status.text}` : status.text;
        tile.querySelector(".tile-count").textContent = status.count ? status.count : "";
        const live = state.live[room.id];
        const zones = (live && live.available ? live.zones : []).filter((z) => z.occupied);
        tile.querySelector(".tile-zones").innerHTML = zones
          .map((z) => `<span class="chip present">${escapeHtml(z.name)}${z.count ? ` · ${z.count}` : ""}</span>`).join("");
      }
      const sub = this.el.querySelector("#home-sub");
      if (sub && state.rooms.length) {
        sub.textContent = `${state.rooms.length} ${state.rooms.length === 1 ? "Raum" : "Räume"} · ${total ? `${E.people(total)} erkannt` : "niemand erkannt"}`;
      }
    },
  };

  // ------------------------------------------------------------ live room

  function sensorLines(live, room) {
    const s = live && live.sensor;
    const name = deviceName(room.sensor.device_id);
    if (!room.sensor.device_id) return `<p class="hint">Diesem Raum ist kein Sensor zugeordnet.</p>`;
    const linkState = { receiving: "empfängt", quiet: "still", unknown: "Modul antwortet nicht" };
    const rows = [
      ["Sensor", name ? `<a href="#/device/${encodeURIComponent(room.sensor.device_id)}">${escapeHtml(name)}</a>` : "—"],
      ["Verbindung", s ? (s.connected ? '<span class="chip ok">verbunden</span>' : '<span class="chip err">getrennt</span>') : "—"],
      ["Radar", s && s.link_state ? escapeHtml(linkState[s.link_state] || s.link_state) : "—"],
      ["Letzte Meldung", s && s.frame_age_s !== null ? `vor ${E.formatNumber(s.frame_age_s, 1)} s` : "—"],
      ["WLAN", s && s.wifi_signal !== null ? `${Math.round(s.wifi_signal)} dBm` : "—"],
      ["Modul-Firmware", s && s.firmware ? escapeHtml(s.firmware) : "—"],
    ];
    return `<div class="stat-lines">${rows.map(([a, b]) => `<div class="stat-line"><span>${a}</span><span>${b}</span></div>`).join("")}</div>`;
  }

  const roomView = {
    mount(el, params) {
      this.el = el;
      this.id = params.id;
      this.render();
    },
    unmount() { if (this.plan) this.plan.destroy(); this.plan = null; },
    onData() {
      const room = state.rooms.find((r) => r.id === this.id);
      if (!room) { this.render(); return; }
      if (this.plan && JSON.stringify(room) !== this.roomJson) {
        this.roomJson = JSON.stringify(room);
        this.plan.setRoom(room);
        this.renderHead(room);
      }
    },
    onLive() { this.update(); },
    render() {
      this.unmount();
      const room = state.rooms.find((r) => r.id === this.id);
      if (!room) {
        this.el.innerHTML = `<div class="empty-state card pad">Diesen Raum gibt es nicht mehr. <a href="#/">Zur Übersicht</a></div>`;
        return;
      }
      this.roomJson = JSON.stringify(room);
      this.el.innerHTML = `
        <div class="page-head" id="room-head"></div>
        <div class="room-layout">
          <section class="card plan-card">
            <div class="plan-top"><span id="room-chip" class="chip">…</span><span class="dims" id="room-dims"></span></div>
            <div class="plan-stage" id="stage"><div class="plan-overlay" id="overlay" hidden></div></div>
            <div class="plan-legend">
              <span><i class="lg-dot"></i>erkanntes Ziel</span>
              <span><i class="lg-pending"></i>noch nicht bestätigt</span>
              <span><i class="lg-ring"></i>zählt nicht (außerhalb / ausgeschlossen / Störquelle)</span>
              <span><i class="lg-zone"></i>Zone</span>
              <span><i class="lg-fov"></i>geplanter Sichtbereich</span>
              <span>Raster 0,5 m</span>
            </div>
          </section>
          <aside class="side">
            <div class="card"><div class="eyebrow">Gerade im Raum</div>
              <div class="big-row"><div class="big-number" id="room-count">–</div><div id="room-state"></div></div>
              <p class="hint" id="room-reason" style="margin-top:10px"></p></div>
            <div class="card"><h2>Zonen</h2><div class="zone-list" id="zone-list"></div></div>
            <div class="card"><h2>Sensor</h2><div id="sensor-lines"></div></div>
          </aside>
        </div>`;
      this.renderHead(room);
      this.plan = new Plan.PlanView(this.el.querySelector("#stage"), { mode: "live" });
      this.plan.setRoom(room);
      this.update();
    },
    renderHead(room) {
      this.el.querySelector("#room-head").innerHTML = `
        <div><div class="eyebrow">${escapeHtml(roomType(room))}</div><h1>${escapeHtml(room.name)}</h1></div>
        <div class="head-actions">
          <a class="btn" href="#/room/${encodeURIComponent(room.id)}/calibrate">${icon("target")}Kalibrieren</a>
          <a class="btn primary" href="#/room/${encodeURIComponent(room.id)}/edit">${icon("edit")}Raum einrichten</a>
        </div>`;
      this.el.querySelector("#room-dims").textContent =
        room.outline
          ? `${E.formatNumber(window.EcholotGeometry.polygonArea(room.outline))} m² · ${E.formatNumber(room.width)} × ${E.formatNumber(room.height)} m`
          : `${E.formatNumber(room.width)} × ${E.formatNumber(room.height)} m`;
    },
    update() {
      if (!this.plan) return;
      const room = state.rooms.find((r) => r.id === this.id);
      if (!room) return;
      const live = state.live[room.id] || null;
      this.plan.setLive(live);
      const status = E.roomStatus(room.id);
      const chip = this.el.querySelector("#room-chip");
      chip.className = `chip ${status.kind === "present" ? "present" : status.kind === "empty" ? "ok" : "warn"}`;
      chip.textContent = status.kind === "present" ? "Anwesend" : status.kind === "empty" ? "Leer" : "Nicht verfügbar";
      this.el.querySelector("#room-count").textContent = status.count === null ? "–" : status.count;
      this.el.querySelector("#room-state").innerHTML = live && live.available && live.occupied && !live.count
        ? `<span class="chip present">hält noch ${E.formatSeconds(live.hold_remaining)}</span>` : "";
      const reason = this.el.querySelector("#room-reason");
      reason.textContent = live && !live.available ? live.reason_text : live && live.available ? "Gezählt werden bestätigte Ziele. Echolot folgt ihnen von Meldung zu Meldung — wer wer ist, weiß das Radar nicht." : "";

      const overlay = this.el.querySelector("#overlay");
      if (live && !live.available && (live.reason === "no_sensor" || live.reason === "offline" || live.reason === "device_missing" || live.reason === "no_frame_entity")) {
        overlay.hidden = false;
        const action = live.reason === "no_sensor" || live.reason === "device_missing"
          ? `<a class="btn primary" href="#/room/${encodeURIComponent(room.id)}/edit">Sensor zuordnen</a>`
          : `<a class="btn primary" href="#/device/${encodeURIComponent(room.sensor.device_id)}">Zum Sensor</a>`;
        overlay.innerHTML = `<div class="card"><h2>${live.reason === "no_sensor" ? "Noch kein Sensor" : "Keine Live-Daten"}</h2>
          <p class="hint" style="margin-bottom:14px">${escapeHtml(live.reason_text)}${live.sensor && live.sensor.error ? `<br>${escapeHtml(live.sensor.error)}` : ""}</p>${action}</div>`;
      } else {
        overlay.hidden = true;
      }

      const zoneList = this.el.querySelector("#zone-list");
      const detect = room.zones.filter((z) => z.kind === "detect");
      const excluded = room.zones.filter((z) => z.kind === "exclude");
      zoneList.innerHTML = detect.length ? detect.map((z) => {
        const zs = live && live.available ? live.zones.find((x) => x.id === z.id) : null;
        const meta = !zs ? "nicht verfügbar" : zs.count ? "belegt" : zs.occupied ? `hält noch ${E.formatSeconds(zs.hold_remaining)}` : "frei";
        return `<div class="zone-row ${zs && zs.occupied ? "present" : ""}">
          <span class="swatch" style="background:${Plan.zoneColor(z.color)}"></span>
          <div class="grow"><div class="name">${escapeHtml(z.name)}</div><div class="meta">${meta}</div></div>
          <div class="count">${zs ? zs.count : "–"}</div></div>`;
      }).join("") + (excluded.length ? `<p class="hint">${excluded.length} Ausschlusszone${excluded.length === 1 ? "" : "n"}: ${excluded.map((z) => escapeHtml(z.name)).join(", ")}</p>` : "")
        : `<div class="empty-state">Noch keine Zonen. Unter „Raum einrichten“ zeichnest du zum Beispiel das Sofa oder den Esstisch ein.</div>`;
      this.el.querySelector("#sensor-lines").innerHTML = sensorLines(live, room);
    },
  };

  // --------------------------------------------------------------- editor

  function field(label, inner, hint = "") {
    return `<label class="field"><span>${label}</span>${inner}${hint ? `<p class="hint">${hint}</p>` : ""}</label>`;
  }

  function num(name, value, { min, max, step = 0.05 } = {}) {
    return `<input type="number" data-f="${name}" value="${Number(value).toFixed(2).replace(/\.?0+$/, "") || 0}"
              ${min !== undefined ? `min="${min}"` : ""} ${max !== undefined ? `max="${max}"` : ""} step="${step}" inputmode="decimal">`;
  }

  const editor = {
    mount(el, params) {
      this.el = el;
      this.id = params.id;
      const room = state.rooms.find((r) => r.id === this.id);
      if (!room) {
        el.innerHTML = `<div class="empty-state card pad">Diesen Raum gibt es nicht mehr. <a href="#/">Zur Übersicht</a></div>`;
        return;
      }
      this.saving = false;
      el.innerHTML = `
        <div class="page-head">
          <div><div class="eyebrow">Raum einrichten</div><h1 id="ed-title">${escapeHtml(room.name)}</h1></div>
          <div class="head-actions">
            <button class="btn" id="ed-cancel">Fertig</button>
            <button class="btn primary" id="ed-save" disabled>${icon("check")}Speichern</button>
          </div>
        </div>
        <div class="room-layout">
          <section class="card plan-card">
            <div class="plan-top">
              <div class="toolbar" id="toolbar">
                <button class="tool active" data-tool="select" title="Auswählen (V)">${icon("cursor")}Auswahl</button>
                <button class="tool" data-tool="rect" title="Rechteckige Zone (R)">${icon("rect")}Rechteck</button>
                <button class="tool" data-tool="poly" title="Freie Zone (P)">${icon("polygon")}Freiform</button>
                <span class="tool-sep"></span>
                <button class="tool" id="t-furn" title="Möbel hinzufügen">${icon("armchair")}Möbel</button>
                <button class="tool" id="t-sensor" title="Sensor auswählen">${icon("sensor")}Sensor</button>
                <button class="tool" id="t-walls" title="Wände formen: Nischen, L-Form">${icon("walls")}Wände</button>
                <span class="tool-sep"></span>
                <button class="tool active" id="t-snap" title="Am 5-cm-Raster einrasten">${icon("magnet")}</button>
                <button class="tool" id="t-undo" title="Rückgängig (⌘Z)" disabled>${icon("undo")}</button>
                <button class="tool" id="t-redo" title="Wiederholen (⇧⌘Z)" disabled>${icon("redo")}</button>
              </div>
              <span class="dims" id="ed-dims"></span>
            </div>
            <div class="editor-hint" id="ed-hint"></div>
            <div class="plan-stage" id="ed-stage"></div>
          </section>
          <aside class="side" id="inspector"></aside>
        </div>`;
      this.plan = new Plan.PlanView(el.querySelector("#ed-stage"), {
        mode: "edit",
        onChange: () => this.changed(),
        onSelect: (sel, opts) => this.inspect(opts && opts.live),
        onHint: (text) => { el.querySelector("#ed-hint").textContent = text; },
      });
      this.plan.onToolChange = (tool) => this.markTool(tool);
      this.plan.setRoom(room);
      this.plan.setLive(state.live[room.id] || null);
      this.plan.setTool("select");
      this.bindToolbar();
      this.inspect();
      this.changed();
    },
    unmount() { if (this.plan) this.plan.destroy(); this.plan = null; },
    dirty() { return this.plan && this.plan.isDirty(); },
    async canLeave() {
      if (!this.dirty()) return true;
      return E.confirmDialog({ title: "Änderungen verwerfen?", text: "Der Raum hat ungespeicherte Änderungen.",
        confirm: "Verwerfen", danger: true });
    },
    onLive() { if (this.plan) this.plan.setLive(state.live[this.id] || null); },
    onData() {},

    bindToolbar() {
      const el = this.el;
      el.querySelectorAll("[data-tool]").forEach((b) => b.addEventListener("click", () => {
        this.plan.setTool(b.dataset.tool);
        this.markTool(b.dataset.tool);
      }));
      el.querySelector("#t-furn").addEventListener("click", () => { this.plan.select(null); this.showPalette = true; this.inspect(); });
      el.querySelector("#t-sensor").addEventListener("click", () => this.plan.select({ kind: "sensor" }));
      el.querySelector("#t-walls").addEventListener("click", () => this.plan.shapeWalls());
      el.querySelector("#t-snap").addEventListener("click", (e) => {
        this.plan.snap = !this.plan.snap;
        e.currentTarget.classList.toggle("active", this.plan.snap);
      });
      el.querySelector("#t-undo").addEventListener("click", () => this.plan.undo());
      el.querySelector("#t-redo").addEventListener("click", () => this.plan.redo());
      el.querySelector("#ed-save").addEventListener("click", () => this.save());
      el.querySelector("#ed-cancel").addEventListener("click", () => E.go(`#/room/${encodeURIComponent(this.id)}`));
    },
    markTool(tool) {
      this.el.querySelectorAll("[data-tool]").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
    },
    changed() {
      if (!this.plan) return;
      const r = this.plan.room;
      this.el.querySelector("#t-undo").disabled = !this.plan.canUndo();
      this.el.querySelector("#t-redo").disabled = !this.plan.canRedo();
      this.el.querySelector("#ed-save").disabled = !this.plan.isDirty() || this.saving;
      this.el.querySelector("#ed-dims").textContent = r.outline
        ? `${E.formatNumber(window.EcholotGeometry.polygonArea(r.outline))} m² · ${E.formatNumber(r.width)} × ${E.formatNumber(r.height)} m`
        : `${E.formatNumber(r.width)} × ${E.formatNumber(r.height)} m`;
      this.el.querySelector("#ed-title").textContent = r.name;
      if (!this.plan.drag) this.inspect(false, true);
    },

    // The inspector follows the selection: nothing selected shows the room.
    inspect(liveDrag = false, keepFocus = false) {
      const host = this.el.querySelector("#inspector");
      if (!host || !this.plan) return;
      if (keepFocus && host.contains(document.activeElement) && document.activeElement.matches("input, select")) return;
      const sel = this.plan.selection;
      const item = this.plan.find(sel);
      if (liveDrag && item) {
        // Numbers follow the drag without rebuilding the form.
        host.querySelectorAll("[data-f]").forEach((input) => {
          const v = input.dataset.f;
          if (v in item && typeof item[v] === "number") input.value = Math.round(item[v] * 100) / 100;
        });
        return;
      }
      if (sel && sel.kind === "zone" && item) this.showPalette = false, host.innerHTML = this.zoneForm(item);
      else if (sel && sel.kind === "furniture" && item) this.showPalette = false, host.innerHTML = this.furnitureForm(item);
      else if (sel && sel.kind === "sensor") this.showPalette = false, host.innerHTML = this.sensorForm(this.plan.room.sensor);
      else if (sel && sel.kind === "outline" && item) this.showPalette = false, host.innerHTML = this.outlineForm(item.points);
      else host.innerHTML = (this.showPalette ? this.paletteCard() : "") + this.roomForm(this.plan.room);
      this.bindInspector(host, sel, item);
    },

    paletteCard() {
      return `<div class="card"><h2>Möbel hinzufügen</h2><div class="palette">
        ${Object.entries(Plan.FURNITURE).map(([k, f]) => `<button data-add="${k}">${icon(f.icon)}${escapeHtml(f.label)}</button>`).join("")}
      </div><p class="hint" style="margin-top:10px">Möbel sind Orientierung auf der Karte. Gezählt wird nur in Zonen.</p></div>`;
    },

    roomForm(r) {
      const others = new Set(state.rooms.filter((x) => x.id !== this.id).map((x) => x.sensor.device_id).filter(Boolean));
      const options = state.devices.map((d) => `<option value="${escapeHtml(d.id)}" ${r.sensor.device_id === d.id ? "selected" : ""}
          ${others.has(d.id) ? "disabled" : ""}>${escapeHtml(d.config.friendly_name || d.config.name)}${others.has(d.id) ? " (in anderem Raum)" : ""}</option>`).join("");
      return `
        <div class="card"><h2>Raum</h2>
          ${field("Name", `<input data-room="name" value="${escapeHtml(r.name)}" maxlength="40">`)}
          ${field("Art", `<select data-room="icon">${Object.entries(E.ROOM_ICONS).map(([k, v]) => `<option value="${k}" ${r.icon === k ? "selected" : ""}>${v}</option>`).join("")}</select>`)}
          <div class="row2">
            ${field("Breite · m", `<input type="number" data-room="width" value="${r.width}" min="1" max="30" step="0.1" inputmode="decimal">`)}
            ${field("Tiefe · m", `<input type="number" data-room="height" value="${r.height}" min="1" max="30" step="0.1" inputmode="decimal">`)}
          </div>
          <div class="field"><span>Form</span><div class="segmented">
            <button type="button" data-shape="rect" class="${r.outline ? "" : "active"}">Rechteck</button>
            <button type="button" data-shape="free" class="${r.outline ? "active" : ""}">Freiform</button></div>
            <p class="hint">${r.outline
              ? `Die Wände folgen dem Umriss (${r.outline.length} Ecken). Alles außerhalb zählt nicht. <a href="#" data-walls>Wände bearbeiten</a>`
              : "Mit Freiform formst du Nischen, L-Formen und Vorsprünge nach; was außerhalb der Wände liegt, zählt dann nicht."}</p></div>
          ${field("Sensor", `<select data-room="device"><option value="">— keiner —</option>${options}</select>`,
            state.devices.length ? "" : 'Noch kein Sensor angelegt. <a href="#/devices/new">Sensor anlegen</a>')}
          ${field(`Abwesenheitsverzögerung · <b data-out="hold">${E.formatSeconds(r.hold_s)}</b>`,
            `<input type="range" data-room="hold_s" min="0" max="300" step="5" value="${r.hold_s}">`,
            "So lange gilt der Raum nach dem letzten erkannten Ziel noch als belegt. Hilft, wenn das Radar ruhig sitzende Personen kurz verliert.")}
          ${field(`Randtoleranz · <b data-out="margin">${Math.round(r.edge_margin_m * 100)} cm</b>`,
            `<input type="range" data-room="edge_margin_m" min="0" max="1.5" step="0.05" value="${r.edge_margin_m}">`,
            "Ziele bis zu diesem Abstand hinter der Wand zählen noch mit. Radar sieht durch Trockenbau.")}
        </div>
        <div class="card"><h2>Grundriss</h2>
          <p class="hint" style="margin-bottom:12px">PNG, JPEG oder WebP bis 4 MB, auf die Raumkanten zugeschnitten. Das Bild wird auf Breite × Tiefe gelegt.</p>
          <div class="actions">
            <label class="btn">${icon("image")}${r.image ? "Bild ersetzen" : "Bild wählen"}<input type="file" id="ed-image" accept="image/png,image/jpeg,image/webp" hidden></label>
            ${r.image ? `<button class="btn danger" id="ed-image-remove">${icon("trash")}Entfernen</button>` : ""}
          </div>
          ${r.image ? field("Deckkraft", `<input type="range" data-room="opacity" min="0.1" max="1" step="0.05" value="${r.image.opacity}">`) : ""}
        </div>
        <div class="card"><h2>Zonen</h2>
          ${r.zones.length ? `<div class="zone-list">${r.zones.map((z) => `
            <button class="zone-row" data-pick="${escapeHtml(z.id)}" style="cursor:pointer;text-align:left">
              <span class="swatch" style="background:${z.kind === "exclude" ? "var(--danger)" : Plan.zoneColor(z.color)}"></span>
              <span class="grow"><span class="name">${escapeHtml(z.name)}</span><br><span class="meta">${z.kind === "exclude" ? "Ausschluss" : "Erkennung"} · ${z.points.length} Ecken</span></span>
            </button>`).join("")}</div>`
            : `<p class="hint">Mit „Rechteck“ oder „Freiform“ in der Werkzeugleiste zeichnest du eine Zone direkt auf den Plan.</p>`}
        </div>
        <div class="card"><button class="btn danger" id="ed-delete-room">${icon("trash")}Raum löschen</button></div>`;
    },

    sensorForm(s) {
      const name = deviceName(s.device_id);
      return `<div class="card"><h2>Sensor</h2>
        <p class="hint" style="margin-bottom:12px">${name ? `Position von <b>${escapeHtml(name)}</b>.` : "Kein Sensor zugeordnet — wähle ihn in den Raumeinstellungen."}
          Auf dem Plan ziehen verschiebt ihn, der Griff vor ihm dreht ihn.</p>
        <div class="row2">${field("X · m", num("x", s.x, { min: 0, step: 0.05 }))}${field("Y · m", num("y", s.y, { min: 0, step: 0.05 }))}</div>
        ${field("Blickrichtung · Grad", `<div class="actions" style="flex-wrap:nowrap">
            <button class="btn small" data-turn="-15" type="button">${icon("undo")}</button>
            ${num("angle", s.angle, { min: -360, max: 360, step: 1 })}
            <button class="btn small" data-turn="15" type="button">${icon("redo")}</button></div>`,
          "0° blickt nach unten in den Plan, positive Werte drehen im Uhrzeigersinn.")}
        <label class="check"><input type="checkbox" data-f="mirror" ${s.mirror ? "checked" : ""}>
          <span>Links und rechts tauschen<small>Welche Seite das Modul positiv zählt, steht nicht im Handbuch. Geh einmal quer vor dem Sensor entlang: läuft der Punkt in die Gegenrichtung, hier umschalten.</small></span></label>
        <div class="row2">${field("Reichweite · m", num("range_m", s.range_m, { min: 1, max: 12, step: 0.5 }))}${field("Öffnungswinkel · °", num("fov_deg", s.fov_deg, { min: 30, max: 180, step: 5 }))}</div>
        <p class="hint">Reichweite und Winkel zeichnen nur den gestrichelten Planungsbereich. Wie weit das Modul wirklich sieht, zeigen die Live-Punkte.</p>
        ${this.plan.room.outline && !window.EcholotGeometry.withinWalls(s.x, s.y, 0, 0, this.plan.room.outline, 0.3)
          ? `<div class="notice warn" style="margin-top:12px">${icon("alert")}<div class="grow">Der Sensor steht außerhalb der Wände.</div></div>` : ""}
        <p class="hint">Genauer als von Hand: <a href="#/room/${encodeURIComponent(this.id)}/calibrate">Kalibrieren</a> richtet den Sensor an Standpunkten aus.</p>
        <div class="actions" style="margin-top:14px"><button class="btn" data-done>Fertig</button></div></div>`;
    },

    outlineForm(points) {
      const G = window.EcholotGeometry;
      const r = this.plan.room;
      const crossed = G.selfIntersects(points);
      const sensorOut = !G.withinWalls(r.sensor.x, r.sensor.y, r.width, r.height, points, 0.3);
      return `<div class="card"><h2>Wände</h2>
        <p class="hint" style="margin-bottom:12px">Ecken ziehen formt Nischen und Vorsprünge nach. Die kleinen Punkte zwischen zwei Ecken fügen eine hinzu, Doppelklick auf eine Ecke entfernt sie. Was außerhalb der Wände liegt — schraffiert —, zählt nicht, bis auf die Randtoleranz von ${Math.round(r.edge_margin_m * 100)} cm.</p>
        <div class="stat-lines">
          <div class="stat-line"><span>Fläche</span><span>${E.formatNumber(G.polygonArea(points))} m²</span></div>
          <div class="stat-line"><span>Ecken</span><span>${points.length}</span></div>
        </div>
        ${crossed ? `<div class="notice err" style="margin-top:12px">${icon("alert")}<div class="grow">Die Wände kreuzen sich. Zieh die Ecke zurück, die auf der falschen Seite liegt — so lässt sich der Raum nicht speichern.</div></div>` : ""}
        ${sensorOut ? `<div class="notice warn" style="margin-top:12px">${icon("alert")}<div class="grow">Der Sensor steht außerhalb der Wände. Er gehört an eine Wand oder in den Raum.</div></div>` : ""}
        <div class="actions" style="margin-top:14px">
          <button class="btn" data-done>Fertig</button>
          <button class="btn" data-redraw>${icon("polygon")}Neu nachzeichnen</button>
          <button class="btn" data-rect>Zurück zum Rechteck</button></div></div>`;
    },

    zoneForm(z) {
      const live = state.live[this.id];
      const zs = live && live.available ? live.zones.find((x) => x.id === z.id) : null;
      return `<div class="card"><h2>Zone</h2>
        ${field("Name", `<input data-f="name" value="${escapeHtml(z.name)}" maxlength="40">`)}
        <div class="field"><span>Art</span><div class="segmented">
          <button type="button" data-kind="detect" class="${z.kind === "detect" ? "active" : ""}">Erkennung</button>
          <button type="button" data-kind="exclude" class="${z.kind === "exclude" ? "active" : ""}">Ausschluss</button></div>
          <p class="hint">${z.kind === "exclude"
            ? "Ziele hier zählen nirgends — für Ventilator, Vorhang, Aquarium."
            : "Zählt die Ziele in der Fläche und wird in Home Assistant zu einem Belegt-Sensor und einer Personenzahl."}</p></div>
        ${z.kind === "detect" ? `
          ${field(`Abwesenheitsverzögerung · <b data-out="zhold">${E.formatSeconds(z.hold_s)}</b>`, `<input type="range" data-f="hold_s" min="0" max="300" step="5" value="${z.hold_s}">`)}
          <div class="field"><span>Farbe</span><div class="swatches">${[0, 1, 2, 3, 4, 5, 6, 7].map((i) =>
            `<button type="button" data-color="${i}" class="${z.color === i ? "active" : ""}" style="background:${Plan.zoneColor(i)}" aria-label="Farbe ${i + 1}"></button>`).join("")}</div></div>
          ${zs ? `<p class="hint">Jetzt: ${zs.count ? E.people(zs.count) : "frei"}${zs.occupied && !zs.count ? ` (hält noch ${E.formatSeconds(zs.hold_remaining)})` : ""}</p>` : ""}` : ""}
        <p class="hint">${z.points.length} Eckpunkte. Punkte ziehen verformt, Doppelklick auf einen Punkt entfernt ihn, die kleinen Punkte dazwischen fügen einen hinzu.</p>
        <div class="actions" style="margin-top:14px">
          <button class="btn" data-done>Fertig</button>
          <button class="btn danger" data-remove>${icon("trash")}Löschen</button></div></div>`;
    },

    furnitureForm(f) {
      return `<div class="card"><h2>${escapeHtml(Plan.FURNITURE[f.kind] ? Plan.FURNITURE[f.kind].label : "Objekt")}</h2>
        ${field("Bezeichnung", `<input data-f="name" value="${escapeHtml(f.name)}" maxlength="40">`)}
        ${field("Art", `<select data-f="kind">${Object.entries(Plan.FURNITURE).map(([k, v]) => `<option value="${k}" ${f.kind === k ? "selected" : ""}>${v.label}</option>`).join("")}</select>`)}
        <div class="row2">${field("Breite · m", num("w", f.w, { min: 0.1, step: 0.05 }))}${field("Tiefe · m", num("h", f.h, { min: 0.1, step: 0.05 }))}</div>
        <div class="row2">${field("X · m", num("x", f.x, { step: 0.05 }))}${field("Y · m", num("y", f.y, { step: 0.05 }))}</div>
        ${field("Drehung · Grad", num("angle", f.angle, { min: -360, max: 360, step: 15 }))}
        <div class="actions" style="margin-top:6px">
          <button class="btn" data-done>Fertig</button>
          <button class="btn danger" data-remove>${icon("trash")}Entfernen</button></div></div>`;
    },

    bindInspector(host, sel, item) {
      const plan = this.plan;
      const r = plan.room;
      host.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => plan.addFurniture(b.dataset.add)));
      host.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", () => plan.select({ kind: "zone", id: b.dataset.pick })));
      host.querySelectorAll("[data-done]").forEach((b) => b.addEventListener("click", () => plan.select(null)));
      host.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", () => plan.removeSelected()));
      host.querySelectorAll("[data-redraw]").forEach((b) => b.addEventListener("click", () => plan.drawWalls()));
      host.querySelectorAll("[data-rect]").forEach((b) => b.addEventListener("click", () => plan.rectangleWalls()));
      host.querySelectorAll("[data-walls]").forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); plan.shapeWalls(); }));
      host.querySelectorAll("[data-shape]").forEach((b) => b.addEventListener("click", () => {
        if (b.dataset.shape === "free") plan.shapeWalls(); else { plan.rectangleWalls(); this.inspect(); }
      }));

      // Item fields.
      host.querySelectorAll("[data-f]").forEach((input) => {
        const key = input.dataset.f;
        const target = sel && sel.kind === "sensor" ? r.sensor : item;
        if (!target) return;
        const apply = (commit) => {
          let value;
          if (input.type === "checkbox") value = input.checked;
          else if (input.type === "number" || input.type === "range") {
            value = Number(input.value);
            if (!Number.isFinite(value)) return;
            if (key === "x") value = window.EcholotGeometry.clamp(value, sel.kind === "furniture" ? -target.w / 2 : 0, r.width);
            if (key === "y") value = window.EcholotGeometry.clamp(value, sel.kind === "furniture" ? -target.h / 2 : 0, r.height);
            if (key === "w") value = window.EcholotGeometry.clamp(value, 0.1, r.width);
            if (key === "h") value = window.EcholotGeometry.clamp(value, 0.1, r.height);
          } else value = input.value;
          if (key === "name" && typeof value === "string" && !value.trim() && sel.kind === "zone") return;
          target[key] = value;
          const out = host.querySelector('[data-out="zhold"]');
          if (key === "hold_s" && out) out.textContent = E.formatSeconds(value);
          plan.render();
          if (commit) { plan.commit(); if (key === "kind") this.inspect(); }
        };
        input.addEventListener("input", () => apply(false));
        input.addEventListener("change", () => apply(true));
      });
      host.querySelectorAll("[data-turn]").forEach((b) => b.addEventListener("click", () => {
        plan.mutate((room) => { room.sensor.angle = ((room.sensor.angle + Number(b.dataset.turn) + 540) % 360) - 180; });
        this.inspect();
      }));
      host.querySelectorAll("[data-kind]").forEach((b) => b.addEventListener("click", () => {
        plan.mutate(() => { item.kind = b.dataset.kind; });
        this.inspect();
      }));
      host.querySelectorAll("[data-color]").forEach((b) => b.addEventListener("click", () => {
        plan.mutate(() => { item.color = Number(b.dataset.color); });
        this.inspect();
      }));

      // Room fields.
      host.querySelectorAll("[data-room]").forEach((input) => {
        const key = input.dataset.room;
        const apply = (commit) => {
          if (key === "name") { if (!input.value.trim()) return; r.name = input.value.slice(0, 40); }
          else if (key === "icon") r.icon = input.value;
          else if (key === "device") r.sensor.device_id = input.value || null;
          else if (key === "opacity") r.image.opacity = Number(input.value);
          else if (key === "width" || key === "height") {
            const v = Number(input.value);
            if (!Number.isFinite(v) || v < 1 || v > 30) return;
            if (!commit) return;
            this.resize(key, v);
          } else {
            r[key] = Number(input.value);
            const out = host.querySelector(`[data-out="${key === "hold_s" ? "hold" : "margin"}"]`);
            if (out) out.textContent = key === "hold_s" ? E.formatSeconds(r.hold_s) : `${Math.round(r.edge_margin_m * 100)} cm`;
          }
          plan.render();
          if (commit) plan.commit();
        };
        input.addEventListener("input", () => apply(false));
        input.addEventListener("change", () => apply(true));
      });
      const image = host.querySelector("#ed-image");
      if (image) image.addEventListener("change", () => this.uploadImage(image.files[0]));
      const removeImage = host.querySelector("#ed-image-remove");
      if (removeImage) removeImage.addEventListener("click", () => this.removeImage());
      const del = host.querySelector("#ed-delete-room");
      if (del) del.addEventListener("click", () => this.deleteRoom());
    },

    // Shrinking a room must not leave anything outside it: the server
    // refuses such a room, and silently clipping a zone would change
    // what it counts.
    resize(key, value) {
      const r = this.plan.room;
      const limit = value;
      const axis = key === "width" ? 0 : 1;
      const tooFar = r.zones.some((z) => z.points.some((p) => p[axis] > limit + 1e-6))
        || (r.outline || []).some((p) => p[axis] > limit + 1e-6)
        || r.furniture.some((f) => (axis === 0 ? f.x + f.w / 2 : f.y + f.h / 2) > limit);
      if (tooFar) {
        toast("Erst Wände, Zonen und Möbel verschieben — sonst lägen sie außerhalb des Plans.", "err");
        this.inspect();
        return;
      }
      r[key] = value;
      if (axis === 0) r.sensor.x = Math.min(r.sensor.x, value); else r.sensor.y = Math.min(r.sensor.y, value);
    },

    async uploadImage(file) {
      if (!file) return;
      try {
        const updated = await api(`api/rooms/${encodeURIComponent(this.id)}/image`, { method: "PUT", body: file, contentType: file.type });
        this.plan.room.revision = updated.revision;
        this.plan.room.image = updated.image;
        this.plan.render();
        await E.refresh();
        this.inspect();
        toast("Grundriss hinterlegt.");
      } catch (err) { toast(err.message, "err"); }
    },

    async removeImage() {
      try {
        const updated = await api(`api/rooms/${encodeURIComponent(this.id)}/image`, { method: "DELETE" });
        this.plan.room.revision = updated.revision;
        this.plan.room.image = null;
        this.plan.render();
        await E.refresh();
        this.inspect();
      } catch (err) { toast(err.message, "err"); }
    },

    async deleteRoom() {
      const ok = await E.confirmDialog({ title: "Raum löschen?", text: "Seine Zonen verschwinden auch aus Home Assistant. Der Sensor bleibt erhalten.",
        confirm: "Löschen", danger: true });
      if (!ok) return;
      try {
        await api(`api/rooms/${encodeURIComponent(this.id)}`, { method: "DELETE" });
        this.plan.savedJson = this.plan.history[this.plan.historyIndex];
        await E.refresh();
        E.go("#/");
      } catch (err) { toast(err.message, "err"); }
    },

    async save() {
      if (this.saving) return;
      if (this.plan.room.outline && window.EcholotGeometry.selfIntersects(this.plan.room.outline)) {
        toast("Die Wände kreuzen sich — erst die Ecke zurückziehen, dann speichern.", "err");
        this.plan.select({ kind: "outline" });
        return;
      }
      this.saving = true;
      this.changed();
      const room = this.plan.room;
      const payload = { ...room, image: room.image ? { opacity: room.image.opacity } : null };
      try {
        const saved = await api(`api/rooms/${encodeURIComponent(this.id)}`, { method: "PUT", body: payload });
        const selection = this.plan.selection;
        this.plan.setRoom(saved);
        this.plan.selection = selection && this.plan.find(selection) ? selection : null;
        this.plan.render();
        await E.refresh();
        toast("Raum gespeichert.");
      } catch (err) {
        if (err.status === 409 && err.detail && err.detail.room) {
          const reload = await E.confirmDialog({ title: "Raum wurde woanders geändert",
            text: "Jemand hat diesen Raum inzwischen gespeichert, vielleicht in einem anderen Tab. Neu laden verwirft deine Änderungen.",
            confirm: "Neu laden", cancel: "Weiter bearbeiten" });
          if (reload) { this.plan.setRoom(err.detail.room); this.plan.select(null); await E.refresh(); }
        } else {
          toast(err.message, "err");
        }
      } finally {
        this.saving = false;
        this.changed();
      }
    },
  };

  // ------------------------------------------------------------- new room

  const newRoom = {
    mount(el) {
      const taken = new Set(state.rooms.map((r) => r.sensor.device_id).filter(Boolean));
      const free = state.devices.filter((d) => !taken.has(d.id));
      el.innerHTML = `
        <div class="page-head"><div><div class="eyebrow">Neuer Raum</div><h1>Raum hinzufügen</h1>
          <p class="sub">Maße und Sensor jetzt, Grundriss und Zonen gleich danach im Editor.</p></div></div>
        <form class="card pad" id="nr" style="max-width:640px">
          ${field("Name", `<input name="name" required maxlength="40" placeholder="z. B. Wohnzimmer" autofocus>`)}
          <div class="field"><span>Art</span><div class="palette" id="nr-icons">
            ${Object.entries(E.ROOM_ICONS).map(([k, v], i) => `<button type="button" data-icon="${k}" ${i === 0 ? 'style="border-color:var(--accent)"' : ""}>${icon(k)}${v}</button>`).join("")}
          </div></div>
          <div class="row2">
            ${field("Breite · m", `<input name="width" type="number" min="1" max="30" step="0.1" value="5" inputmode="decimal">`)}
            ${field("Tiefe · m", `<input name="height" type="number" min="1" max="30" step="0.1" value="4" inputmode="decimal">`)}
          </div>
          <p class="hint" style="margin:-4px 0 12px">Außenmaße des Raums. Nischen, L-Formen und Vorsprünge formst du danach im Editor unter „Wände“ nach.</p>
          ${field("Sensor", `<select name="device_id"><option value="">— später zuordnen —</option>
            ${free.map((d) => `<option value="${escapeHtml(d.id)}">${escapeHtml(d.config.friendly_name || d.config.name)}</option>`).join("")}</select>`,
            free.length ? "Ein Radar sieht einen Raum; jeder Sensor steht in höchstens einem." : 'Kein freier Sensor. <a href="#/devices/new">Sensor anlegen</a>')}
          <div class="actions"><button class="btn primary" type="submit">${icon("check")}Anlegen und einrichten</button>
            <a class="btn" href="#/">Abbrechen</a></div>
        </form>`;
      let chosen = "living";
      el.querySelectorAll("[data-icon]").forEach((b) => b.addEventListener("click", () => {
        chosen = b.dataset.icon;
        el.querySelectorAll("[data-icon]").forEach((x) => { x.style.borderColor = x === b ? "var(--accent)" : ""; });
        const name = el.querySelector('[name="name"]');
        if (!name.value.trim() && chosen !== "generic") name.value = E.ROOM_ICONS[chosen];
      }));
      el.querySelector("#nr").addEventListener("submit", async (e) => {
        e.preventDefault();
        const form = new FormData(e.target);
        try {
          const room = await api("api/rooms", { method: "POST", body: {
            name: form.get("name").trim(), icon: chosen, width: Number(form.get("width")), height: Number(form.get("height")),
            device_id: form.get("device_id") || null } });
          await E.refresh();
          E.go(`#/room/${encodeURIComponent(room.id)}/edit`);
        } catch (err) { toast(err.message, "err"); }
      });
    },
  };

  // --------------------------------------------------------------- system

  const system = {
    async mount(el) {
      await E.loadInfo();
      const info = state.info || {};
      const mqtt = info.mqtt || {};
      const theme = (() => { try { return localStorage.getItem("echolot-theme") || "auto"; } catch { return "auto"; } })();
      el.innerHTML = `
        <div class="page-head"><div><div class="eyebrow">Echolot</div><h1>System</h1></div></div>
        <div class="device-grid">
          <div class="card pad"><h2>Add-on</h2><div class="stat-lines">
            <div class="stat-line"><span>Version</span><span>${escapeHtml(info.version || "—")}</span></div>
            <div class="stat-line"><span>ESPHome</span><span>${info.esphome && info.esphome.available ? escapeHtml(info.esphome.version) : '<span class="chip err">nicht verfügbar</span>'}</span></div>
            <div class="stat-line"><span>Räume</span><span>${state.rooms.length}</span></div>
            <div class="stat-line"><span>Sensoren</span><span>${state.devices.length}</span></div>
          </div></div>
          <div class="card pad"><h2>Home Assistant</h2><div class="stat-lines">
            <div class="stat-line"><span>MQTT-Export</span><span>${mqtt.wanted === false ? "abgeschaltet" : mqtt.connected ? '<span class="chip ok">verbunden</span>' : `<span class="chip warn">nicht verbunden</span>`}</span></div>
          </div>
          <p class="hint" style="margin-top:12px">Je Raum entsteht ein Gerät mit „Anwesenheit“ und „Personen“, je Erkennungszone ein weiteres Paar. ${mqtt.error ? escapeHtml(mqtt.error) : ""}</p></div>
          <div class="card pad"><h2>Darstellung</h2><div class="segmented" id="theme">
            <button data-theme="auto" class="${theme === "auto" ? "active" : ""}">Automatisch</button>
            <button data-theme="light" class="${theme === "light" ? "active" : ""}">Hell</button>
            <button data-theme="dark" class="${theme === "dark" ? "active" : ""}">Dunkel</button></div></div>
          <div class="card pad"><h2>Hilfe</h2><p class="hint">Verkabelung, Einrichtung und was noch offen ist:</p>
            <p><a href="https://github.com/NacoTeX/echolot/blob/main/echolot/DOCS.md" target="_blank" rel="noreferrer">Dokumentation öffnen</a></p></div>
        </div>`;
      el.querySelectorAll("[data-theme]").forEach((b) => b.addEventListener("click", () => {
        applyTheme(b.dataset.theme);
        el.querySelectorAll("[data-theme]").forEach((x) => x.classList.toggle("active", x === b));
      }));
    },
  };

  function applyTheme(theme) {
    try { localStorage.setItem("echolot-theme", theme); } catch { /* per-browser nicety only */ }
    if (theme === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  }
  try { const t = localStorage.getItem("echolot-theme"); if (t && t !== "auto") document.documentElement.dataset.theme = t; } catch { /* ignore */ }

  E.register("home", home);
  E.register("room", roomView);
  E.register("editor", editor);
  E.register("newRoom", newRoom);
  E.register("system", system);
})();
